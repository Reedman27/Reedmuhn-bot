"""Counting game: members take turns posting the next number in sequence
in a designated channel. Post the wrong number, or count twice in a row,
and it resets to 0. Tracks each guild's all-time high score.

Numbers can be plain integers or arithmetic expressions (e.g. '7*6' for
42) - both go through utils.safe_eval so nothing unsafe ever runs.

Saves: post the wrong number and, if you've banked a save (earned by
hitting a personal correct-count milestone), it's spent automatically to
forgive the mistake instead of resetting the count. Saves only cover
wrong numbers, not counting twice in a row - that's a distinct rule and
resets regardless of saves, same as the reference bot this was modeled on.
"""
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from utils import CalcError, safe_eval


from utils import manager_or_permission

class Counting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Per-guild lock so two near-simultaneous messages in the counting
        # channel can't both read the same current_number before either has
        # written its update back - without this, two people posting the
        # next number at almost the same time can both be told they're
        # right (or both trigger a reset), and the stored count can end up
        # skipping or repeating a number. Same pattern as InviteTracking's
        # per-guild join lock in cogs/invites.py.
        self._guild_locks: dict[int, asyncio.Lock] = {}

    @app_commands.command(name="calc", description="Evaluates a math expression")
    @app_commands.describe(expression="e.g. 7*6, (3+2)**2, 10/4")
    @app_commands.checks.cooldown(1, 3.0, key=lambda i: i.user.id)
    async def calc(self, interaction: discord.Interaction, expression: str):
        try:
            result = safe_eval(expression)
        except CalcError as exc:
            await interaction.response.send_message(f"Couldn't evaluate that: {exc}", ephemeral=True)
            return
        result = int(result) if isinstance(result, float) and result.is_integer() else result
        await interaction.response.send_message(f"`{expression}` = **{result}**")

    @app_commands.command(name="channel", description="Sets the counting channel for this server")
    @app_commands.describe(channel="The channel members will count in")
    @manager_or_permission("manage_guild")
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        self.bot.db.set_counting_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"Counting channel set to {channel.mention}. Next number is **1**."
        )

    @app_commands.command(name="current-number", description="Replies with the current number and high score")
    async def current_number(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        state = self.bot.db.get_counting(interaction.guild.id)
        if state is None:
            await interaction.response.send_message(
                "No counting channel set up yet. An admin can set one with `/channel`.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Current number: **{state['current_number']}** | High score: **{state['high_score']}**"
        )

    @app_commands.command(name="saves", description="Check your banked saves and progress toward the next one")
    async def saves(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        state = self.bot.db.get_counting(interaction.guild.id)
        milestone = state["save_milestone"] if state else 50
        max_saves = state["max_saves"] if state else 3

        stats = self.bot.db.get_user_counting_stats(interaction.guild.id, interaction.user.id)
        remaining_to_next = milestone - (stats["correct_count"] % milestone)
        if stats["saves"] >= max_saves:
            progress_note = f"Saves banked: **{stats['saves']}/{max_saves}** (maxed out)"
        else:
            progress_note = (
                f"Saves banked: **{stats['saves']}/{max_saves}** "
                f"({remaining_to_next} more correct count{'s' if remaining_to_next != 1 else ''} to earn another)"
            )

        await interaction.response.send_message(
            f"Lifetime correct counts: **{stats['correct_count']}**\n{progress_note}", ephemeral=True
        )

    @app_commands.command(name="savemilestone", description="Configure how saves are earned in the counting game")
    @app_commands.describe(
        milestone="Correct counts needed to earn 1 save (default 50)",
        max_saves="Max saves a person can bank at once (default 3)",
    )
    @manager_or_permission("manage_guild")
    async def savemilestone(self, interaction: discord.Interaction, milestone: int, max_saves: int):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        if milestone < 1 or max_saves < 0:
            await interaction.response.send_message(
                "Milestone must be at least 1, and max saves can't be negative.", ephemeral=True
            )
            return

        self.bot.db.set_save_settings(interaction.guild.id, milestone, max_saves)
        await interaction.response.send_message(
            f"Saves are now earned every **{milestone}** correct counts, capped at **{max_saves}** banked."
        )

    @app_commands.command(name="highscorealerts", description="Toggle the '🏆 new high score!' announcement")
    @app_commands.describe(enabled="Whether to announce it in the counting channel when a new high score is hit")
    @manager_or_permission("manage_guild")
    async def highscorealerts(self, interaction: discord.Interaction, enabled: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_high_score_alerts(interaction.guild.id, enabled)
        self.bot.db.record_bot_event("counting.high_score_alerts", interaction.guild.id, interaction.user.id, None, f"enabled={enabled}")
        await interaction.response.send_message(
            f"High score alerts are now {'on' if enabled else 'off'}."
            + ("" if enabled else " `/current-number` still shows the high score any time you want it.")
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        state = self.bot.db.get_counting(message.guild.id)
        if state is None or message.channel.id != state["channel_id"]:
            return

        # Anything that doesn't even look like a number/expression is
        # probably just chat in the counting channel - ignore it rather
        # than treating every message as a failed count.
        stripped = message.content.strip()
        if not stripped or not any(ch.isdigit() for ch in stripped):
            return

        try:
            value = safe_eval(stripped)
        except CalcError:
            return  # not a valid expression either - ignore, don't punish

        # Everything from here on reads current_number/last_user_id and then
        # writes an update based on what it read, so it all has to happen as
        # one unit per guild - otherwise two messages posted at nearly the
        # same time (each awaiting a reaction/send before its DB write lands)
        # can both read the same state and step on each other's update.
        lock = self._guild_locks.setdefault(message.guild.id, asyncio.Lock())
        async with lock:
            # Re-fetch inside the lock: the state read above (used only for
            # the cheap channel-match check) may be stale by now if another
            # message was still being processed when this one arrived.
            state = self.bot.db.get_counting(message.guild.id)
            if state is None:
                return

            expected = state["current_number"] + 1

            if message.author.id == state["last_user_id"]:
                # Saves deliberately don't cover this - counting twice in a row
                # is a distinct rule violation, not a miscount.
                await message.add_reaction("❌")
                await message.channel.send(
                    f"{message.author.mention} you can't count twice in a row! Back to **0**. Next up: **1**."
                )
                self.bot.db.reset_count(message.guild.id)
                return

            if value != expected:
                user_stats = self.bot.db.get_user_counting_stats(message.guild.id, message.author.id)
                if user_stats["saves"] > 0:
                    remaining = self.bot.db.use_save(message.guild.id, message.author.id)
                    await message.add_reaction("🛡️")
                    await message.channel.send(
                        f"Save used! You have {remaining} save(s) left. "
                        f"Still on **{state['current_number']}** - next up: **{expected}**."
                    )
                    return

                await message.add_reaction("❌")
                await message.channel.send(
                    f"{message.author.mention} wrong number! Expected **{expected}**. Back to **0**. Next up: **1**."
                )
                self.bot.db.reset_count(message.guild.id)
                return

            await message.add_reaction("✅")
            self.bot.db.advance_count(message.guild.id, expected, message.author.id)

            _, _, earned_save = self.bot.db.record_correct_count(
                message.guild.id, message.author.id, state["save_milestone"], state["max_saves"]
            )
            if earned_save:
                await message.channel.send(f"🛡️ {message.author.mention} earned a save for counting accuracy!")

            # Off by default (it used to fire on every single count once past
            # the old record, which got noisy fast) - opt back in per-server
            # with /highscorealerts on, or via the WebUI toggle.
            if state["high_score_alerts"] and expected > state["high_score"] > 0:
                await message.channel.send(f"🏆 New high score: **{expected}**!")


async def setup(bot: commands.Bot):
    await bot.add_cog(Counting(bot))
