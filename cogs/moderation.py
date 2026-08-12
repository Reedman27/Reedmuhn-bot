import datetime
import time

import discord
from discord import app_commands
from discord.ext import commands

import scheduler
from utils import format_duration, parse_duration, tempnick_self_allowed, can_moderate


from utils import manager_or_permission

class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="purge", description="Delete recent messages, optionally only from one user")
    @app_commands.describe(
        amount="Number of messages to delete (1-1000)",
        user="Only delete messages sent by this member",
        reason="Reason for the cleanup",
    )
    @manager_or_permission("manage_messages")
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 1000],
        user: discord.Member = None,
        reason: str = "Manual message purge",
    ):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command only works in a server text channel.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        deleted = 0
        before = None
        scanned = 0
        fourteen_days = 14 * 86400

        # When a user is supplied, `amount` means matching messages to delete,
        # not messages to scan. This makes /purge behave like Carl's targeted
        # purge rather than requiring moderators to guess how far back to scan.
        while deleted < amount:
            batch_size = min(100, max(100, amount - deleted))
            kwargs = {"limit": batch_size}
            if before is not None:
                kwargs["before"] = before

            messages = [m async for m in interaction.channel.history(**kwargs)]
            if not messages:
                break
            scanned += len(messages)

            targets = messages if user is None else [m for m in messages if m.author.id == user.id]

            recent = [
                m for m in targets[: amount - deleted]
                if (discord.utils.utcnow() - m.created_at).total_seconds() < fourteen_days
            ]
            old = [m for m in targets[: amount - deleted] if m not in recent]

            if recent:
                try:
                    deleted += len(await interaction.channel.delete_messages(recent))
                except (discord.Forbidden, discord.HTTPException):
                    for message in recent:
                        try:
                            await message.delete(reason=reason)
                            deleted += 1
                        except (discord.NotFound, discord.Forbidden):
                            pass

            for message in old:
                if deleted >= amount:
                    break
                try:
                    await message.delete(reason=reason)
                    deleted += 1
                except (discord.NotFound, discord.Forbidden):
                    pass

            before = messages[-1]
            if len(messages) < batch_size:
                break

        target_text = f" from {user.mention}" if user else ""
        await interaction.followup.send(
            f"Deleted **{deleted}** message{'s' if deleted != 1 else ''}{target_text}.",
            ephemeral=True,
        )

        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is not None and deleted:
            embed = discord.Embed(
                description=f"**Purge** - deleted {deleted} message(s){target_text}",
                color=discord.Color.orange(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Moderator", value=interaction.user.mention)
            embed.add_field(name="Channel", value=interaction.channel.mention)
            embed.add_field(name="Reason", value=reason)
            await logging_cog.log_event(interaction.guild, "moderation", embed)

    @app_commands.command(name="tempban", description="Temporarily ban a user")
    @app_commands.describe(user="Who to ban", duration="e.g. 10m, 2h, 3d", reason="Reason for the ban")
    @manager_or_permission("ban_members")
    async def tempban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: str = "No reason given",
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        if not can_moderate(interaction.user, user):
            await interaction.response.send_message(
                "You can't moderate someone with an equal or higher role than you.", ephemeral=True
            )
            return

        try:
            seconds = parse_duration(duration)
        except ValueError:
            await interaction.response.send_message(
                "Couldn't parse that duration. Try something like `10m`, `2h`, `3d`.", ephemeral=True
            )
            return

        if seconds < 1 or seconds > 365 * 86400:
            await interaction.response.send_message("Tempban duration must be between 1 second and 365 days.", ephemeral=True)
            return

        # Ban immediately, same as a normal ban.
        await interaction.guild.ban(user, reason=reason, delete_message_seconds=0)

        # Store the unban time so the background scheduler can unban them
        # later even if the bot restarts before then.
        run_at = int(time.time()) + seconds
        scheduler.schedule_unban(self.bot.db, interaction.guild.id, user.id, run_at)
        self.bot.db.record_member_history(interaction.guild.id, user.id, "tempban", interaction.user.id, reason, f"duration_seconds={seconds}")

        await interaction.response.send_message(
            f"Banned {user.mention} for {format_duration(seconds)}. Reason: {reason}"
        )


    @app_commands.command(name="tempnick", description="Temporarily change your nickname (or someone else's, with permission)")
    @app_commands.describe(
        nickname="The temporary nickname",
        duration="e.g. 30m, 1h, 2d (defaults to 30m)",
        user="Change someone else's nickname instead of your own (requires Manage Nicknames)",
    )
    @app_commands.checks.cooldown(3, 30.0)
    async def tempnick(
        self,
        interaction: discord.Interaction,
        nickname: str,
        duration: str = "30m",
        user: discord.Member = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        target = user or interaction.user
        changing_someone_else = target.id != interaction.user.id

        if changing_someone_else:
            if not interaction.user.guild_permissions.manage_nicknames:
                await interaction.response.send_message(
                    "You need the Manage Nicknames permission to change someone else's nickname.",
                    ephemeral=True,
                )
                return
            if not can_moderate(interaction.user, target):
                await interaction.response.send_message(
                    "You can't moderate someone with an equal or higher role than you.", ephemeral=True
                )
                return
        else:
            # Self-service use - governed by this server's configurable
            # tempnick rule (set via the web dashboard), not a Discord
            # permission. Defaults to open to everyone.
            mode = self.bot.db.get_tempnick_mode(interaction.guild.id)
            configured_roles = set(self.bot.db.list_tempnick_roles(interaction.guild.id))
            member_roles = {role.id for role in interaction.user.roles}
            if not tempnick_self_allowed(mode, member_roles, configured_roles):
                await interaction.response.send_message(
                    "You're not allowed to use /tempnick on this server right now.", ephemeral=True
                )
                return

        try:
            seconds = parse_duration(duration)
        except ValueError:
            await interaction.response.send_message(
                "Couldn't parse that duration. Try something like `30m`, `1h`, `2d`.", ephemeral=True
            )
            return

        if seconds < 1 or seconds > 365 * 86400:
            await interaction.response.send_message("Tempnick duration must be between 1 second and 365 days.", ephemeral=True)
            return

        if len(nickname) > 32:
            await interaction.response.send_message("Discord nicknames can be at most 32 characters.", ephemeral=True)
            return

        original_nick = target.nick  # None if they had no nickname override set

        try:
            await target.edit(nick=nickname, reason=f"Tempnick by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't change that nickname - check that my role sits above theirs "
                "and that I have the Manage Nicknames permission.",
                ephemeral=True,
            )
            return

        run_at = int(time.time()) + seconds
        scheduler.schedule_nick_revert(self.bot.db, interaction.guild.id, run_at, target.id, original_nick)

        await interaction.response.send_message(
            f"Changed {target.mention}'s nickname to \"{nickname}\" for {format_duration(seconds)}."
        )


    # ---- warns ----

    async def _log_action(self, guild: discord.Guild, action: str, target: discord.Member, moderator: discord.Member, reason: str = None):
        """Sends a moderation-log entry for actions that are purely
        bot/DB-driven (warn, clearwarns, role-based mute/unmute) and so
        never fire a native Discord event the Logging cog's own listeners
        could pick up on their own - unlike a real kick/ban, which already
        gets logged automatically via on_member_remove/on_member_ban."""
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is None:
            return
        embed = discord.Embed(
            description=f"**{action}** - {target.mention} ({target})",
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Moderator", value=moderator.mention, inline=True)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=True)
        embed.set_footer(text=f"User ID: {target.id}")
        await logging_cog.log_event(guild, "moderation", embed)

    @app_commands.command(name="warn", description="Warn a member")
    @app_commands.describe(user="Who to warn", reason="Why they're being warned")
    @manager_or_permission("moderate_members")
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        if not can_moderate(interaction.user, user):
            await interaction.response.send_message(
                "You can't moderate someone with an equal or higher role than you.", ephemeral=True
            )
            return

        self.bot.db.add_warn(interaction.guild.id, user.id, interaction.user.id, reason, int(time.time()))
        total = self.bot.db.count_warns(interaction.guild.id, user.id)

        await interaction.response.send_message(
            f"Warned {user.mention}. Reason: {reason}\nThey now have {total} warning{'s' if total != 1 else ''}."
        )
        await self._log_action(interaction.guild, "Warned", user, interaction.user, reason)

        try:
            await user.send(f"You were warned in **{interaction.guild.name}**: {reason}")
        except discord.Forbidden:
            pass  # DMs closed - the mod-facing message above already confirms the warn happened

    @app_commands.command(name="warnings", description="List a member's warnings")
    @app_commands.describe(user="Whose warnings to list")
    @manager_or_permission("moderate_members")
    async def warnings(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        rows = self.bot.db.list_warns(interaction.guild.id, user.id)
        if not rows:
            await interaction.response.send_message(f"{user.mention} has no warnings.")
            return

        lines = []
        for warn_id, moderator_id, reason, created_at in rows:
            when = time.strftime("%Y-%m-%d", time.localtime(created_at))
            lines.append(f"`#{warn_id}` {when} by <@{moderator_id}>: {reason}")

        embed = discord.Embed(
            title=f"Warnings for {user.display_name}",
            description="\n".join(lines),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="clearwarns", description="Clear all warnings for a member")
    @app_commands.describe(user="Whose warnings to clear")
    @manager_or_permission("moderate_members")
    async def clearwarns(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        removed = self.bot.db.clear_warns(interaction.guild.id, user.id)
        await interaction.response.send_message(f"Cleared {removed} warning{'s' if removed != 1 else ''} for {user.mention}.")
        if removed:
            self.bot.db.record_member_history(interaction.guild.id, user.id, "warnings_cleared", interaction.user.id, None, f"{removed} warning(s) removed")
            await self._log_action(interaction.guild, "Warnings cleared", user, interaction.user, f"{removed} warning(s) removed")

    # ---- mute / unmute ----
    # Role-based mute with a server-configurable role and permission profile.
    # The same configuration is available from the WebUI and Discord.

    muterole = app_commands.Group(name="muterole", description="Configure the server's muted role")

    async def get_or_create_muted_role(self, guild: discord.Guild) -> discord.Role | None:
        """Return the guild's configured Muted role.

        Channel overwrites are only (re)applied here the first time a role is
        linked - either because it was just created, or because an existing
        "Muted"-named role is being linked for the first time. Once a role is
        already configured, this is a cheap lookup with no channel iteration;
        keeping the policy in sync afterward is the job of the explicit
        /muterole commands, not of every /mute call.
        """
        cfg = self.bot.db.get_guild_config(guild.id)
        role_id = cfg["muted_role_id"]
        if role_id:
            role = guild.get_role(role_id)
            if role is not None:
                return role

        existing = discord.utils.find(lambda r: r.name.lower() == "muted", guild.roles)
        if existing is not None:
            self.bot.db.set_muted_role(guild.id, existing.id)
            await self._apply_muted_role_overwrites(guild, existing)
            return existing

        try:
            role = await guild.create_role(
                name="Muted",
                permissions=discord.Permissions.none(),
                reason="Auto-created by /mute",
            )
        except discord.Forbidden:
            return None

        self.bot.db.set_muted_role(guild.id, role.id)
        await self._apply_muted_role_overwrites(guild, role)
        return role

    async def _apply_muted_role_overwrites(self, guild: discord.Guild, role: discord.Role) -> tuple[int, int]:
        cfg = self.bot.db.get_guild_config(guild.id)
        changed = 0
        failed = 0
        for channel in guild.channels:
            try:
                overwrite = channel.overwrites_for(role)
                overwrite.view_channel = False if cfg["muted_deny_view_channel"] else None
                if isinstance(channel, (discord.TextChannel, discord.ForumChannel, discord.CategoryChannel)):
                    overwrite.send_messages = False if cfg["muted_deny_send_messages"] else None
                    overwrite.add_reactions = False if cfg["muted_deny_reactions"] else None
                    overwrite.create_public_threads = False if cfg["muted_deny_threads"] else None
                    overwrite.create_private_threads = False if cfg["muted_deny_threads"] else None
                if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                    overwrite.connect = False if cfg["muted_deny_connect"] else None
                    overwrite.speak = False if cfg["muted_deny_speak"] else None
                    overwrite.stream = False if cfg["muted_deny_stream"] else None
                await channel.set_permissions(role, overwrite=overwrite, reason="Updated Muted role configuration")
                changed += 1
            except (discord.Forbidden, discord.HTTPException):
                failed += 1
        return changed, failed

    @muterole.command(name="set", description="Assign an existing role as the server's Muted role")
    @app_commands.describe(role="The role to use for mutes")
    @manager_or_permission("manage_guild")
    async def muterole_set(self, interaction: discord.Interaction, role: discord.Role):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        if role.is_default() or role.managed:
            await interaction.response.send_message("Choose a normal, non-managed role.", ephemeral=True)
            return
        if interaction.guild.me and role.position >= interaction.guild.me.top_role.position:
            await interaction.response.send_message("That role is at or above my highest role. Move my bot role above it first.", ephemeral=True)
            return
        self.bot.db.set_muted_role(interaction.guild.id, role.id)
        # Sweeping every channel can take longer than Discord's 3-second
        # interaction deadline, so defer before doing the bulk work.
        await interaction.response.defer()
        changed, failed = await self._apply_muted_role_overwrites(interaction.guild, role)
        await interaction.followup.send(
            f"Muted role set to {role.mention}. Applied the current mute policy to {changed} channel(s)"
            + (f"; {failed} could not be updated." if failed else ".")
        )

    @muterole.command(name="create", description="Create or repair the server's Muted role")
    @manager_or_permission("manage_guild")
    async def muterole_create(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        # Defer up front: get_or_create_muted_role may need to create the role
        # and sweep every channel, which can exceed Discord's 3-second deadline.
        await interaction.response.defer()
        role = await self.get_or_create_muted_role(interaction.guild)
        if role is None:
            await interaction.followup.send("I need Manage Roles to create the Muted role.", ephemeral=True)
            return
        if interaction.guild.me and role.position >= interaction.guild.me.top_role.position:
            await interaction.followup.send("The Muted role is not below my bot role. Move my bot role higher first.", ephemeral=True)
            return
        changed, failed = await self._apply_muted_role_overwrites(interaction.guild, role)
        await interaction.followup.send(
            f"Muted role is {role.mention}. Applied the current mute policy to {changed} channel(s)"
            + (f"; {failed} could not be updated." if failed else ".")
        )

    @muterole.command(name="settings", description="Configure what a Muted role blocks")
    @app_commands.describe(
        messages="Block sending messages",
        reactions="Block adding reactions",
        threads="Block creating public/private threads",
        connect="Block connecting to voice",
        speak="Block speaking in voice",
        stream="Block streaming in voice",
        view_channel="Hide channels entirely from muted members",
    )
    @manager_or_permission("manage_guild")
    async def muterole_settings(self, interaction: discord.Interaction, messages: bool, reactions: bool,
                                 threads: bool, connect: bool, speak: bool, stream: bool,
                                 view_channel: bool = False):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_muted_settings(
            interaction.guild.id,
            deny_send_messages=messages, deny_reactions=reactions, deny_threads=threads,
            deny_connect=connect, deny_speak=speak, deny_stream=stream, deny_view_channel=view_channel,
        )
        # Defer up front: applying the new policy sweeps every channel, which
        # can exceed Discord's 3-second interaction deadline.
        await interaction.response.defer()
        role = await self.get_or_create_muted_role(interaction.guild)
        if role is None:
            await interaction.followup.send("Settings saved, but I couldn't find/create the Muted role. Use `/muterole create` after granting Manage Roles.", ephemeral=True)
            return
        changed, failed = await self._apply_muted_role_overwrites(interaction.guild, role)
        await interaction.followup.send(
            f"Muted role settings saved and applied to {changed} channel(s)"
            + (f"; {failed} could not be updated." if failed else ".")
        )

    # Presets bundle the six granular toggles above into the three "shapes"
    # of mute people usually ask for. They just write the same settings the
    # checkboxes would - not a separate mechanism - so /muterole settings
    # (or the WebUI) still shows and can further tweak the result afterward.
    MUTE_PRESETS = {
        "visible_no_talk": dict(  # A: see everything, can join VC, can't talk anywhere
            deny_send_messages=True, deny_reactions=True, deny_threads=True,
            deny_connect=False, deny_speak=True, deny_stream=True, deny_view_channel=False,
        ),
        "visible_no_voice_no_type": dict(  # B: see everything, can't join VC, can't type
            deny_send_messages=True, deny_reactions=True, deny_threads=True,
            deny_connect=True, deny_speak=True, deny_stream=True, deny_view_channel=False,
        ),
        "fully_isolated": dict(  # C: can't see or join anything
            deny_send_messages=True, deny_reactions=True, deny_threads=True,
            deny_connect=True, deny_speak=True, deny_stream=True, deny_view_channel=True,
        ),
    }

    @muterole.command(name="preset", description="Apply one of the common Muted role presets")
    @app_commands.describe(preset="Which preset to apply")
    @app_commands.choices(preset=[
        app_commands.Choice(name="Can see channels + join VC, but can't talk anywhere", value="visible_no_talk"),
        app_commands.Choice(name="Can see channels, can't join VC or type", value="visible_no_voice_no_type"),
        app_commands.Choice(name="Fully isolated - can't see or join anything", value="fully_isolated"),
    ])
    @manager_or_permission("manage_guild")
    async def muterole_preset(self, interaction: discord.Interaction, preset: app_commands.Choice[str]):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_muted_settings(interaction.guild.id, **self.MUTE_PRESETS[preset.value])
        await interaction.response.defer()
        role = await self.get_or_create_muted_role(interaction.guild)
        if role is None:
            await interaction.followup.send("Settings saved, but I couldn't find/create the Muted role. Use `/muterole create` after granting Manage Roles.", ephemeral=True)
            return
        changed, failed = await self._apply_muted_role_overwrites(interaction.guild, role)
        await interaction.followup.send(
            f"Applied \"{preset.name}\" to {changed} channel(s)"
            + (f"; {failed} could not be updated." if failed else ".")
        )

    async def apply_role_mute(self, member: discord.Member, seconds: int, reason: str) -> tuple[bool, str]:
        """Gives `member` the guild's configured (or auto-created) Muted
        role for `seconds`, scheduling the removal. Shared by /mute and by
        AutoMod's escalation tiers so both go through the exact same
        role-lookup, hierarchy check, and scheduling logic.

        Returns (True, "") on success, or (False, human_readable_reason) on
        failure - the caller decides how to surface that (an interaction
        reply for /mute, a log line for AutoMod).
        """
        guild = member.guild
        role = await self.get_or_create_muted_role(guild)
        if role is None:
            return False, "couldn't find or create the Muted role"
        if guild.me and role.position >= guild.me.top_role.position:
            return False, "the Muted role is at or above my highest role"
        try:
            await member.add_roles(role, reason=reason)
        except discord.Forbidden:
            return False, "missing permission or role hierarchy to add the Muted role"

        run_at = int(time.time()) + seconds
        scheduler.schedule_role_unmute(self.bot.db, guild.id, run_at, member.id, role.id)
        return True, ""

    @app_commands.command(name="mute", description="Temporarily mute a member with the server's configured Muted role")
    @app_commands.describe(user="Member to mute", duration="How long, e.g. 10m, 1h, 1d", reason="Why the member is being muted")
    @manager_or_permission("moderate_members")
    async def mute(self, interaction: discord.Interaction, user: discord.Member, duration: str, reason: str = "No reason given"):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        if not can_moderate(interaction.user, user):
            await interaction.response.send_message(
                "You can't moderate someone with an equal or higher role than you.", ephemeral=True
            )
            return

        try:
            seconds = parse_duration(duration)
        except ValueError:
            await interaction.response.send_message(
                "Couldn't parse that duration. Try `10m`, `1h`, `1d`, etc.", ephemeral=True
            )
            return

        if seconds < 1 or seconds > 365 * 86400:
            await interaction.response.send_message("Mute duration must be between 1 second and 365 days.", ephemeral=True)
            return

        # Defer before touching the Muted role: on a guild's very first /mute
        # this may create the role and sweep every channel to apply the
        # configured policy, which can exceed Discord's 3-second deadline.
        # On every later /mute the role is already linked and this is a
        # cheap, single lookup - the policy stays in sync via the explicit
        # /muterole commands instead of being reapplied on every mute.
        await interaction.response.defer()

        role = await self.get_or_create_muted_role(interaction.guild)
        if role is None:
            await interaction.followup.send(
                "I can't create/find the Muted role. Grant me Manage Roles or configure an existing role with `/muterole set`.",
                ephemeral=True,
            )
            return

        if role.position >= interaction.guild.me.top_role.position:
            await interaction.followup.send(
                f"The Muted role ({role.mention}) is above my own role. Move my bot role higher first.",
                ephemeral=True,
            )
            return

        try:
            await user.add_roles(role, reason=reason)
        except discord.Forbidden:
            await interaction.followup.send(
                "I can't give that member the Muted role. Check my role is above theirs and I have Manage Roles.",
                ephemeral=True
            )
            return

        run_at = int(time.time()) + seconds
        scheduler.schedule_role_unmute(self.bot.db, interaction.guild.id, run_at, user.id, role.id)
        self.bot.db.record_member_history(interaction.guild.id, user.id, "mute", interaction.user.id, reason, f"duration_seconds={seconds}")

        await interaction.followup.send(
            f"Muted {user.mention} for {format_duration(seconds)} using {role.mention}. Reason: {reason}"
        )
        await self._log_action(interaction.guild, f"Muted ({format_duration(seconds)})", user, interaction.user, reason)

    @app_commands.command(name="unmute", description="Remove a member's Muted role")
    @app_commands.describe(user="Who to unmute")
    @manager_or_permission("moderate_members")
    async def unmute(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        cfg = self.bot.db.get_guild_config(interaction.guild.id)
        role = interaction.guild.get_role(cfg["muted_role_id"]) if cfg["muted_role_id"] else None

        if role is None or role not in user.roles:
            await interaction.response.send_message(f"{user.mention} doesn't have the Muted role.", ephemeral=True)
            return

        try:
            await user.remove_roles(role, reason=f"Unmuted by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't remove that member's Muted role - check my role is above theirs.", ephemeral=True
            )
            return

        self.bot.db.record_member_history(interaction.guild.id, user.id, "unmute", interaction.user.id, "Manual unmute")
        await interaction.response.send_message(f"Unmuted {user.mention}.")
        await self._log_action(interaction.guild, "Unmuted", user, interaction.user)

    # ---- kick ----

    @app_commands.command(name="kick", description="Kick a member")
    @app_commands.describe(user="Who to kick", reason="Reason for the kick")
    @manager_or_permission("kick_members")
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason given"):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        if not can_moderate(interaction.user, user):
            await interaction.response.send_message(
                "You can't moderate someone with an equal or higher role than you.", ephemeral=True
            )
            return

        try:
            await user.kick(reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't kick that member - check my role is above theirs.", ephemeral=True
            )
            return

        self.bot.db.record_member_history(interaction.guild.id, user.id, "kick", interaction.user.id, reason)
        await interaction.response.send_message(f"Kicked {user.mention}. Reason: {reason}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
