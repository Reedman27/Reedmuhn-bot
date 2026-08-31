"""WebUI -> Discord moderation action queue.

The dashboard is a separate process, so destructive Discord actions requested
from the WebUI are queued in SQLite and executed by the bot process. This keeps
Discord API access and permission checks in the bot while still making the
feature fully usable from the dashboard.
"""
import datetime
import logging
import time

import discord
from discord.ext import commands, tasks

import scheduler
from utils import format_duration, restore_stripped_roles

logger = logging.getLogger("dashboardmoderation")

# Actions the WebUI can queue for a member, and how they're described back
# in the dashboard's action log. Kept in sync with the choices rendered in
# webui/main.py's MOD_ACTION_LABELS (a separate dict since the dashboard is
# a separate process - see the note atop db.py about this codebase's
# process split).
MOD_ACTION_LABELS = {
    "kick": "kicked",
    "ban": "banned",
    "tempban": "temporarily banned",
    "unban": "unbanned",
    "mute_role": "muted",
    "unmute_role": "unmuted",
    "timeout": "timed out",
    "untimeout": "had their timeout removed",
}
# Actions that take a duration_seconds value.
TIMED_MOD_ACTIONS = {"tempban", "mute_role", "timeout"}
MAX_TIMEOUT_SECONDS = 28 * 86400


class DashboardModeration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_purge_requests.start()
        self.poll_mod_action_requests.start()
        self.poll_mute_role_sync_requests.start()

    def cog_unload(self):
        self.poll_purge_requests.cancel()
        self.poll_mod_action_requests.cancel()
        self.poll_mute_role_sync_requests.cancel()

    async def _channel(self, guild_id: int, channel_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None, "The bot is no longer in that server."
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                return None, "The channel no longer exists."
            except discord.Forbidden:
                return None, "The bot cannot access that channel."
            except discord.HTTPException as exc:
                return None, f"Discord could not resolve the channel: {exc}"
        if not isinstance(channel, discord.TextChannel):
            return None, "Target channel is not a standard text channel."
        me = guild.me
        if me is not None and not channel.permissions_for(me).manage_messages:
            return None, "The bot does not have Manage Messages in that channel."
        return channel, None

    @staticmethod
    async def _purge(channel: discord.TextChannel, amount: int, user_id: int | None, reason: str) -> tuple[int, dict[str, int]]:
        deleted = 0
        before = None
        fourteen_days = 14 * 86400
        author_counts: dict[int, int] = {}
        author_names: dict[int, str] = {}

        def _record(msg: discord.Message) -> None:
            author_counts[msg.author.id] = author_counts.get(msg.author.id, 0) + 1
            author_names[msg.author.id] = str(msg.author)

        while deleted < amount:
            batch_size = min(100, amount - deleted)
            kwargs = {"limit": batch_size}
            if before is not None:
                kwargs["before"] = before
            messages = [m async for m in channel.history(**kwargs)]
            if not messages:
                break
            targets = messages if user_id is None else [m for m in messages if m.author.id == user_id]
            targets = targets[: amount - deleted]
            recent = [m for m in targets if (discord.utils.utcnow() - m.created_at).total_seconds() < fourteen_days]
            old = [m for m in targets if m not in recent]
            if recent:
                try:
                    # delete_messages() returns None, not the deleted list -
                    # count from `recent` itself rather than its return value.
                    await channel.delete_messages(recent)
                except (discord.Forbidden, discord.HTTPException):
                    for message in recent:
                        try:
                            await message.delete(reason=reason)
                            deleted += 1
                            _record(message)
                        except (discord.NotFound, discord.Forbidden):
                            pass
                else:
                    deleted += len(recent)
                    for message in recent:
                        _record(message)
            for message in old:
                if deleted >= amount:
                    break
                try:
                    await message.delete(reason=reason)
                    deleted += 1
                    _record(message)
                except (discord.NotFound, discord.Forbidden):
                    pass
            before = messages[-1]
            if len(messages) < batch_size:
                break
        # Named by display name (not just ID) so the dashboard can render the
        # breakdown without a separate member lookup - same info /purge's
        # ephemeral reply shows.
        breakdown = {author_names[uid]: count for uid, count in author_counts.items()}
        return deleted, breakdown

    @tasks.loop(seconds=2)
    async def poll_purge_requests(self):
        try:
            requests = self.bot.db.claim_purge_requests(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI purge requests")
            return
        for request_id, guild_id, channel_id, user_id, amount, reason in requests:
            try:
                channel, error = await self._channel(guild_id, channel_id)
                if channel is None:
                    self.bot.db.complete_purge_request(request_id, error)
                    continue
                deleted, breakdown = await self._purge(channel, amount, user_id, reason)
                self.bot.db.complete_purge_request(request_id, deleted_count=deleted, breakdown=breakdown)
                logger.info("WebUI purge %s deleted %s messages in %s", request_id, deleted, channel_id)
            except Exception as exc:
                logger.exception("WebUI purge %s failed", request_id)
                self.bot.db.complete_purge_request(request_id, str(exc)[:500])

    @poll_purge_requests.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    # ---- muted-role settings sync (WebUI changes the policy in the db,
    # the bot applies it to every channel) ----

    @tasks.loop(seconds=2)
    async def poll_mute_role_sync_requests(self):
        try:
            requests = self.bot.db.claim_mute_role_sync_requests(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI muted-role sync requests")
            return
        for request_id, guild_id, reason in requests:
            try:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    self.bot.db.complete_mute_role_sync(request_id, "The bot is no longer in that server.")
                    continue
                moderation_cog = self.bot.get_cog("Moderation")
                if moderation_cog is None:
                    self.bot.db.complete_mute_role_sync(request_id, "The Moderation cog isn't loaded.")
                    continue
                # Same lookup/create the /muterole commands use, so a WebUI
                # settings change auto-creates the role too if none is
                # configured yet, instead of silently doing nothing.
                role = await moderation_cog.get_or_create_muted_role(guild)
                if role is None:
                    self.bot.db.complete_mute_role_sync(request_id, "Couldn't find or create the Muted role - the bot needs Manage Roles.")
                    continue
                if guild.me and role.position >= guild.me.top_role.position:
                    self.bot.db.complete_mute_role_sync(request_id, "The Muted role is not below the bot's role - move the bot's role higher.")
                    continue
                changed, failed = await moderation_cog.apply_muted_role_overwrites(guild, role)
                self.bot.db.complete_mute_role_sync(request_id, changed=changed, failed=failed)
                logger.info("WebUI muted-role sync %s applied to %s channel(s), %s failed, in guild %s", request_id, changed, failed, guild_id)
            except Exception as exc:
                logger.exception("WebUI muted-role sync %s failed", request_id)
                self.bot.db.complete_mute_role_sync(request_id, str(exc)[:500])

    @poll_mute_role_sync_requests.before_loop
    async def before_poll_mute_role_sync(self):
        await self.bot.wait_until_ready()

    # ---- mod actions (kick/ban/mute/timeout etc. queued from the WebUI) ----

    async def _guild(self, guild_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None, "The bot is no longer in that server."
        return guild, None

    async def _member(self, guild: discord.Guild, user_id: int):
        """Resolves a live discord.Member for role/kick/timeout-style actions,
        which (unlike ban/unban) only make sense for someone currently in the
        server."""
        member = guild.get_member(user_id)
        if member is not None:
            return member, None
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return None, "That member is no longer in the server."
        except discord.Forbidden:
            return None, "The bot can't look up members in that server."
        except discord.HTTPException as exc:
            return None, f"Discord could not resolve the member: {exc}"
        return member, None

    def _hierarchy_error(self, guild: discord.Guild, member: discord.Member) -> str | None:
        me = guild.me
        if me is None:
            return None
        if member.id == guild.owner_id:
            return "that member owns the server."
        if member.top_role >= me.top_role:
            return "that member has a role equal to or higher than the bot's own role."
        return None

    @tasks.loop(seconds=2)
    async def poll_mod_action_requests(self):
        try:
            requests = self.bot.db.claim_mod_actions(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI mod action requests")
            return
        for request_id, guild_id, user_id, action, duration_seconds, reason in requests:
            try:
                error = await self._apply_mod_action(guild_id, user_id, action, duration_seconds, reason)
                if not error and action in {"kick", "ban", "tempban", "mute_role", "timeout"} and reason.startswith("Warnings:"):
                    self.bot.db.clear_automod_violations(guild_id, user_id)
                    self.bot.db.set_escalation_reset(guild_id, user_id)
                self.bot.db.complete_mod_action(request_id, error)
                if error:
                    logger.warning("WebUI mod action %s (%s on %s in %s) failed: %s", request_id, action, user_id, guild_id, error)
                else:
                    logger.info("WebUI mod action %s (%s) applied to %s in %s", request_id, action, user_id, guild_id)
            except Exception as exc:
                logger.exception("WebUI mod action %s failed", request_id)
                self.bot.db.complete_mod_action(request_id, str(exc)[:500])

    @poll_mod_action_requests.before_loop
    async def before_poll_mod_actions(self):
        await self.bot.wait_until_ready()

    async def _apply_mod_action(self, guild_id: int, user_id: int, action: str, duration_seconds: int | None, reason: str) -> str | None:
        """Applies one queued mod action. Returns an error string on
        failure, or None on success."""
        guild, error = await self._guild(guild_id)
        if guild is None:
            return error

        if action == "ban":
            return await self._ban(guild, user_id, reason)
        if action == "tempban":
            return await self._tempban(guild, user_id, duration_seconds, reason)
        if action == "unban":
            return await self._unban(guild, user_id, reason)

        member, error = await self._member(guild, user_id)
        if member is None:
            return error

        hierarchy_error = self._hierarchy_error(guild, member)
        if hierarchy_error and action in {"kick", "timeout", "mute_role"}:
            return f"Can't {action.replace('_', ' ')} that member - {hierarchy_error}"

        if action == "kick":
            return await self._kick(guild, member, reason)
        if action == "timeout":
            return await self._timeout(guild, member, duration_seconds, reason)
        if action == "untimeout":
            return await self._untimeout(guild, member, reason)
        if action == "mute_role":
            return await self._mute_role(guild, member, duration_seconds, reason)
        if action == "unmute_role":
            return await self._unmute_role(guild, member, reason)

        return f"Unknown mod action {action!r}."

    async def _ban(self, guild: discord.Guild, user_id: int, reason: str) -> str | None:
        try:
            await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_seconds=0)
        except discord.Forbidden:
            return "The bot can't ban that user - check its role and Ban Members permission."
        except discord.HTTPException as exc:
            return f"Discord rejected the ban: {exc}"
        self._history(guild.id, user_id, "ban", reason)
        await self._log(guild, "ban", user_id, reason)
        return None

    async def _tempban(self, guild: discord.Guild, user_id: int, duration_seconds: int | None, reason: str) -> str | None:
        if not duration_seconds or duration_seconds < 1:
            return "Missing or invalid duration for a temp ban."
        try:
            await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_seconds=0)
        except discord.Forbidden:
            return "The bot can't ban that user - check its role and Ban Members permission."
        except discord.HTTPException as exc:
            return f"Discord rejected the ban: {exc}"
        run_at = int(time.time()) + duration_seconds
        scheduler.schedule_unban(self.bot.db, guild.id, user_id, run_at)
        self._history(guild.id, user_id, "tempban", reason, f"duration_seconds={duration_seconds}")
        await self._log(guild, f"temp ban ({format_duration(duration_seconds)})", user_id, reason)
        return None

    async def _unban(self, guild: discord.Guild, user_id: int, reason: str) -> str | None:
        try:
            await guild.unban(discord.Object(id=user_id), reason=reason)
        except discord.NotFound:
            return "That user isn't currently banned."
        except discord.Forbidden:
            return "The bot can't unban that user - check its Ban Members permission."
        except discord.HTTPException as exc:
            return f"Discord rejected the unban: {exc}"
        self._history(guild.id, user_id, "unban", reason)
        await self._log(guild, "unban", user_id, reason)
        return None

    async def _kick(self, guild: discord.Guild, member: discord.Member, reason: str) -> str | None:
        try:
            await member.kick(reason=reason)
        except discord.Forbidden:
            return "The bot can't kick that member - check its role is above theirs."
        except discord.HTTPException as exc:
            return f"Discord rejected the kick: {exc}"
        self._history(guild.id, member.id, "kick", reason)
        await self._log(guild, "kick", member.id, reason)
        return None

    async def _timeout(self, guild: discord.Guild, member: discord.Member, duration_seconds: int | None, reason: str) -> str | None:
        if not duration_seconds or duration_seconds < 1:
            return "Missing or invalid duration for a timeout."
        if duration_seconds > MAX_TIMEOUT_SECONDS:
            return "Timeouts are capped at 28 days by Discord."
        try:
            await member.timeout(discord.utils.utcnow() + datetime.timedelta(seconds=duration_seconds), reason=reason)
        except discord.Forbidden:
            return "The bot can't time out that member - check its role is above theirs."
        except discord.HTTPException as exc:
            return f"Discord rejected the timeout: {exc}"
        self._history(guild.id, member.id, "timeout", reason, f"duration_seconds={duration_seconds}")
        await self._log(guild, f"timeout ({format_duration(duration_seconds)})", member.id, reason)
        return None

    async def _untimeout(self, guild: discord.Guild, member: discord.Member, reason: str) -> str | None:
        try:
            await member.timeout(None, reason=reason)
        except discord.Forbidden:
            return "The bot can't remove that member's timeout - check its role is above theirs."
        except discord.HTTPException as exc:
            return f"Discord rejected the request: {exc}"
        self._history(guild.id, member.id, "untimeout", reason)
        await self._log(guild, "timeout removed", member.id, reason)
        return None

    async def _mute_role(self, guild: discord.Guild, member: discord.Member, duration_seconds: int | None, reason: str) -> str | None:
        if not duration_seconds or duration_seconds < 1:
            return "Missing or invalid duration for a mute."
        moderation_cog = self.bot.get_cog("Moderation")
        if moderation_cog is None:
            return "The Moderation cog isn't loaded."
        ok, why = await moderation_cog.apply_role_mute(member, duration_seconds, reason)
        if not ok:
            return f"Couldn't apply the Muted role - {why}."
        self._history(guild.id, member.id, "mute", reason, f"duration_seconds={duration_seconds}")
        await self._log(guild, f"muted ({format_duration(duration_seconds)})", member.id, reason)
        return None

    async def _unmute_role(self, guild: discord.Guild, member: discord.Member, reason: str) -> str | None:
        cfg = self.bot.db.get_guild_config(guild.id)
        role = guild.get_role(cfg["muted_role_id"]) if cfg["muted_role_id"] else None
        if role is None or role not in member.roles:
            return "That member doesn't have the Muted role."
        try:
            await member.remove_roles(role, reason=reason)
        except discord.Forbidden:
            return "The bot can't remove that member's Muted role - check its role is above theirs."
        except discord.HTTPException as exc:
            return f"Discord rejected the request: {exc}"
        await restore_stripped_roles(self.bot.db, guild, member, reason=reason)
        self._history(guild.id, member.id, "unmute", reason)
        await self._log(guild, "unmuted", member.id, reason)
        return None

    def _history(self, guild_id: int, user_id: int, event_type: str, reason: str, details: str | None = None) -> None:
        # actor_id 0 is the same "issued from the dashboard" sentinel used
        # by the WebUI's add-warn route - there's no per-admin dashboard
        # login to attribute it to a specific Discord user.
        self.bot.db.record_member_history(guild_id, user_id, event_type, 0, reason, details)

    async def _log(self, guild: discord.Guild, action_label: str, user_id: int, reason: str) -> None:
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is None:
            return
        member = guild.get_member(user_id)
        target_desc = f"{member.mention} ({member})" if member else f"<@{user_id}> ({user_id})"
        embed = discord.Embed(
            description=f"**Dashboard: {action_label}** - {target_desc}",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Moderator", value="Dashboard", inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=True)
        embed.set_footer(text=f"User ID: {user_id}")
        await logging_cog.log_event(guild, "moderation", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardModeration(bot))
