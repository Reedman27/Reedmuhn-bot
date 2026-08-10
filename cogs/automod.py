"""Automod - message filtering ported from YAGPDB's automod module.
Detection algorithms live in automod_checks.py (pure, unit-tested);
this file is just the Discord-facing wiring: per-message checks, deleting
violations, and escalating to a timeout once someone racks up enough
violations in a rolling window.
"""
import datetime
import logging
import time
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from automod_checks import (
    UserMessageTracker,
    caps_violation,
    contains_banned_word,
    count_consecutive_duplicates,
    find_invite_codes,
    sliding_window_count,
)

logger = logging.getLogger("automod")


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
        lines = [
            f"Enabled: {'yes' if cfg['enabled'] else 'no'}",
            f"Block invites: {'yes' if cfg['block_invites'] else 'no'}",
            f"Banned words: {len(cfg['banned_words'])} configured",
            f"Caps: {cfg['caps_percent']}% (min {cfg['caps_min_len']} capital letters)",
            f"Mention spam: {cfg['mention_threshold']} unique mentions per message",
            f"Message spam: {cfg['spam_count']} messages in {cfg['spam_window_seconds']}s",
            f"Duplicate spam: {cfg['duplicate_count']} identical in a row within {cfg['duplicate_window_seconds']}s",
            f"Escalation: {cfg['violation_mute_threshold']} violations in {cfg['violation_window_seconds']}s -> "
            f"{cfg['violation_mute_duration_seconds']}s mute",
        ]
        await interaction.response.send_message("\n".join(lines))

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

        recent = self.bot.db.count_recent_automod_violations(
            message.guild.id, message.author.id, now - cfg["violation_window_seconds"]
        )

        if recent >= cfg["violation_mute_threshold"] and isinstance(message.author, discord.Member):
            await self._escalate_to_mute(message, cfg)
        else:
            try:
                await message.author.send(
                    f"Your message in **{message.guild.name}** was removed for {reason}. "
                    f"({recent}/{cfg['violation_mute_threshold']} violations before a timeout)"
                )
            except discord.Forbidden:
                pass  # DMs closed - message deletion already happened, nothing more to do

    async def _escalate_to_mute(self, message: discord.Message, cfg: dict):
        duration = datetime.timedelta(seconds=cfg["violation_mute_duration_seconds"])
        try:
            await message.author.timeout(discord.utils.utcnow() + duration, reason="Automod: repeated violations")
        except discord.Forbidden:
            logger.warning(
                "couldn't timeout %s in guild %s - missing permission or role hierarchy",
                message.author.id, message.guild.id,
            )
            return

        self.bot.db.clear_automod_violations(message.guild.id, message.author.id)

        try:
            await message.author.send(
                f"You've been muted in **{message.guild.name}** for {cfg['violation_mute_duration_seconds']}s "
                f"after repeated automod violations."
            )
        except discord.Forbidden:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoMod(bot))
