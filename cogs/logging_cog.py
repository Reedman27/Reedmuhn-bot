"""Server activity logging - message edits/deletes, member joins/leaves/
bans/kicks, role and channel changes, and voice activity, each routed to
its own configurable channel per category (messages / members /
moderation / server / voice), the same category-per-channel shape Carl-bot
uses. Any category left unconfigured is simply silent - nothing is logged
until an admin points it at a channel.

Where it goes further than a bare event listener: kicks and bans are
resolved against the audit log so the log shows WHO did it and WHY (not
just "member left"), gracefully falling back to a plain event if the bot
lacks View Audit Log permission - never crashes for missing permissions.
"""
import datetime
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import manager_or_permission

logger = logging.getLogger("logging_cog")

CATEGORY_CHOICES = [
    app_commands.Choice(name="Messages (edits/deletes)", value="messages"),
    app_commands.Choice(name="Members (join/leave/nick/roles)", value="members"),
    app_commands.Choice(name="Moderation (ban/unban/kick/timeout)", value="moderation"),
    app_commands.Choice(name="Server (channels/roles/emoji/settings)", value="server"),
    app_commands.Choice(name="Voice (join/leave/move)", value="voice"),
]

# How recent an audit log entry has to be to count as "this is what caused
# the event we just saw" - long enough to allow for normal API latency,
# short enough that an unrelated older kick doesn't get misattributed to a
# different member leaving around the same time.
_AUDIT_LOG_MATCH_WINDOW_SECONDS = 5

_COLOR_ADD = discord.Color.green()
_COLOR_REMOVE = discord.Color.red()
_COLOR_EDIT = discord.Color.orange()
_COLOR_NEUTRAL = discord.Color.blurple()


def _truncate(text: str, limit: int = 1024) -> str:
    if text is None:
        return "*(none)*"
    text = text if text.strip() else "*(empty)*"
    return text if len(text) <= limit else text[: limit - 1] + "…"


class LoggingCog(commands.Cog, name="Logging"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- configuration commands ----

    logging_group = app_commands.Group(name="logging", description="Configure server activity logging")

    @logging_group.command(name="channel", description="Send a category of logs to a channel")
    @app_commands.describe(category="Which kind of event to log", channel="Where to send that category's logs")
    @app_commands.choices(category=CATEGORY_CHOICES)
    @manager_or_permission("manage_guild")
    async def logging_channel(
        self, interaction: discord.Interaction, category: app_commands.Choice[str], channel: discord.TextChannel
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_log_channel(interaction.guild.id, category.value, channel.id)
        await interaction.response.send_message(f"**{category.name}** logs will now post in {channel.mention}.")

    @logging_group.command(name="disable", description="Stop logging a category")
    @app_commands.describe(category="Which category to turn off")
    @app_commands.choices(category=CATEGORY_CHOICES)
    @manager_or_permission("manage_guild")
    async def logging_disable(self, interaction: discord.Interaction, category: app_commands.Choice[str]):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.disable_log_category(interaction.guild.id, category.value)
        await interaction.response.send_message(f"**{category.name}** logging is now off.")

    @logging_group.command(name="status", description="Show current logging configuration")
    @manager_or_permission("manage_guild")
    async def logging_status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        configured = self.bot.db.get_all_log_channels(interaction.guild.id)
        lines = []
        for choice in CATEGORY_CHOICES:
            channel_id = configured.get(choice.value)
            status = f"<#{channel_id}>" if channel_id else "*off*"
            lines.append(f"**{choice.name}** - {status}")
        ignored = self.bot.db.list_ignored_log_channels(interaction.guild.id)
        if ignored:
            lines.append("")
            lines.append("Ignored channels: " + ", ".join(f"<#{c}>" for c in ignored))
        embed = discord.Embed(title="Logging configuration", description="\n".join(lines), color=_COLOR_NEUTRAL)
        await interaction.response.send_message(embed=embed)

    @logging_group.command(name="ignore", description="Stop message-log events from a specific channel")
    @app_commands.describe(channel="Channel to ignore for message edit/delete logging")
    @manager_or_permission("manage_guild")
    async def logging_ignore(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.add_ignored_log_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"{channel.mention} will no longer show up in message logs.")

    @logging_group.command(name="unignore", description="Resume message-log events from a channel")
    @app_commands.describe(channel="Channel to stop ignoring")
    @manager_or_permission("manage_guild")
    async def logging_unignore(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        removed = self.bot.db.remove_ignored_log_channel(interaction.guild.id, channel.id)
        msg = f"{channel.mention} is no longer ignored." if removed else f"{channel.mention} wasn't ignored."
        await interaction.response.send_message(msg)

    # ---- dispatch helper ----

    async def log_event(self, guild: discord.Guild, category: str, embed: discord.Embed) -> None:
        """Public entry point for other cogs to log actions that have no
        native Discord event to hook - warns and role-based mutes/unmutes
        don't fire anything discord.py can listen for on their own, unlike
        a real kick/ban which shows up via on_member_remove/on_member_ban
        and gets resolved through the audit log already."""
        await self._log(guild, category, embed)

    async def _log(
        self, guild: discord.Guild, category: str, embed: discord.Embed, file: discord.File | None = None
    ) -> None:
        channel_id = self.bot.db.get_log_channel(guild.id, category)
        if channel_id is None:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return  # channel was deleted - admin needs to reconfigure, nothing to do here
        try:
            if file is not None:
                await channel.send(embed=embed, file=file)
            else:
                await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning("missing permission to send logs in guild %s channel %s", guild.id, channel_id)
        except discord.HTTPException:
            logger.warning("failed to send log message in guild %s channel %s", guild.id, channel_id)

    async def _find_audit_actor(
        self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int
    ) -> discord.AuditLogEntry | None:
        """Looks for a recent audit log entry matching this action/target,
        so a kick/ban can be attributed to a moderator + reason instead of
        just showing up as a bare member-remove event. Returns None (not an
        exception) if the bot lacks View Audit Log permission, or if
        nothing matching turns up in time - callers should treat that as
        "fall back to the plain event", not an error."""
        try:
            async for entry in guild.audit_logs(action=action, limit=5):
                if entry.target is None or entry.target.id != target_id:
                    continue
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                if age <= _AUDIT_LOG_MATCH_WINDOW_SECONDS:
                    return entry
        except discord.Forbidden:
            return None
        except discord.HTTPException:
            return None
        return None

    # ---- messages ----

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.bot:
            return
        if before.content == after.content:
            return  # embed-load edits etc. - nothing the author actually changed
        if self.bot.db.is_log_channel_ignored(before.guild.id, before.channel.id):
            return
        embed = discord.Embed(
            description=f"**Message edited in {before.channel.mention}** [Jump to message]({after.jump_url})",
            color=_COLOR_EDIT,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
        embed.add_field(name="Before", value=_truncate(before.content), inline=False)
        embed.add_field(name="After", value=_truncate(after.content), inline=False)
        embed.set_footer(text=f"User ID: {before.author.id}")
        await self._log(before.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if self.bot.db.is_log_channel_ignored(message.guild.id, message.channel.id):
            return
        embed = discord.Embed(
            description=f"**Message deleted in {message.channel.mention}**",
            color=_COLOR_REMOVE,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        embed.add_field(name="Content", value=_truncate(message.content), inline=False)
        if message.attachments:
            # link only, not a re-uploaded copy - just enough to see what it was
            links = "\n".join(f"{a.filename}: {a.url}" for a in message.attachments)
            embed.add_field(name="Attachments", value=_truncate(links, 1024), inline=False)
        embed.set_footer(text=f"User ID: {message.author.id}")
        await self._log(message.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        """Purges are noisy - dumping every message inline would blow past
        embed limits fast. Instead we render a transcript: raw content plus
        attachment URLs (a pasted gif/tenor link is already part of
        message.content, so it survives here as plain text - not a re-hosted
        embed, just the link) for every message, attach the full transcript
        as a .txt so nothing purged is actually lost, and show only the most
        recent lines inline for a quick skim.
        """
        if not messages or messages[0].guild is None:
            return
        guild = messages[0].guild
        channel = messages[0].channel
        if self.bot.db.is_log_channel_ignored(guild.id, channel.id):
            return

        # oldest first - matches the order the messages were actually posted in
        ordered = sorted(messages, key=lambda m: m.created_at)

        lines = []
        for m in ordered:
            content = m.content.strip() if m.content else ""
            if m.attachments:
                urls = " ".join(a.url for a in m.attachments)
                content = f"{content} {urls}".strip() if content else urls
            if not content:
                content = "*(no text content)*"
            lines.append(f"[{m.author}]: {content}")

        full_transcript = "\n".join(lines)

        # keep the inline preview inside embed description limits; trim from
        # the front until what's left fits, then note how much is showing
        preview_lines = lines[-40:]
        preview = "\n".join(preview_lines)
        while len(preview) > 3500 and len(preview_lines) > 1:
            preview_lines.pop(0)
            preview = "\n".join(preview_lines)

        description = f"**{len(messages)} messages purged in {channel.mention}**\n{preview}"
        if len(preview_lines) < len(lines):
            description += f"\n\n*{len(preview_lines)} latest shown*"

        embed = discord.Embed(
            description=_truncate(description, 4096),
            color=_COLOR_REMOVE,
            timestamp=discord.utils.utcnow(),
        )

        filename = f"purged-{channel.id}-{int(discord.utils.utcnow().timestamp())}.txt"
        file = discord.File(io.BytesIO(full_transcript.encode("utf-8")), filename=filename)
        await self._log(guild, "messages", embed, file=file)

    # ---- members ----

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        account_age = discord.utils.utcnow() - member.created_at
        embed = discord.Embed(
            description=f"**{member.mention} joined**",
            color=_COLOR_ADD,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        age_note = f"{account_age.days} days ago"
        if account_age.days < 7:
            age_note += " ⚠️ new account"
        embed.add_field(name="Account created", value=age_note, inline=False)
        embed.set_footer(text=f"User ID: {member.id}")
        await self._log(member.guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        entry = await self._find_audit_actor(member.guild, discord.AuditLogAction.kick, member.id)
        if entry is not None:
            embed = discord.Embed(
                description=f"**{member.mention} was kicked**",
                color=_COLOR_REMOVE,
                timestamp=discord.utils.utcnow(),
            )
            embed.set_author(name=str(member), icon_url=member.display_avatar.url)
            embed.add_field(name="Moderator", value=str(entry.user), inline=True)
            embed.add_field(name="Reason", value=_truncate(entry.reason, 512), inline=True)
            embed.set_footer(text=f"User ID: {member.id}")
            await self._log(member.guild, "moderation", embed)
            return

        embed = discord.Embed(
            description=f"**{member.mention} left**",
            color=_COLOR_REMOVE,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_footer(text=f"User ID: {member.id}")
        await self._log(member.guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        entry = await self._find_audit_actor(guild, discord.AuditLogAction.ban, user.id)
        embed = discord.Embed(
            description=f"**{user.mention} was banned**", color=_COLOR_REMOVE, timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        if entry is not None:
            embed.add_field(name="Moderator", value=str(entry.user), inline=True)
            embed.add_field(name="Reason", value=_truncate(entry.reason, 512), inline=True)
        embed.set_footer(text=f"User ID: {user.id}")
        await self._log(guild, "moderation", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        entry = await self._find_audit_actor(guild, discord.AuditLogAction.unban, user.id)
        embed = discord.Embed(
            description=f"**{user.mention} was unbanned**", color=_COLOR_ADD, timestamp=discord.utils.utcnow()
        )
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        if entry is not None:
            embed.add_field(name="Moderator", value=str(entry.user), inline=True)
        embed.set_footer(text=f"User ID: {user.id}")
        await self._log(guild, "moderation", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            embed = discord.Embed(
                description=f"**{after.mention}'s nickname changed**", color=_COLOR_EDIT, timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Before", value=_truncate(before.nick or before.name, 256), inline=True)
            embed.add_field(name="After", value=_truncate(after.nick or after.name, 256), inline=True)
            embed.set_footer(text=f"User ID: {after.id}")
            await self._log(after.guild, "members", embed)

        before_roles, after_roles = set(before.roles), set(after.roles)
        added = after_roles - before_roles
        removed = before_roles - after_roles
        if added or removed:
            embed = discord.Embed(
                description=f"**{after.mention}'s roles changed**", color=_COLOR_EDIT, timestamp=discord.utils.utcnow()
            )
            if added:
                embed.add_field(name="Added", value=", ".join(r.mention for r in added), inline=False)
            if removed:
                embed.add_field(name="Removed", value=", ".join(r.mention for r in removed), inline=False)
            embed.set_footer(text=f"User ID: {after.id}")
            await self._log(after.guild, "members", embed)

        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until and (
                before.timed_out_until is None or after.timed_out_until > discord.utils.utcnow()
            ):
                embed = discord.Embed(
                    description=f"**{after.mention} was timed out**", color=_COLOR_REMOVE, timestamp=discord.utils.utcnow()
                )
                embed.add_field(name="Until", value=discord.utils.format_dt(after.timed_out_until, "F"), inline=False)
            else:
                embed = discord.Embed(
                    description=f"**{after.mention}'s timeout was removed**",
                    color=_COLOR_ADD,
                    timestamp=discord.utils.utcnow(),
                )
            embed.set_footer(text=f"User ID: {after.id}")
            await self._log(after.guild, "moderation", embed)

    # ---- server structure ----

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(
            description=f"**Channel created:** {channel.mention if hasattr(channel, 'mention') else channel.name}",
            color=_COLOR_ADD,
            timestamp=discord.utils.utcnow(),
        )
        await self._log(channel.guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(
            description=f"**Channel deleted:** #{channel.name}", color=_COLOR_REMOVE, timestamp=discord.utils.utcnow()
        )
        await self._log(channel.guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        changes = []
        if before.name != after.name:
            changes.append(f"name: `{before.name}` → `{after.name}`")
        if isinstance(before, discord.TextChannel) and isinstance(after, discord.TextChannel):
            if before.topic != after.topic:
                changes.append("topic changed")
            if before.slowmode_delay != after.slowmode_delay:
                changes.append(f"slowmode: {before.slowmode_delay}s → {after.slowmode_delay}s")
        if not changes:
            return
        embed = discord.Embed(
            description=f"**#{after.name} updated**\n" + "\n".join(changes),
            color=_COLOR_EDIT,
            timestamp=discord.utils.utcnow(),
        )
        await self._log(after.guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        embed = discord.Embed(
            description=f"**Role created:** {role.mention}", color=_COLOR_ADD, timestamp=discord.utils.utcnow()
        )
        await self._log(role.guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        embed = discord.Embed(
            description=f"**Role deleted:** {role.name}", color=_COLOR_REMOVE, timestamp=discord.utils.utcnow()
        )
        await self._log(role.guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name != after.name:
            changes.append(f"name: `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"color: {before.color} → {after.color}")
        if before.permissions != after.permissions:
            changes.append("permissions changed")
        if not changes:
            return
        embed = discord.Embed(
            description=f"**Role {after.mention} updated**\n" + "\n".join(changes),
            color=_COLOR_EDIT,
            timestamp=discord.utils.utcnow(),
        )
        await self._log(after.guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes = []
        if before.name != after.name:
            changes.append(f"name: `{before.name}` → `{after.name}`")
        if before.icon != after.icon:
            changes.append("icon changed")
        if not changes:
            return
        embed = discord.Embed(
            description="**Server settings updated**\n" + "\n".join(changes),
            color=_COLOR_EDIT,
            timestamp=discord.utils.utcnow(),
        )
        await self._log(after, "server", embed)

    # ---- voice ----

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        if before.channel == after.channel:
            return  # a mute/deafen/stream toggle with no channel change - not logged, too noisy
        if before.channel is None:
            embed = discord.Embed(
                description=f"**{member.mention} joined voice** {after.channel.mention}",
                color=_COLOR_ADD, timestamp=discord.utils.utcnow(),
            )
        elif after.channel is None:
            embed = discord.Embed(
                description=f"**{member.mention} left voice** {before.channel.mention}",
                color=_COLOR_REMOVE, timestamp=discord.utils.utcnow(),
            )
        else:
            embed = discord.Embed(
                description=f"**{member.mention} moved voice channels**\n{before.channel.mention} → {after.channel.mention}",
                color=_COLOR_EDIT, timestamp=discord.utils.utcnow(),
            )
        await self._log(member.guild, "voice", embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))
