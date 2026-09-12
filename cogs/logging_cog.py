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
import asyncio
import datetime
import io
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import manager_or_permission

logger = logging.getLogger("logging_cog")

CATEGORY_CHOICES = [
    app_commands.Choice(name="Messages (edits/deletes)", value="messages"),
    app_commands.Choice(name="Members (join/leave/nick/roles)", value="members"),
    app_commands.Choice(name="Moderation (ban/unban/kick/timeout)", value="moderation"),
    app_commands.Choice(name="Server (channels/roles/emoji/settings)", value="server"),
    app_commands.Choice(name="Voice (join/leave/move)", value="voice"),
    app_commands.Choice(name="Automod (filter actions)", value="automod"),
    app_commands.Choice(name="Tickets (opened/closed)", value="tickets"),
    app_commands.Choice(name="Reports (filed/resolved/dismissed)", value="reports"),
]

# Channel name used for each category's "Automatic - create/reuse" option
# (both the WebUI's Automatic dropdown choice and /logging setup) - named
# after what actually gets logged there ("member-logs", not a generic
# shared "bot-logs") so a server with several categories set to Automatic
# ends up with clearly-labelled channels instead of everything piling into
# one. An existing channel with the matching name is reused (same
# find-by-name-first approach as /muterole's auto-created "Muted" role) so
# clicking Automatic twice for the same category doesn't spawn a duplicate.
CATEGORY_LOG_CHANNEL_NAMES = {
    "messages": "message-logs",
    "members": "member-logs",
    "moderation": "moderation-logs",
    "server": "server-logs",
    "voice": "voice-logs",
    "automod": "automod-logs",
    "tickets": "ticket-logs",
    "reports": "report-logs",
}

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
        bot.register_webui_wake("log_channel_create", self.poll_log_channel_create_requests)
        self.poll_log_channel_create_requests.start()

    def cog_unload(self):
        self.poll_log_channel_create_requests.cancel()

    # ---- WebUI -> Discord: create/reuse a category's auto channel ----
    # The dashboard is a separate process with no bot token, so it can only
    # queue the request in SQLite; this loop is what actually talks to
    # Discord. Same queue/claim/complete shape as the muted-role sync in
    # cogs/dashboardmoderation.py.

    async def get_or_create_log_channel(self, guild: discord.Guild, category: str) -> discord.TextChannel | None:
        """Return the private text channel for this category's auto-created
        logs (e.g. "member-logs" for the members category), creating it if
        needed. An existing channel with the matching name is reused so
        clicking Automatic again for the same category doesn't spawn a
        duplicate - the same way /muterole reuses an existing "Muted"-named
        role instead of making a second one."""
        channel_name = CATEGORY_LOG_CHANNEL_NAMES.get(category, f"{category}-logs")
        existing = discord.utils.find(lambda c: isinstance(c, discord.TextChannel) and c.name == channel_name, guild.channels)
        if existing is not None:
            return existing

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        if guild.me is not None:
            overwrites[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True)
        try:
            return await guild.create_text_channel(
                channel_name,
                overwrites=overwrites,
                reason="Auto-created by WebUI logging setup",
            )
        except discord.Forbidden:
            return None
        except discord.HTTPException:
            return None

    @tasks.loop(seconds=2)
    async def poll_log_channel_create_requests(self):
        try:
            requests = self.bot.db.claim_log_channel_create_requests(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI log-channel create requests")
            return
        for request_id, guild_id, category, _reason in requests:
            try:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    self.bot.db.complete_log_channel_create(request_id, "The bot is no longer in that server.")
                    continue
                channel = await self.get_or_create_log_channel(guild, category)
                if channel is None:
                    self.bot.db.complete_log_channel_create(request_id, "Couldn't find or create the channel - the bot needs Manage Channels.")
                    continue
                me = guild.me
                if me is not None and not channel.permissions_for(me).send_messages:
                    self.bot.db.complete_log_channel_create(request_id, f"Created/found #{channel.name}, but the bot can't send messages there - check its permissions.", channel_id=channel.id)
                    continue
                self.bot.db.set_log_channel(guild_id, category, channel.id)
                self.bot.db.complete_log_channel_create(request_id, channel_id=channel.id)
                logger.info("WebUI log-channel create %s pointed %s logs at channel %s in guild %s", request_id, category, channel.id, guild_id)
            except Exception as exc:
                logger.exception("WebUI log-channel create %s failed", request_id)
                self.bot.db.complete_log_channel_create(request_id, str(exc)[:500])

    @poll_log_channel_create_requests.before_loop
    async def before_poll_log_channel_create(self):
        await self.bot.wait_until_ready()

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

    @logging_group.command(name="setup", description="Auto-create any missing log channels and wire them up")
    @app_commands.describe(
        under="Optional category to create the new channels under",
        overwrite="Also recreate channels for categories that already point at a channel that still exists",
    )
    @manager_or_permission("manage_guild")
    async def logging_setup(
        self,
        interaction: discord.Interaction,
        under: discord.CategoryChannel = None,
        overwrite: bool = False,
    ):
        """One-shot setup for people who don't want to hand-create seven
        channels and run /logging channel seven times: creates a private
        #<category>-logs channel for anything not already configured (or
        whose configured channel got deleted), and points that category at
        it automatically."""
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        guild = interaction.guild
        me = guild.me
        if me is not None and not guild.me.guild_permissions.manage_channels:
            await interaction.response.send_message("I need Manage Channels to create log channels.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        existing = self.bot.db.get_all_log_channels(guild.id)
        manager_role_ids = self.bot.db.list_bot_manager_roles(guild.id)

        # Private by default - deny @everyone, explicitly allow whatever
        # roles are configured as bot managers plus the bot itself. Server
        # admins can already see it regardless (Administrator bypasses
        # channel overwrites), so they don't need an explicit entry.
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        for role_id in manager_role_ids:
            role = guild.get_role(role_id)
            if role is not None:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, read_message_history=True, send_messages=False)
        if me is not None:
            overwrites[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        created, skipped, failed = [], [], []
        for choice in CATEGORY_CHOICES:
            key = choice.value
            current_id = existing.get(key)
            if current_id and guild.get_channel(current_id) is not None and not overwrite:
                skipped.append(choice.name)
                continue
            try:
                channel = await guild.create_text_channel(
                    name=CATEGORY_LOG_CHANNEL_NAMES.get(key, f"{key}-logs"), category=under, overwrites=overwrites,
                    reason=f"Automatic log channel setup by {interaction.user} (/logging setup)",
                )
            except discord.Forbidden:
                failed.append(f"{choice.name} - missing permission")
                continue
            except discord.HTTPException as exc:
                failed.append(f"{choice.name} - {exc}")
                continue
            self.bot.db.set_log_channel(guild.id, key, channel.id)
            created.append(f"**{choice.name}** → {channel.mention}")

        lines = []
        if created:
            lines.append("Created and wired up:\n" + "\n".join(created))
        if skipped:
            lines.append("Already configured (left alone): " + ", ".join(skipped))
        if failed:
            lines.append("Failed:\n" + "\n".join(failed))
        await interaction.followup.send("\n\n".join(lines) or "Nothing to do.", ephemeral=True)

    @logging_group.command(name="import", description="Backfill a log channel with existing history for one category")
    @app_commands.describe(
        category="Which kind of history to import",
        channel="Where to post it (defaults to that category's configured log channel)",
        limit="Max number of past records to import (default 25, max 100)",
    )
    @app_commands.choices(category=CATEGORY_CHOICES)
    @manager_or_permission("manage_guild")
    async def logging_import(
        self,
        interaction: discord.Interaction,
        category: app_commands.Choice[str],
        channel: discord.TextChannel = None,
        limit: int = 25,
    ):
        """Not everything the bot logs going forward has history behind
        it - message content and server-config changes were never stored
        anywhere, so there's nothing to backfill for those. Moderation
        cases, tickets, reports, and join/leave/voice activity DO have a
        durable record already (member_history / tickets / reports /
        bot_events), so this pulls from whichever table actually backs the
        chosen category and posts it into the log channel as regular
        embeds, oldest first - handy right after pointing a category at a
        channel for the first time, so it isn't empty."""
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        guild = interaction.guild
        cat = category.value
        limit = max(1, min(int(limit), 100))

        target = channel
        if target is None:
            configured_id = self.bot.db.get_log_channel(guild.id, cat)
            target = guild.get_channel(configured_id) if configured_id else None
        if target is None:
            await interaction.response.send_message(
                f"**{category.name}** has no log channel configured, and you didn't pick one. "
                f"Run `/logging channel` first or pass a `channel` here.",
                ephemeral=True,
            )
            return
        me = guild.me
        if me is not None and not target.permissions_for(me).send_messages:
            await interaction.response.send_message(f"I can't send messages in {target.mention} - check my permissions there.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        embeds = self._build_import_embeds(guild, cat, limit)
        if embeds is None:
            await interaction.followup.send(
                f"**{category.name}** doesn't have any stored history to import - the bot only started keeping "
                f"a durable record for categories like moderation, tickets, reports, members, and voice. "
                f"New {category.name.lower()} events will still log normally from now on.",
                ephemeral=True,
            )
            return
        if not embeds:
            await interaction.followup.send(f"No past **{category.name}** records found to import.", ephemeral=True)
            return

        posted = 0
        for embed in embeds:
            try:
                await target.send(embed=embed)
                posted += 1
            except discord.Forbidden:
                await interaction.followup.send(f"Lost permission to post in {target.mention} partway through - stopped after {posted}.", ephemeral=True)
                return
            except discord.HTTPException:
                continue
            await asyncio.sleep(0.5)  # stay well clear of the channel-send rate limit for a batch like this

        await interaction.followup.send(f"Imported {posted} past **{category.name}** record(s) into {target.mention}.", ephemeral=True)

    def _build_import_embeds(self, guild: discord.Guild, category: str, limit: int) -> list[discord.Embed] | None:
        """Returns embeds oldest-first for the given category, or None if
        that category has no durable history to backfill from at all
        (as opposed to an empty list, which means the source exists but is
        currently empty)."""
        if category == "moderation":
            rows = list(reversed(self.bot.db.list_recent_cases(guild.id, limit)))
            embeds = []
            for case_number, user_id, event_type, actor_id, reason, created_at, voided in rows:
                title = f"Case #{case_number} - {event_type}" + (" (voided)" if voided else "")
                embed = discord.Embed(title=title, color=_COLOR_NEUTRAL, timestamp=datetime.datetime.fromtimestamp(created_at, tz=datetime.timezone.utc))
                embed.add_field(name="User", value=f"<@{user_id}> (`{user_id}`)", inline=True)
                embed.add_field(name="Moderator", value=f"<@{actor_id}>" if actor_id else "*unknown*", inline=True)
                embed.add_field(name="Reason", value=_truncate(reason or "*no reason given*", 512), inline=False)
                embed.set_footer(text="Imported from existing case history")
                embeds.append(embed)
            return embeds

        if category == "tickets":
            rows = list(reversed(self.bot.db.list_tickets(guild.id, limit)))
            embeds = []
            for ticket_id, channel_id, opener_id, subject, status, created_at, closed_at, closed_by, close_reason in rows:
                embed = discord.Embed(
                    title=f"Ticket #{ticket_id} - {status}",
                    color=_COLOR_NEUTRAL,
                    timestamp=datetime.datetime.fromtimestamp(created_at, tz=datetime.timezone.utc),
                )
                embed.add_field(name="Opened by", value=f"<@{opener_id}>", inline=True)
                embed.add_field(name="Subject", value=_truncate(subject or "*none*", 256), inline=False)
                if status != "open":
                    embed.add_field(name="Closed by", value=f"<@{closed_by}>" if closed_by else "*unknown*", inline=True)
                    if close_reason:
                        embed.add_field(name="Close reason", value=_truncate(close_reason, 256), inline=True)
                embed.set_footer(text="Imported from existing ticket history")
                embeds.append(embed)
            return embeds

        if category == "reports":
            rows = list(reversed(self.bot.db.list_reports(guild.id, limit=limit)))
            embeds = []
            for r in rows:
                embed = discord.Embed(
                    title=f"Report #{r['id']} - {r['status']}",
                    color=_COLOR_NEUTRAL,
                    timestamp=datetime.datetime.fromtimestamp(r["created_at"], tz=datetime.timezone.utc),
                )
                embed.add_field(name="Reporter", value=f"<@{r['reporter_id']}>", inline=True)
                embed.add_field(name="Target", value=f"<@{r['target_user_id']}>", inline=True)
                embed.add_field(name="Reason", value=_truncate(r["reason"] or "*no reason given*", 512), inline=False)
                if r["resolved_by"]:
                    embed.add_field(name="Resolved by", value=f"<@{r['resolved_by']}>", inline=True)
                    if r["resolution_note"]:
                        embed.add_field(name="Resolution", value=_truncate(r["resolution_note"], 256), inline=True)
                embed.set_footer(text="Imported from existing report history")
                embeds.append(embed)
            return embeds

        if category == "automod":
            rows = list(reversed(self.bot.db.list_automod_violations(guild.id, limit)))
            embeds = []
            for user_id, reason, created_at in rows:
                embed = discord.Embed(
                    description=f"**AutoMod violation:** <@{user_id}>",
                    color=_COLOR_REMOVE,
                    timestamp=datetime.datetime.fromtimestamp(created_at, tz=datetime.timezone.utc),
                )
                embed.add_field(name="Reason", value=_truncate(reason or "*no reason recorded*", 512), inline=False)
                embed.set_footer(text=f"Imported from existing automod history - User ID: {user_id}")
                embeds.append(embed)
            return embeds

        # members / voice / messages fall back to the generic durable
        # bot_events audit trail - it doesn't carry message content or full
        # embeds, just who/where/when, so the import is a plain summary line
        # rather than a re-creation of the original log embed.
        event_types_by_category = {
            "members": ("member.join", "member.leave"),
            "voice": ("voice.join", "voice.leave"),
            "messages": ("message.edited", "message.deleted"),
        }
        event_types = event_types_by_category.get(category)
        if event_types is None:
            return None  # server: no durable history stored for this category at all

        combined: list[tuple[int, str, int | None, int | None, str | None]] = []
        for event_type in event_types:
            for created_at, actor_id, target_id, details in self.bot.db.list_recent_events(guild.id, 0, event_type, limit):
                combined.append((created_at, event_type, actor_id, target_id, details))
        if not combined:
            return []
        combined.sort(key=lambda row: row[0])
        combined = combined[-limit:]

        _labels = {
            "member.join": ("Member joined", _COLOR_ADD),
            "member.leave": ("Member left", _COLOR_REMOVE),
            "voice.join": ("Joined voice", _COLOR_ADD),
            "voice.leave": ("Left voice", _COLOR_REMOVE),
            "message.edited": ("Message edited", _COLOR_EDIT),
            "message.deleted": ("Message deleted", _COLOR_REMOVE),
        }
        embeds = []
        for created_at, event_type, actor_id, target_id, details in combined:
            label, color = _labels.get(event_type, (event_type, _COLOR_NEUTRAL))
            channel_id = None
            if details:
                try:
                    channel_id = json.loads(details).get("channel_id")
                except (ValueError, AttributeError):
                    channel_id = None
            description = f"**{label}**"
            if actor_id:
                description += f" - <@{actor_id}>"
            if channel_id:
                description += f" in <#{channel_id}>"
            embed = discord.Embed(description=description, color=color, timestamp=datetime.datetime.fromtimestamp(created_at, tz=datetime.timezone.utc))
            embed.set_footer(text="Imported from existing activity history - no message content is stored")
            embeds.append(embed)
        return embeds

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
        self, guild: discord.Guild, action: discord.AuditLogAction, target_id: int,
        channel_id: int | None = None, changed_attr: str | None = None,
    ) -> discord.AuditLogEntry | None:
        """Looks for a recent audit log entry matching this action/target,
        so an action can be attributed to a moderator + reason instead of
        just showing up as a bare event. Returns None (not an exception) if
        the bot lacks View Audit Log permission, or if nothing matching
        turns up in time - callers should treat that as "fall back to the
        plain event", not an error.

        Entries whose actor is this bot are also treated as no-match: every
        bot-driven action (a slash command, AutoMod, a dashboard-queued mod
        action) already logs its own dedicated embed with the real
        moderator/reason at the point it happens, so re-surfacing "the bot
        did it" here would just be a confusing, less-specific duplicate.
        This is purely about filling the gap for actions taken straight
        from Discord's native UI, which otherwise show no actor at all.

        `channel_id`, when given, additionally requires the entry's
        `extra.channel` to match - needed for message-delete actions, whose
        target is the message's author (a moderator could plausibly delete
        that same person's message in two different channels within the
        match window, so the author id alone isn't a precise enough match).

        `changed_attr`, when given, additionally requires the entry's
        `after` diff to actually include that attribute - needed for
        AuditLogAction.member_update, which covers nickname changes AND
        timeouts (and more) with no distinction in the `action` value
        itself. Without this, a mod renaming and timing out the same member
        within the match window could get the nickname-change log
        attributed using the timeout's audit entry, or vice versa.
        """
        try:
            async for entry in guild.audit_logs(action=action, limit=5):
                if entry.target is None or entry.target.id != target_id:
                    continue
                if entry.user is not None and self.bot.user is not None and entry.user.id == self.bot.user.id:
                    continue
                if channel_id is not None:
                    entry_channel = getattr(entry.extra, "channel", None)
                    if entry_channel is None or entry_channel.id != channel_id:
                        continue
                if changed_attr is not None and not hasattr(entry.after, changed_attr):
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
    async def on_message(self, message: discord.Message):
        # Analytics/audit metadata only: no message content is persisted here.
        if message.guild is None or message.author.bot:
            return
        self.bot.db.record_bot_event(
            "message.received",
            message.guild.id,
            message.author.id,
            message.id,
            {"channel_id": message.channel.id},
            source="discord_event",
            status="success",
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.bot:
            return
        if before.content == after.content:
            return  # embed-load edits etc. - nothing the author actually changed
        self.bot.db.record_bot_event(
            "message.edited",
            before.guild.id,
            before.author.id,
            before.id,
            {"channel_id": before.channel.id},
            source="discord_event",
            status="success",
        )
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
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Handles every message delete, not just ones discord.py happened
        to have cached. on_message_delete (the non-raw event) silently
        never fires for anything evicted from the bot's local message
        cache - which, on a bot process shared across every channel in
        every guild, is most messages more than a few minutes old. This is
        the actual fix for deletions going unlogged: the raw event always
        fires, we just get less detail (no content/author) when the
        message wasn't cached.
        """
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        if self.bot.db.is_log_channel_ignored(guild.id, payload.channel_id):
            return

        message = payload.cached_message
        if message is not None:
            # Was cached - full detail path, same as before.
            if message.author.bot:
                return
            await self._log_deleted_message(message)
            return

        # Not cached: we only know the channel + message ID, not who wrote
        # it or what it said. Still worth a log entry - "something was
        # deleted here" beats total silence - just without the content.
        self.bot.db.record_bot_event(
            "message.deleted",
            guild.id,
            None,
            payload.message_id,
            {"channel_id": payload.channel_id},
            source="discord_event",
            status="success",
        )
        channel = guild.get_channel(payload.channel_id)
        channel_mention = channel.mention if channel is not None else f"<#{payload.channel_id}>"
        embed = discord.Embed(
            description=(
                f"**Message deleted in {channel_mention}**\n"
                "*(content unavailable - message wasn't in the bot's cache)*"
            ),
            color=_COLOR_REMOVE,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Message ID: {payload.message_id}")
        await self._log(guild, "messages", embed)

    async def _log_deleted_message(self, message: discord.Message) -> None:
        self.bot.db.record_bot_event(
            "message.deleted",
            message.guild.id,
            message.author.id,
            message.id,
            {"channel_id": message.channel.id},
            source="discord_event",
            status="success",
        )
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
        # Discord only writes a message_delete audit log entry when someone
        # other than the author deletes it (self-deletes aren't audited at
        # all), so finding an entry here means a moderator - not the author
        # - removed it. entry.reason is whatever the deleter's client sent
        # (often blank for a plain right-click delete), so only show it when
        # actually present rather than displaying an empty field.
        entry = await self._find_audit_actor(
            message.guild, discord.AuditLogAction.message_delete, message.author.id,
            channel_id=message.channel.id,
        )
        if entry is not None and entry.user is not None:
            embed.add_field(name="Deleted by", value=str(entry.user), inline=True)
            if entry.reason:
                embed.add_field(name="Reason", value=_truncate(entry.reason, 512), inline=True)
        embed.set_footer(text=f"User ID: {message.author.id}")
        await self._log(message.guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        """Raw counterpart to on_bulk_message_delete for the same reason as
        on_raw_message_delete above - a purge of older messages (all
        evicted from cache) would otherwise log nothing at all."""
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        if self.bot.db.is_log_channel_ignored(guild.id, payload.channel_id):
            return

        if payload.cached_messages:
            # Full detail path - reuse the existing transcript builder.
            await self._log_bulk_deleted_messages(guild, payload.channel_id, payload.cached_messages)
            return

        channel = guild.get_channel(payload.channel_id)
        channel_mention = channel.mention if channel is not None else f"<#{payload.channel_id}>"
        embed = discord.Embed(
            description=(
                f"**{len(payload.message_ids)} messages purged in {channel_mention}**\n"
                "*(content unavailable - none of these were in the bot's cache)*"
            ),
            color=_COLOR_REMOVE,
            timestamp=discord.utils.utcnow(),
        )
        await self._log(guild, "messages", embed)

    async def _log_bulk_deleted_messages(
        self, guild: discord.Guild, channel_id: int, messages: list[discord.Message]
    ) -> None:
        """Purges are noisy - dumping every message inline would blow past
        embed limits fast. Instead we render a transcript: raw content plus
        attachment URLs (a pasted gif/tenor link is already part of
        message.content, so it survives here as plain text - not a re-hosted
        embed, just the link) for every message, attach the full transcript
        as a .txt so nothing purged is actually lost, and show only the most
        recent lines inline for a quick skim.
        """
        channel = guild.get_channel(channel_id)
        channel_mention = channel.mention if channel is not None else f"<#{channel_id}>"

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

        description = f"**{len(messages)} messages purged in {channel_mention}**\n{preview}"
        if len(preview_lines) < len(lines):
            description += f"\n\n*{len(preview_lines)} latest shown*"

        embed = discord.Embed(
            description=_truncate(description, 4096),
            color=_COLOR_REMOVE,
            timestamp=discord.utils.utcnow(),
        )
        # message_bulk_delete's audit log target is the channel (Discord
        # doesn't attribute a bulk delete to individual authors), so this
        # picks up whoever/whatever actually called it - a moderator's
        # native "select and delete" in the Discord client, or the bot
        # itself when it was /purge or a dashboard-queued purge.
        entry = await self._find_audit_actor(
            guild, discord.AuditLogAction.message_bulk_delete, channel_id,
        )
        if entry is not None and entry.user is not None:
            embed.add_field(name="Deleted by", value=str(entry.user), inline=True)

        filename = f"purged-{channel_id}-{int(discord.utils.utcnow().timestamp())}.txt"
        file = discord.File(io.BytesIO(full_transcript.encode("utf-8")), filename=filename)
        await self._log(guild, "messages", embed, file=file)

    # ---- members ----

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        self.bot.db.record_bot_event(
            "member.join",
            member.guild.id,
            None,
            member.id,
            {"member": str(member)},
            source="discord_event",
            status="success",
        )
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
        self.bot.db.record_bot_event(
            "member.leave",
            member.guild.id,
            None,
            member.id,
            {"member": str(member)},
            source="discord_event",
            status="success",
        )
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
            # Discord doesn't audit-log members changing their own nickname -
            # only entries where someone else changed it, so finding one
            # here means staff (or the bot, e.g. tempnick) did this, not the
            # member themselves.
            entry = await self._find_audit_actor(
                after.guild, discord.AuditLogAction.member_update, after.id, changed_attr="nick"
            )
            if entry is not None and entry.user is not None and entry.user.id != after.id:
                embed.add_field(name="Changed by", value=str(entry.user), inline=True)
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
            entry = await self._find_audit_actor(after.guild, discord.AuditLogAction.member_role_update, after.id)
            if entry is not None and entry.user is not None:
                embed.add_field(name="Changed by", value=str(entry.user), inline=True)
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
            # Bot-issued timeouts (/timeout, AutoMod, dashboard) already log
            # their own dedicated moderation embed elsewhere - this listener
            # fires for those too (it's a plain gateway event), so this adds
            # the same attribution for a timeout applied straight from
            # Discord's native member menu, which otherwise showed no actor
            # at all.
            entry = await self._find_audit_actor(
                after.guild, discord.AuditLogAction.member_update, after.id, changed_attr="timed_out_until"
            )
            if entry is not None and entry.user is not None:
                embed.add_field(name="Moderator", value=str(entry.user), inline=True)
                if entry.reason:
                    embed.add_field(name="Reason", value=_truncate(entry.reason, 512), inline=True)
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
            self.bot.db.record_bot_event(
                "voice.join", member.guild.id, member.id, after.channel.id,
                {"channel_id": after.channel.id}, source="discord_event", status="success",
            )
            embed = discord.Embed(
                description=f"**{member.mention} joined voice** {after.channel.mention}",
                color=_COLOR_ADD, timestamp=discord.utils.utcnow(),
            )
        elif after.channel is None:
            self.bot.db.record_bot_event(
                "voice.leave", member.guild.id, member.id, before.channel.id,
                {"channel_id": before.channel.id}, source="discord_event", status="success",
            )
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

    # ---- reactions ----

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        # Analytics/audit metadata only: which emoji isn't recorded, just
        # that a reaction happened - matches the message.received approach
        # of not storing content, only activity counts.
        if payload.guild_id is None:
            return
        if payload.member is not None and payload.member.bot:
            return
        self.bot.db.record_bot_event(
            "reaction.added",
            payload.guild_id,
            payload.user_id,
            payload.message_id,
            {"channel_id": payload.channel_id},
            source="discord_event",
            status="success",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(LoggingCog(bot))
