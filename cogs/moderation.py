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

    # ---- mute / unmute (Discord's native timeout, not a role) ----

    @app_commands.command(name="mute", description="Timeout a member")
    @app_commands.describe(user="Who to mute", duration="e.g. 10m, 1h, 1d (max 28d, Discord's own limit)", reason="Reason for the mute")
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
                "Couldn't parse that duration. Try something like `10m`, `1h`, `1d`.", ephemeral=True
            )
            return

        if seconds > 28 * 86400:
            await interaction.response.send_message("Discord's timeout limit is 28 days max.", ephemeral=True)
            return

        try:
            await user.timeout(discord.utils.utcnow() + datetime.timedelta(seconds=seconds), reason=reason)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't timeout that member - check my role is above theirs.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Muted {user.mention} for {format_duration(seconds)}. Reason: {reason}"
        )

    @app_commands.command(name="unmute", description="Remove a member's timeout")
    @app_commands.describe(user="Who to unmute")
    @manager_or_permission("moderate_members")
    async def unmute(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        try:
            await user.timeout(None, reason=f"Unmuted by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I can't remove that member's timeout - check my role is above theirs.", ephemeral=True
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
