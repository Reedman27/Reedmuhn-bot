"""Emergency Control Center - a handful of blunt, high-blast-radius actions
for an active raid or incident: lock every text channel, revoke every
invite link, or mass-timeout a whole role at once.

Dashboard-only by design, deliberately with no matching slash commands.
Everything else destructive in this codebase (ban included - see
dashboardmoderation.py's _ban) either has a slash command as the primary
path with the dashboard as a convenience, or is dashboard-only when the
action needs a "read a list, act on many things at once" workflow the
WebUI already supports and a slash command awkwardly wouldn't (this is
that same case, at a larger blast radius than anything else in the app -
these actions affect the whole server or a whole role at once, not one
member - so the dashboard's typed-confirmation-phrase safety net is worth
requiring on the one path that reaches them, rather than also offering a
faster but less-guarded Discord command for the same thing).

Lockdown intentionally only touches the @everyone role's send_messages
overwrite (and only where it isn't already explicitly denied), remembering
each channel's prior value so Unlock restores it exactly - never "no
overwrite" by default, which could accidentally open a channel that was
deliberately locked before the emergency started.
"""
import logging

import discord
from discord.ext import commands, tasks
import datetime
import json

logger = logging.getLogger("emergency")

MAX_MASS_TIMEOUT_SECONDS = 28 * 86400  # Discord's own timeout ceiling


class Emergency(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_emergency_requests.start()

    def cog_unload(self):
        self.poll_emergency_requests.cancel()

    @tasks.loop(seconds=2)
    async def poll_emergency_requests(self):
        try:
            requests = self.bot.db.claim_emergency_requests(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI emergency requests")
            return
        for request_id, guild_id, action, params_json in requests:
            try:
                params = json.loads(params_json) if params_json else {}
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    self.bot.db.complete_emergency_request(request_id, "The bot is no longer in that server.")
                    continue

                if action == "lockdown":
                    result = await self._lockdown(guild, params.get("started_by", 0))
                elif action == "unlock":
                    result = await self._unlock(guild)
                elif action == "revoke_invites":
                    result = await self._revoke_invites(guild)
                elif action == "mass_timeout":
                    result = await self._mass_timeout(
                        guild, params.get("role_id"), params.get("duration_seconds"), params.get("reason", "")
                    )
                else:
                    self.bot.db.complete_emergency_request(request_id, f"Unknown emergency action {action!r}.")
                    continue

                self.bot.db.complete_emergency_request(request_id, result=result)
                await self._log(guild, action, result)
                logger.info("WebUI emergency action %s (%s) applied in guild %s: %s", request_id, action, guild_id, result)
            except Exception as exc:
                logger.exception("WebUI emergency request %s failed", request_id)
                self.bot.db.complete_emergency_request(request_id, str(exc)[:500])

    @poll_emergency_requests.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    # ---- lockdown / unlock ----

    async def _lockdown(self, guild: discord.Guild, started_by: int) -> dict:
        everyone = guild.default_role
        me = guild.me
        prior_by_channel: dict[int, bool | None] = {}
        locked, skipped, failed = 0, 0, 0

        for channel in guild.text_channels:
            if me is not None and not channel.permissions_for(me).manage_permissions:
                failed += 1
                continue
            overwrite = channel.overwrites_for(everyone)
            if overwrite.send_messages is False:
                skipped += 1  # already locked - don't record or touch it, Unlock shouldn't un-restrict this one
                continue
            prior_by_channel[channel.id] = overwrite.send_messages
            overwrite.send_messages = False
            try:
                await channel.set_permissions(everyone, overwrite=overwrite, reason="Emergency lockdown (dashboard)")
                locked += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
                prior_by_channel.pop(channel.id, None)

        if prior_by_channel:
            self.bot.db.set_lockdown_state(guild.id, prior_by_channel, started_by)
        return {"locked": locked, "already_locked": skipped, "failed": failed}

    async def _unlock(self, guild: discord.Guild) -> dict:
        state = self.bot.db.get_lockdown_state(guild.id)
        if state is None:
            return {"unlocked": 0, "failed": 0, "note": "No lockdown was active."}
        everyone = guild.default_role
        unlocked, failed = 0, 0
        for channel_id_str, prior in state["channel_overwrites"].items():
            channel = guild.get_channel(int(channel_id_str))
            if channel is None:
                continue
            overwrite = channel.overwrites_for(everyone)
            overwrite.send_messages = prior
            try:
                if overwrite.is_empty():
                    await channel.set_permissions(everyone, overwrite=None, reason="Emergency lockdown lifted (dashboard)")
                else:
                    await channel.set_permissions(everyone, overwrite=overwrite, reason="Emergency lockdown lifted (dashboard)")
                unlocked += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        self.bot.db.clear_lockdown_state(guild.id)
        return {"unlocked": unlocked, "failed": failed}

    # ---- revoke all invites ----

    async def _revoke_invites(self, guild: discord.Guild) -> dict:
        try:
            invites = await guild.invites()
        except discord.Forbidden:
            return {"error": "The bot needs Manage Server to list and revoke invites."}
        revoked, failed = 0, 0
        for invite in invites:
            try:
                await invite.delete(reason="Emergency: revoke all invites (dashboard)")
                revoked += 1
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                failed += 1
        return {"revoked": revoked, "failed": failed, "total": len(invites)}

    # ---- mass timeout ----

    async def _mass_timeout(self, guild: discord.Guild, role_id: int | None, duration_seconds: int | None, reason: str) -> dict:
        if not role_id:
            return {"error": "No role selected."}
        role = guild.get_role(role_id)
        if role is None:
            return {"error": "That role no longer exists."}
        if not duration_seconds or duration_seconds < 1:
            return {"error": "Missing or invalid duration."}
        duration_seconds = min(duration_seconds, MAX_MASS_TIMEOUT_SECONDS)

        me = guild.me
        timed_out, skipped, failed = 0, 0, 0
        until = discord.utils.utcnow() + datetime.timedelta(seconds=duration_seconds)
        for member in role.members:
            if member.bot or member.id == guild.owner_id or member.guild_permissions.administrator:
                skipped += 1
                continue
            if me is not None and member.top_role >= me.top_role:
                skipped += 1
                continue
            try:
                await member.timeout(until, reason=reason or "Emergency mass timeout (dashboard)")
                timed_out += 1
                self.bot.db.record_member_history(guild.id, member.id, "timeout", 0, reason or "Emergency mass timeout", f"duration_seconds={duration_seconds}")
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        return {"timed_out": timed_out, "skipped": skipped, "failed": failed, "role": role.name}

    async def _log(self, guild: discord.Guild, action: str, result: dict) -> None:
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is None:
            return
        summary = ", ".join(f"{k}={v}" for k, v in result.items())
        embed = discord.Embed(
            description=f"**Emergency: {action.replace('_', ' ')}** - {summary}",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Triggered from", value="Dashboard", inline=True)
        await logging_cog.log_event(guild, "moderation", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Emergency(bot))
