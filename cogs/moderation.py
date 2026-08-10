import asyncio
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

    # ---- mute / unmute ----
    # Role-based mute with a server-configurable role and permission profile.
    # The same configuration is available from the WebUI and Discord.

    muterole = app_commands.Group(name="muterole", description="Configure the server's muted role")

    async def _get_or_create_muted_role(self, guild: discord.Guild) -> discord.Role | None:
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
        # Defer up front: _get_or_create_muted_role may need to create the role
        # and sweep every channel, which can exceed Discord's 3-second deadline.
        await interaction.response.defer()
        role = await self._get_or_create_muted_role(interaction.guild)
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
    )
    @manager_or_permission("manage_guild")
    async def muterole_settings(self, interaction: discord.Interaction, messages: bool, reactions: bool,
                                 threads: bool, connect: bool, speak: bool, stream: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_muted_settings(
            interaction.guild.id,
            deny_send_messages=messages, deny_reactions=reactions, deny_threads=threads,
            deny_connect=connect, deny_speak=speak, deny_stream=stream,
        )
        # Defer up front: applying the new policy sweeps every channel, which
        # can exceed Discord's 3-second interaction deadline.
        await interaction.response.defer()
        role = await self._get_or_create_muted_role(interaction.guild)
        if role is None:
            await interaction.followup.send("Settings saved, but I couldn't find/create the Muted role. Use `/muterole create` after granting Manage Roles.", ephemeral=True)
            return
        changed, failed = await self._apply_muted_role_overwrites(interaction.guild, role)
        await interaction.followup.send(
            f"Muted role settings saved and applied to {changed} channel(s)"
            + (f"; {failed} could not be updated." if failed else ".")
        )

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

        role = await self._get_or_create_muted_role(interaction.guild)
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

        await interaction.followup.send(
            f"Muted {user.mention} for {format_duration(seconds)} using {role.mention}. Reason: {reason}"
        )

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

        await interaction.response.send_message(f"Unmuted {user.mention}.")

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

        await interaction.response.send_message(f"Kicked {user.mention}. Reason: {reason}")

    # ---- purge ----
    # Ported/adapted from the chunked bulk-delete approach used by
    # Red-DiscordBot's moderation utilities. This keeps the implementation
    # native to discord.py rather than importing Red's framework.

    @app_commands.command(name="purge", description="Delete recent messages in this channel")
    @app_commands.describe(count="How many messages to delete (max 500)")
    @manager_or_permission("manage_messages")
    async def purge(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 500]):
        if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("This only works in a server text channel.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        remaining = count
        deleted_total = 0
        while remaining:
            batch = min(remaining, 100)
            deleted = await interaction.channel.purge(limit=batch)
            deleted_total += len(deleted)
            remaining -= batch
            if len(deleted) < batch:
                break
            if remaining:
                await asyncio.sleep(1.5)

        await interaction.followup.send(
            f"Deleted {deleted_total} message{'s' if deleted_total != 1 else ''}.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
