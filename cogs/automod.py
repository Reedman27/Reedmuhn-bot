"""Automod - message filtering ported from YAGPDB's automod module.
Detection algorithms live in automod_checks.py (pure, unit-tested);
this file is just the Discord-facing wiring: per-message checks, deleting
violations, and escalating through a configurable ladder of punishments
(mute role, timeout, kick, ban, temp ban) once someone racks up enough
warnings in a rolling window. The ladder itself - how many warnings and
which punishment at each step - is fully configurable per guild from the
WebUI (or /automodescalation), not hardcoded here.
"""
import datetime
import logging
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

import scheduler
from automod_checks import (
    UserMessageTracker,
    caps_violation,
    contains_banned_word,
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
    "mute_role": "muted",
    "timeout": "timed out",
    "kick": "kicked",
    "ban": "banned",
    "tempban": "temporarily banned",
}
# Actions that take a duration (seconds). Kick/ban are permanent/instant.
TIMED_ACTIONS = {"mute_role", "timeout", "tempban"}
# Discord's own hard cap on a single timeout.
MAX_TIMEOUT_SECONDS = 28 * 86400


from utils import manager_or_permission

class AutoMod(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory only, keyed by (guild_id, channel_id, user_id) - see
        # automod_checks.UserMessageTracker for why this isn't persisted.
        self.trackers: dict[tuple[int, int, int], UserMessageTracker] = defaultdict(UserMessageTracker)

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

    @app_commands.command(name="automodinvites", description="Toggle blocking Discord invite links")
    @manager_or_permission("manage_guild")
    async def automod_invites(self, interaction: discord.Interaction, block: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_automod_invites(interaction.guild.id, block)
        await interaction.response.send_message(f"Invite link blocking is now {'on' if block else 'off'}.")

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
            f"Banned words: {len(cfg['banned_words'])} configured",
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

        violation_reason = self._check_message(message, cfg, tracker, now)

        tracker.record(now, message.content)

        if violation_reason is None:
            return

        await self._handle_violation(message, violation_reason, cfg)

    def _check_message(self, message: discord.Message, cfg: dict, tracker: UserMessageTracker, now: float) -> str | None:
        """Returns a human-readable violation reason, or None if clean."""
        content = message.content

        if cfg["block_invites"] and find_invite_codes(content):
            return "posting a Discord invite link"

        banned = contains_banned_word(content, cfg["banned_words"])
        if banned:
            return "using a banned word"

        if caps_violation(content, cfg["caps_min_len"], cfg["caps_percent"]):
            return "excessive caps"

        if len(message.mentions) >= cfg["mention_threshold"] and cfg["mention_threshold"] > 0:
            return "mentioning too many people at once"

        if cfg["spam_count"] > 0:
            # +1 to count this message itself alongside its recent history
            recent_count = sliding_window_count(list(tracker.timestamps), now, cfg["spam_window_seconds"]) + 1
            if recent_count >= cfg["spam_count"]:
                return "sending messages too quickly"

        if cfg["duplicate_count"] > 0:
            dup_count = count_consecutive_duplicates(
                list(tracker.contents), now, cfg["duplicate_window_seconds"], content
            )
            if dup_count >= cfg["duplicate_count"]:
                return "repeating the same message"

        return None

    async def _handle_violation(self, message: discord.Message, reason: str, cfg: dict):
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

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
        guild, member = message.guild, message.author
        action = tier["action"]
        duration = tier["duration_seconds"]
        reason = f"Automod: {tier['threshold']} warnings (latest: {violation_reason})"

        try:
            if action == "timeout":
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

        # Every action above either removes the member (kick/ban/tempban)
        # or otherwise restricts them (mute/timeout) - either way this
        # "cycle" of warnings is resolved, so reset the count.
        self.bot.db.clear_automod_violations(guild.id, member.id)
        self.bot.db.record_member_history(
            guild.id, member.id, f"automod_{action}", self.bot.user.id if self.bot.user else None,
            reason, f"duration_seconds={duration}" if duration else None,
        )

        outcome = ACTION_LABELS[action] + (f" for {format_duration(duration)}" if duration else "")
        try:
            await member.send(
                f"You've been {outcome} in **{guild.name}** after reaching {tier['threshold']} automod warnings."
            )
        except discord.Forbidden:
            pass

        await self._log_action(guild, tier, member, violation_reason)

    async def _log_action(self, guild: discord.Guild, tier: dict, member: discord.Member, violation_reason: str):
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is None:
            return
        embed = discord.Embed(
            description=f"**Automod: {_describe_tier(tier)}** - {member.mention} ({member})",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Warnings reached", value=str(tier["threshold"]), inline=True)
        embed.add_field(name="Latest violation", value=violation_reason, inline=True)
        embed.set_footer(text=f"User ID: {member.id}")
        await logging_cog.log_event(guild, "moderation", embed)


def _describe_tier(tier: dict) -> str:
    label = ACTION_LABELS.get(tier["action"], tier["action"])
    if tier["duration_seconds"]:
        return f"{label} ({format_duration(tier['duration_seconds'])})"
    return label


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
