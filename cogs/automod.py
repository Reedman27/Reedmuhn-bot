"""Automod - message filtering ported from YAGPDB's automod module.
Detection algorithms live in automod_checks.py (pure, unit-tested);
this file is just the Discord-facing wiring: per-message checks, deleting
violations, and escalating through a configurable ladder of punishments
(warn, mute role, timeout, kick, ban, temp ban) once someone racks up
enough warnings in a rolling window. The ladder itself - how many warnings
and what happens at each step - is fully configurable per guild from the
WebUI (or /automodescalation), not hardcoded here.
"""
import datetime
import logging
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, tasks

import scheduler
from automod_checks import (
    UserMessageTracker,
    caps_violation,
    contains_banned_word,
    contains_gif,
    count_consecutive_duplicates,
    find_invite_codes,
    sliding_window_count,
)
from utils import format_duration

logger = logging.getLogger("automod")

# Punishments a tier can apply, and how they're described back to the
# member/mod-log. Kept as a plain dict (rather than an enum) since it's
# read/written straight out of SQLite and the WebUI form.
ACTION_LABELS = {
    "warn": "warned",
    "mute_role": "muted",
    "timeout": "timed out",
    "kick": "kicked",
    "ban": "banned",
    "tempban": "temporarily banned",
}
# Actions that take a duration (seconds). Kick/ban/warn are permanent/instant.
TIMED_ACTIONS = {"mute_role", "timeout", "tempban"}
# Actions that resolve the current "cycle" of warnings, so the automod
# violation count resets afterwards. "warn" deliberately isn't one of
# these - it's meant as an early, non-restrictive rung on the ladder (e.g.
# 3 warnings -> formal warn, 5 -> mute, 8 -> ban) and shouldn't erase the
# progress that's building toward the harsher tiers above it.
RESOLVING_ACTIONS = {"mute_role", "timeout", "kick", "ban", "tempban"}
# Discord's own hard cap on a single timeout.
MAX_TIMEOUT_SECONDS = 28 * 86400


from utils import manager_or_permission

class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory only, keyed by (guild_id, channel_id, user_id) - see
        # automod_checks.UserMessageTracker for why this isn't persisted.
        self.trackers: dict[tuple[int, int, int], UserMessageTracker] = defaultdict(UserMessageTracker)
        self._poll_queue_decisions.start()

    def cog_unload(self):
        self._poll_queue_decisions.cancel()

    async def _finalize_queued_violation(self, guild: discord.Guild, member: discord.Member, reason: str) -> None:
        """Runs the same violation-recording + escalation-ladder logic
        _handle_violation applies immediately for a non-queued violation,
        for a queued match a moderator just confirmed. The message itself
        was already deleted when it was queued - this only handles
        counting the violation and applying the ladder, using confirm-time
        as "now" for the counting window."""
        cfg = self.bot.db.get_automod_config(guild.id)
        now = int(time.time())
        self.bot.db.add_automod_violation(guild.id, member.id, reason, now)
        self.bot.db.record_member_history(
            guild.id, member.id, "automod_violation", self.bot.user.id if self.bot.user else None, reason
        )
        recent = self.bot.db.count_recent_automod_violations(guild.id, member.id, now - cfg["violation_window_seconds"])
        tiers = self.bot.db.list_automod_escalation_tiers(guild.id)
        tier = next((t for t in tiers if t["threshold"] == recent), None)
        if tier is None and tiers and recent > tiers[-1]["threshold"]:
            tier = tiers[-1]
        if tier is not None:
            await self._apply_tier_action(guild, member, tier, reason, source="Automod (reviewed)")

    async def _log_queue_dismissal(self, guild: discord.Guild, review: dict, resolved_by: int) -> None:
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is None:
            return
        actor = "Dashboard" if resolved_by == 0 else f"<@{resolved_by}>"
        embed = discord.Embed(
            description=f"**AutoMod: Queued match dismissed** - <@{review['user_id']}> by {actor}",
            color=discord.Color.light_grey(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Reason", value=review["rule_label"], inline=True)
        await logging_cog.log_event(guild, "automod", embed)

    @tasks.loop(seconds=3)
    async def _poll_queue_decisions(self):
        for request_id, guild_id, review_id, decision, resolved_by in self.bot.db.claim_automod_decisions():
            review = self.bot.db.get_automod_review(guild_id, review_id)
            if review is None or review["status"] != "pending":
                continue  # already resolved from Discord's side in the meantime
            status = "confirmed" if decision == "confirm" else "dismissed"
            applied = self.bot.db.resolve_automod_review(guild_id, review_id, resolved_by, status)
            if not applied:
                continue
            guild = self.bot.get_guild(guild_id)
            if decision != "confirm":
                if guild is not None:
                    await self._log_queue_dismissal(guild, review, resolved_by)
                continue
            if guild is None:
                logger.warning("automod queue confirm for unknown guild %s (request %s)", guild_id, request_id)
                continue
            member = guild.get_member(review["user_id"])
            if member is None:
                logger.info("automod queue confirm for guild %s: member %s isn't in the guild anymore", guild_id, review["user_id"])
                continue
            await self._finalize_queued_violation(guild, member, review["rule_label"])

    @_poll_queue_decisions.before_loop
    async def _before_poll_queue_decisions(self):
        await self.bot.wait_until_ready()

    # ---- configuration commands ----

    @app_commands.command(name="automod", description="Turn automod on or off")
    @app_commands.describe(enabled="Whether automod should be active")
    @manager_or_permission("manage_guild")
    async def automod_toggle(self, interaction: discord.Interaction, enabled: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_automod_enabled(interaction.guild.id, enabled)
        await interaction.response.send_message(f"Automod is now {'on' if enabled else 'off'}.")

    automodword = app_commands.Group(
        name="automodword", description="Manage the server's banned-word filter"
    )

    @automodword.command(name="add", description="Add a word/phrase to the banned-word filter")
    @app_commands.describe(word="Word or phrase to remove whenever members say it")
    @manager_or_permission("manage_guild")
    async def automodword_add(self, interaction: discord.Interaction, word: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        word = word.strip()
        if not word or len(word) > 100:
            await interaction.response.send_message("Enter a word or phrase up to 100 characters.", ephemeral=True)
            return
        cfg = self.bot.db.get_automod_config(interaction.guild.id)
        words = cfg["banned_words"]
        if word.lower() in {w.lower() for w in words}:
            await interaction.response.send_message(f"`{word}` is already banned.", ephemeral=True)
            return
        words.append(word)
        self.bot.db.set_automod_words(interaction.guild.id, words)
        await interaction.response.send_message(
            f"Added `{word}` to the banned-word filter. Automod must be enabled for it to take effect."
        )

    @automodword.command(name="remove", description="Remove a word/phrase from the banned-word filter")
    @app_commands.describe(word="Word or phrase to allow again")
    @manager_or_permission("manage_guild")
    async def automodword_remove(self, interaction: discord.Interaction, word: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        cfg = self.bot.db.get_automod_config(interaction.guild.id)
        matches = [w for w in cfg["banned_words"] if w.lower() != word.strip().lower()]
        if len(matches) == len(cfg["banned_words"]):
            await interaction.response.send_message(f"`{word}` isn't currently banned.", ephemeral=True)
            return
        self.bot.db.set_automod_words(interaction.guild.id, matches)
        await interaction.response.send_message(f"Removed `{word.strip()}` from the banned-word filter.")

    @automodword.command(name="list", description="List the server's banned words")
    @manager_or_permission("manage_guild")
    async def automodword_list(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        words = self.bot.db.get_automod_config(interaction.guild.id)["banned_words"]
        if not words:
            await interaction.response.send_message("No banned words are configured.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Banned words: " + ", ".join(f"`{w}`" for w in words),
            ephemeral=True,
        )

    @app_commands.command(name="automodwords", description="Set the banned word list (replaces the current list)")
    @app_commands.describe(words="Comma-separated list of words, e.g. word1, word2, word3")
    @manager_or_permission("manage_guild")
    async def automod_words(self, interaction: discord.Interaction, words: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        word_list = [w.strip() for w in words.split(",") if w.strip()]
        self.bot.db.set_automod_words(interaction.guild.id, word_list)
        await interaction.response.send_message(f"Banned word list updated ({len(word_list)} words).")

    @app_commands.command(name="automodalikewords", description="Toggle catching look-alike spellings of banned words")
    @app_commands.describe(enabled="Whether to also catch spaced-out/leetspeak/stretched-out evasions of banned words")
    @manager_or_permission("manage_guild")
    async def automod_alike_words(self, interaction: discord.Interaction, enabled: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_automod_fuzzy_words(interaction.guild.id, enabled)
        await interaction.response.send_message(
            f"Alike-words matching is now {'on' if enabled else 'off'}. "
            "This makes the banned-word filter also catch spaced-out, leetspeak'd, "
            "punctuated, and stretched-out versions of the words, not just exact matches."
        )

    @app_commands.command(name="automodinvites", description="Toggle blocking Discord invite links")
    @manager_or_permission("manage_guild")
    async def automod_invites(self, interaction: discord.Interaction, block: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_automod_invites(interaction.guild.id, block)
        await interaction.response.send_message(f"Invite link blocking is now {'on' if block else 'off'}.")

    @app_commands.command(name="automodgifs", description="Toggle blocking GIFs (uploaded, or linked from Tenor/Giphy)")
    @manager_or_permission("manage_guild")
    async def automod_gifs(self, interaction: discord.Interaction, block: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_automod_block_gifs(interaction.guild.id, block)
        await interaction.response.send_message(f"GIF blocking is now {'on' if block else 'off'}.")

    @app_commands.command(name="automodstatus", description="Show current automod settings")
    async def automod_status(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        cfg = self.bot.db.get_automod_config(interaction.guild.id)
        tiers = self.bot.db.list_automod_escalation_tiers(interaction.guild.id)
        lines = [
            f"Enabled: {'yes' if cfg['enabled'] else 'no'}",
            f"Block invites: {'yes' if cfg['block_invites'] else 'no'}",
            f"Block GIFs: {'yes' if cfg['block_gifs'] else 'no'}",
            f"Banned words: {len(cfg['banned_words'])} configured (alike-words matching: {'on' if cfg['fuzzy_words'] else 'off'})",
            f"Caps: {cfg['caps_percent']}% (min {cfg['caps_min_len']} capital letters)",
            f"Mention spam: {cfg['mention_threshold']} unique mentions per message",
            f"Message spam: {cfg['spam_count']} messages in {cfg['spam_window_seconds']}s",
            f"Duplicate spam: {cfg['duplicate_count']} identical in a row within {cfg['duplicate_window_seconds']}s",
            f"Warning window: {cfg['violation_window_seconds']}s",
        ]
        if tiers:
            lines.append("Escalation tiers:")
            for tier in tiers:
                lines.append(f"  {tier['threshold']} warnings -> {_describe_tier(tier)}")
        else:
            lines.append("Escalation tiers: none configured (use the WebUI or /automodescalation to add some)")
        await interaction.response.send_message("\n".join(lines))

    automodescalation = app_commands.Group(
        name="automodescalation", description="Configure automod's escalating punishments"
    )

    @automodescalation.command(name="add", description="Add or replace the punishment for a given warning count")
    @app_commands.describe(
        warnings="Number of warnings (within the violation window) that triggers this punishment",
        action="What to do to the member",
        duration="Required for mute/timeout/tempban, e.g. 10m, 1h, 1d. Ignored for kick/ban.",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="Warn", value="warn"),
        app_commands.Choice(name="Mute (role)", value="mute_role"),
        app_commands.Choice(name="Timeout", value="timeout"),
        app_commands.Choice(name="Kick", value="kick"),
        app_commands.Choice(name="Ban", value="ban"),
        app_commands.Choice(name="Temporary ban", value="tempban"),
    ])
    @manager_or_permission("manage_guild")
    async def automodescalation_add(
        self, interaction: discord.Interaction, warnings: app_commands.Range[int, 1, 1000],
        action: app_commands.Choice[str], duration: str = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        from utils import parse_duration

        duration_seconds = None
        if action.value in TIMED_ACTIONS:
            if not duration:
                await interaction.response.send_message(
                    f"{action.name} needs a duration, e.g. `10m`, `1h`, `1d`.", ephemeral=True
                )
                return
            try:
                duration_seconds = parse_duration(duration)
            except ValueError:
                await interaction.response.send_message(
                    "Couldn't parse that duration. Try something like `10m`, `1h`, `1d`.", ephemeral=True
                )
                return
            max_seconds = MAX_TIMEOUT_SECONDS if action.value == "timeout" else 365 * 86400
            if not 1 <= duration_seconds <= max_seconds:
                await interaction.response.send_message(
                    f"Duration for {action.name} must be between 1 second and {format_duration(max_seconds)}.",
                    ephemeral=True,
                )
                return

        self.bot.db.set_automod_escalation_tier(interaction.guild.id, warnings, action.value, duration_seconds)
        await interaction.response.send_message(
            f"At {warnings} warning(s), automod will now {ACTION_LABELS[action.value]} the member"
            + (f" for {format_duration(duration_seconds)}." if duration_seconds else ".")
        )

    @automodescalation.command(name="remove", description="Remove the punishment configured for a given warning count")
    @app_commands.describe(warnings="Warning count whose tier should be removed")
    @manager_or_permission("manage_guild")
    async def automodescalation_remove(self, interaction: discord.Interaction, warnings: int):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        tiers = self.bot.db.list_automod_escalation_tiers(interaction.guild.id)
        match = next((t for t in tiers if t["threshold"] == warnings), None)
        if match is None:
            await interaction.response.send_message(f"No tier configured for {warnings} warnings.", ephemeral=True)
            return
        self.bot.db.remove_automod_escalation_tier(interaction.guild.id, match["id"])
        await interaction.response.send_message(f"Removed the {warnings}-warning tier.")

    @app_commands.command(name="automodqueue", description="List messages held for review (fuzzy word-filter matches)")
    @manager_or_permission("moderate_members")
    async def automodqueue(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        pending = self.bot.db.list_automod_queue(interaction.guild.id, status="pending", limit=15)
        if not pending:
            await interaction.response.send_message("Nothing pending review.", ephemeral=True)
            return
        lines = []
        for item in pending:
            snippet = item["content_snapshot"][:150]
            lines.append(f"`#{item['id']}` <@{item['user_id']}> in <#{item['channel_id']}>: {snippet}")
        embed = discord.Embed(
            title="AutoMod review queue",
            description="\n".join(lines),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="/automodqueueconfirm <id> or /automodqueuedismiss <id> to resolve one")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="automodqueueconfirm", description="Confirm a queued match and apply the escalation ladder")
    @app_commands.describe(review_id="The queue entry's number, from /automodqueue")
    @manager_or_permission("moderate_members")
    async def automodqueueconfirm(self, interaction: discord.Interaction, review_id: int):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        review = self.bot.db.get_automod_review(interaction.guild.id, review_id)
        if review is None or review["status"] != "pending":
            await interaction.response.send_message(f"No pending review entry #{review_id}.", ephemeral=True)
            return
        member = interaction.guild.get_member(review["user_id"])
        applied = self.bot.db.resolve_automod_review(interaction.guild.id, review_id, interaction.user.id, "confirmed")
        if not applied:
            await interaction.response.send_message("That entry was just resolved by someone else.", ephemeral=True)
            return
        if member is None:
            await interaction.response.send_message(
                f"Confirmed #{review_id}, but that member isn't in the server anymore - no action applied.", ephemeral=True
            )
            return
        await self._finalize_queued_violation(interaction.guild, member, review["rule_label"])
        await interaction.response.send_message(f"Confirmed #{review_id} - escalation ladder applied if they hit a tier.", ephemeral=True)

    @app_commands.command(name="automodqueuedismiss", description="Dismiss a queued match with no further action")
    @app_commands.describe(review_id="The queue entry's number, from /automodqueue")
    @manager_or_permission("moderate_members")
    async def automodqueuedismiss(self, interaction: discord.Interaction, review_id: int):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        review = self.bot.db.get_automod_review(interaction.guild.id, review_id)
        applied = self.bot.db.resolve_automod_review(interaction.guild.id, review_id, interaction.user.id, "dismissed")
        if not applied:
            await interaction.response.send_message(f"No pending review entry #{review_id}.", ephemeral=True)
            return
        await interaction.response.send_message(f"Dismissed #{review_id}.", ephemeral=True)
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is not None and review is not None:
            embed = discord.Embed(
                description=f"**AutoMod: Queued match dismissed** - <@{review['user_id']}> by {interaction.user.mention}",
                color=discord.Color.light_grey(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Reason", value=review["rule_label"], inline=True)
            await logging_cog.log_event(interaction.guild, "automod", embed)

    @app_commands.command(name="automodqueuefuzzy", description="Toggle whether fuzzy word-filter matches get queued for review instead of acted on immediately")
    @app_commands.describe(enabled="On to hold fuzzy matches for review; off to act on them immediately like other violations")
    @manager_or_permission("manage_guild")
    async def automodqueuefuzzy(self, interaction: discord.Interaction, enabled: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_automod_queue_fuzzy(interaction.guild.id, enabled)
        await interaction.response.send_message(
            f"Fuzzy word-filter matches will {'be held for review' if enabled else 'act immediately'} from now on."
        )

    # ---- detection ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        cfg = self.bot.db.get_automod_config(message.guild.id)
        if not cfg["enabled"]:
            return

        # AutoMod exemptions are explicit. Having Manage Messages is NOT an
        # automatic bypass; administrators and roles configured in the WebUI
        # are the only exemptions.
        if isinstance(message.author, discord.Member):
            if message.author.guild_permissions.administrator:
                return
            exempt_roles = set(self.bot.db.list_automod_exempt_roles(message.guild.id))
            if exempt_roles.intersection(role.id for role in message.author.roles):
                return

        # Single-message checks (invites, banned words, caps, mentions)
        # don't need history. Spam-rate and duplicate checks do - and need
        # it *before* this message is added to the tracker, or the current
        # message would count as its own history.
        key = (message.guild.id, message.channel.id, message.author.id)
        tracker = self.trackers[key]
        now = time.time()

        violation = self._check_message(message, cfg, tracker, now)

        tracker.record(now, message.content)

        if violation is None:
            return

        reason, queue_for_review = violation
        await self._handle_violation(message, reason, cfg, queue_for_review)

    def _check_message(self, message: discord.Message, cfg: dict, tracker: UserMessageTracker, now: float) -> tuple[str, bool] | None:
        """Returns (reason, queue_for_review) or None if clean.
        queue_for_review is only ever True for a fuzzy-only banned-word
        match when the guild has opted into queuing those (see
        cfg["queue_fuzzy_matches"]) - every other violation type still
        acts immediately, same as before."""
        content = message.content

        if cfg["block_invites"] and find_invite_codes(content):
            return ("posting a Discord invite link", False)

        if cfg["block_gifs"] and contains_gif(
            content,
            attachment_filenames=[a.filename for a in message.attachments],
            attachment_content_types=[a.content_type for a in message.attachments],
        ):
            return ("posting a GIF", False)

        banned = contains_banned_word(content, cfg["banned_words"], fuzzy=cfg["fuzzy_words"])
        if banned:
            word, was_fuzzy_only = banned
            queue = was_fuzzy_only and cfg["queue_fuzzy_matches"]
            return ("using a banned word", queue)

        if caps_violation(content, cfg["caps_min_len"], cfg["caps_percent"]):
            return ("excessive caps", False)

        if len(message.mentions) >= cfg["mention_threshold"] and cfg["mention_threshold"] > 0:
            return ("mentioning too many people at once", False)

        if cfg["spam_count"] > 0:
            # +1 to count this message itself alongside its recent history
            recent_count = sliding_window_count(list(tracker.timestamps), now, cfg["spam_window_seconds"]) + 1
            if recent_count >= cfg["spam_count"]:
                return ("sending messages too quickly", False)

        if cfg["duplicate_count"] > 0:
            dup_count = count_consecutive_duplicates(
                list(tracker.contents), now, cfg["duplicate_window_seconds"], content
            )
            if dup_count >= cfg["duplicate_count"]:
                return ("repeating the same message", False)

        return None

    async def _handle_violation(self, message: discord.Message, reason: str, cfg: dict, queue_for_review: bool = False):
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        if queue_for_review:
            # Hold off on the escalation ladder until a moderator confirms
            # this one - the message is still gone either way (deleted
            # above), only the punishment step waits. content_snapshot
            # preserves what was said since the message itself is gone by
            # the time anyone reviews this.
            self.bot.db.queue_automod_review(
                message.guild.id, message.channel.id, message.author.id, reason, message.content[:1500]
            )
            await self._log_violation(message, reason, queued=True)
            return

        await self._log_violation(message, reason, queued=False)

        now = int(time.time())
        self.bot.db.add_automod_violation(message.guild.id, message.author.id, reason, now)
        self.bot.db.record_member_history(message.guild.id, message.author.id, "automod_violation", self.bot.user.id if self.bot.user else None, reason)

        recent = self.bot.db.count_recent_automod_violations(
            message.guild.id, message.author.id, now - cfg["violation_window_seconds"]
        )

        tiers = self.bot.db.list_automod_escalation_tiers(message.guild.id)
        tier = next((t for t in tiers if t["threshold"] == recent), None)
        if tier is None and tiers and recent > tiers[-1]["threshold"]:
            # Already past every configured tier (e.g. tiers were edited
            # mid-cycle) - apply the harshest one now rather than never
            # firing again until the window resets.
            tier = tiers[-1]

        if tier is not None and isinstance(message.author, discord.Member):
            await self._apply_escalation_tier(message, tier, reason)
            return

        next_tier = next((t for t in tiers if t["threshold"] > recent), None)
        try:
            if next_tier is not None:
                await message.author.send(
                    f"Your message in **{message.guild.name}** was removed for {reason}. "
                    f"({recent}/{next_tier['threshold']} warnings before you're {ACTION_LABELS[next_tier['action']]})"
                )
            else:
                await message.author.send(
                    f"Your message in **{message.guild.name}** was removed for {reason}. "
                    f"You now have {recent} automod warning(s)."
                )
        except discord.Forbidden:
            pass  # DMs closed - message deletion already happened, nothing more to do

    async def _apply_escalation_tier(self, message: discord.Message, tier: dict, violation_reason: str):
        await self._apply_tier_action(message.guild, message.author, tier, violation_reason, source="Automod")

    async def apply_warn_escalation(self, guild: discord.Guild, member: discord.Member, latest_reason: str) -> None:
        """Checks a manually-issued warning (from /warn or the WebUI's "Add
        warning") against the same escalation ladder configured on the
        Automod tab, and applies the matching tier if the member's warning
        count within the configured window lands exactly on one.

        Manual warns and AutoMod violations are tracked separately (the
        `warns` table vs `automod_violations`), but both climb the *same*
        tier list, since a mod hand-issuing 3 warnings is just as much a
        signal as AutoMod catching 3 violations - the ladder shouldn't care
        which one did the counting.

        Deliberately only fires on an *exact* threshold match, unlike
        AutoMod's own violation handling - AutoMod clears its violation
        count after a resolving action so "past every tier" is a rare edge
        case (tiers edited mid-cycle). Manual warns are a permanent record
        that's never auto-cleared, so without the exact-match restriction
        every later warning past the last tier would re-trigger the
        harshest punishment forever.
        """
        cfg = self.bot.db.get_automod_config(guild.id)
        recent = self.bot.db.count_recent_warns(guild.id, member.id, int(time.time()) - cfg["violation_window_seconds"])
        tiers = self.bot.db.list_automod_escalation_tiers(guild.id)
        tier = next((t for t in tiers if t["threshold"] == recent), None)
        if tier is None:
            return
        await self._apply_tier_action(guild, member, tier, latest_reason, source="Warnings")

    async def _apply_tier_action(self, guild: discord.Guild, member: discord.Member, tier: dict, violation_reason: str, source: str):
        action = tier["action"]
        duration = tier["duration_seconds"]
        reason = f"{source}: {tier['threshold']} warnings (latest: {violation_reason})"

        try:
            if action == "warn":
                self.bot.db.add_warn(guild.id, member.id, self.bot.user.id, reason, int(time.time()))
            elif action == "timeout":
                await member.timeout(discord.utils.utcnow() + datetime.timedelta(seconds=duration), reason=reason)
            elif action == "mute_role":
                moderation_cog = self.bot.get_cog("Moderation")
                if moderation_cog is None:
                    logger.warning("can't apply mute_role tier - Moderation cog isn't loaded")
                    return
                ok, why = await moderation_cog.apply_role_mute(member, duration, reason)
                if not ok:
                    logger.warning("automod mute_role tier failed for %s in guild %s: %s", member.id, guild.id, why)
                    return
            elif action == "kick":
                await member.kick(reason=reason)
            elif action == "ban":
                await guild.ban(member, reason=reason, delete_message_seconds=0)
            elif action == "tempban":
                await guild.ban(member, reason=reason, delete_message_seconds=0)
                scheduler.schedule_unban(self.bot.db, guild.id, member.id, int(time.time()) + duration)
            else:
                logger.warning("unknown automod escalation action %r for guild %s", action, guild.id)
                return
        except discord.Forbidden:
            logger.warning(
                "couldn't %s %s in guild %s - missing permission or role hierarchy",
                action, member.id, guild.id,
            )
            return

        # Every other action either removes the member (kick/ban/tempban)
        # or otherwise restricts them (mute/timeout) - either way that
        # "cycle" of warnings is resolved, so reset the automod count. A
        # "warn" tier is deliberately excluded (see RESOLVING_ACTIONS
        # above) so the member keeps climbing toward whatever tier comes
        # next. This only ever clears AutoMod's own violation count - the
        # `warns` table (manual mod warnings) is a permanent record and is
        # never cleared automatically, from either source.
        if action in RESOLVING_ACTIONS:
            self.bot.db.clear_automod_violations(guild.id, member.id)
        self.bot.db.record_member_history(
            guild.id, member.id, f"automod_{action}", self.bot.user.id if self.bot.user else None,
            reason, f"duration_seconds={duration}" if duration else None,
        )

        outcome = ACTION_LABELS[action] + (f" for {format_duration(duration)}" if duration else "")
        try:
            await member.send(
                f"You've been {outcome} in **{guild.name}** after reaching {tier['threshold']} warnings."
            )
        except discord.Forbidden:
            pass

        await self._log_action(guild, tier, member, violation_reason, source)

    async def _log_violation(self, message: discord.Message, reason: str, queued: bool) -> None:
        """Every AutoMod catch, not just the ones that cross an escalation
        threshold - previously only escalations (a small fraction of actual
        catches) showed up in any log channel, so routine filter activity
        was invisible unless someone happened to also cross a punishment
        tier. Separate "automod" category from "moderation" - this is the
        filter doing its job automatically, not a staff-initiated action."""
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is None:
            return
        title = "Held for review" if queued else "Message removed"
        embed = discord.Embed(
            description=f"**AutoMod: {title}** - {message.author.mention} ({message.author}) in {message.channel.mention}",
            color=discord.Color.gold() if queued else discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Reason", value=reason, inline=True)
        if message.content:
            embed.add_field(name="Content", value=message.content[:1000], inline=False)
        embed.set_footer(text=f"User ID: {message.author.id}")
        await logging_cog.log_event(message.guild, "automod", embed)

    async def _log_action(self, guild: discord.Guild, tier: dict, member: discord.Member, violation_reason: str, source: str = "Automod"):
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is None:
            return
        embed = discord.Embed(
            description=f"**{source}: {_describe_tier(tier)}** - {member.mention} ({member})",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Warnings reached", value=str(tier["threshold"]), inline=True)
        embed.add_field(name="Latest reason", value=violation_reason, inline=True)
        embed.set_footer(text=f"User ID: {member.id}")
        # AutoMod-triggered escalations (including a queued match a
        # moderator later confirmed, source="Automod (reviewed)") are the
        # filter's own pipeline ("automod"); a manual /warn crossing the
        # same ladder is a staff-initiated chain of events ("moderation")
        # even though it runs through this same tier-application code.
        category = "automod" if source.startswith("Automod") else "moderation"
        await logging_cog.log_event(guild, category, embed)


def _describe_tier(tier: dict) -> str:
    label = ACTION_LABELS.get(tier["action"], tier["action"])
    if tier["duration_seconds"]:
        return f"{label} ({format_duration(tier['duration_seconds'])})"
    return label


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
