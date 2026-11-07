"""Counting game.

Members take turns posting the next whole number in the counting channel.
A wrong number, or posting twice in a row, sends the count back to 0.

Server saves (optional, switched on per server from the WebUI or
/counting save-config): the whole server earns one save each time the count
reaches a multiple of the save interval (50 by default, so at 50, 100,
150...). After a reset the server has to climb back up to the interval to
earn another. When a mistake happens and the server has a save banked, the
save is spent and the count carries on instead of resetting.

All of the rules live in Db.apply_count_attempt so a message is judged and
stored in one step; this cog only reads messages and reports the outcome.
"""
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.utils import manager_or_permission

# Plain whole numbers only (no maths, no text). Capped at 18 digits.
_NUMBER_ONLY = re.compile(r"[0-9]{1,18}", re.ASCII)


class Counting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    counting = app_commands.Group(name="counting", description="Counting game commands")

    async def _need_guild(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return False
        return True

    @counting.command(name="channel", description="Set the counting channel for this server")
    @app_commands.describe(channel="The channel members will count in")
    @manager_or_permission("manage_guild")
    async def channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if not await self._need_guild(interaction):
            return
        self.bot.db.set_counting_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"Counting channel set to {channel.mention}. Next number is **{self._next_number(interaction.guild.id)}**."
        )

    def _next_number(self, guild_id: int) -> int:
        state = self.bot.db.get_counting(guild_id)
        return (state["current_number"] if state else 0) + 1

    @counting.command(name="current-number", description="Show the current number and the high score")
    async def current_number(self, interaction: discord.Interaction):
        if not await self._need_guild(interaction):
            return
        state = self.bot.db.get_counting(interaction.guild.id)
        if state is None or not state["channel_id"]:
            await interaction.response.send_message(
                "No counting channel set up yet. An admin can set one with `/counting channel`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Current number: **{state['current_number']}** | High score: **{state['high_score']}**"
        )

    @counting.command(name="saves", description="See the server's banked counting saves")
    async def saves(self, interaction: discord.Interaction):
        if not await self._need_guild(interaction):
            return
        state = self.bot.db.get_counting(interaction.guild.id)
        if state is None or not state["saves_enabled"]:
            await interaction.response.send_message(
                "Counting saves are turned off in this server. An admin can turn them on with "
                "`/counting save-config` or from the dashboard.",
                ephemeral=True,
            )
            return
        interval = state["save_milestone"]
        current = state["current_number"]
        to_next = interval - (current % interval)
        lines = [f"Saves banked: **{state['server_saves']}/{state['max_saves']}**"]
        if state["server_saves"] >= state["max_saves"]:
            lines.append("The bank is full.")
        else:
            lines.append(
                f"A new save is earned every **{interval}** numbers - "
                f"**{to_next}** more to reach the next one (at {current + to_next})."
            )
        await interaction.response.send_message("\n".join(lines))

    @counting.command(name="save-config", description="Turn counting saves on or off and tune them")
    @app_commands.describe(
        enabled="Turn server saves on or off",
        interval="Earn one save each time the count reaches a multiple of this (default 50)",
        cap="Most saves the server can bank at once (default 3)",
    )
    @manager_or_permission("manage_guild")
    async def save_config(
        self,
        interaction: discord.Interaction,
        enabled: Optional[bool] = None,
        interval: Optional[app_commands.Range[int, 1, 1_000_000]] = None,
        cap: Optional[app_commands.Range[int, 0, 100]] = None,
    ):
        if not await self._need_guild(interaction):
            return
        db = self.bot.db
        guild_id = interaction.guild.id
        state = db.get_counting(guild_id)
        if enabled is None and interval is None and cap is None:
            await interaction.response.send_message(
                "Nothing to change - pass `enabled`, `interval` or `cap`.", ephemeral=True
            )
            return
        if interval is not None or cap is not None:
            db.set_save_settings(
                guild_id,
                interval if interval is not None else (state["save_milestone"] if state else 50),
                cap if cap is not None else (state["max_saves"] if state else 3),
            )
        if enabled is not None:
            db.set_saves_enabled(guild_id, enabled)
        state = db.get_counting(guild_id)
        db.record_bot_event(
            "counting.save_config", guild_id, interaction.user.id, None,
            f"enabled={state['saves_enabled']} interval={state['save_milestone']} cap={state['max_saves']}",
        )
        await interaction.response.send_message(
            f"Saves are **{'on' if state['saves_enabled'] else 'off'}**. "
            f"One is earned every **{state['save_milestone']}** numbers, up to **{state['max_saves']}** banked."
        )

    @counting.command(name="highscorealerts", description="Toggle the new high score announcement")
    @app_commands.describe(enabled="Announce a new high score in the counting channel")
    @manager_or_permission("manage_guild")
    async def highscorealerts(self, interaction: discord.Interaction, enabled: bool):
        if not await self._need_guild(interaction):
            return
        self.bot.db.set_high_score_alerts(interaction.guild.id, enabled)
        self.bot.db.record_bot_event(
            "counting.high_score_alerts", interaction.guild.id, interaction.user.id, None, f"enabled={enabled}"
        )
        await interaction.response.send_message(
            f"High score alerts are now {'on' if enabled else 'off'}."
            + ("" if enabled else " `/counting current-number` still shows the high score any time.")
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        text = message.content.strip()
        if not _NUMBER_ONLY.fullmatch(text):
            return  # ordinary chat in the counting channel is left alone

        state = self.bot.db.get_counting(message.guild.id)
        if state is None or message.channel.id != state["channel_id"]:
            return

        # apply_count_attempt judges and stores the attempt in a single
        # synchronous step, so two messages can never see the same number.
        outcome = self.bot.db.apply_count_attempt(message.guild.id, message.author.id, int(text))
        if outcome is None:
            return
        await self._report(message, state, outcome)

    async def _react(self, message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass  # missing permission or deleted message - not worth failing over

    async def _say(self, message: discord.Message, content: str) -> None:
        try:
            await message.channel.send(content)
        except discord.HTTPException:
            pass

    async def _report(self, message: discord.Message, state: dict, outcome: dict) -> None:
        result = outcome["result"]
        who = message.author.mention

        if result == "ok":
            await self._react(message, "✅")
            if outcome["earned_save"]:
                await self._say(
                    message,
                    f"🛡️ **{outcome['number']}**! The server earned a save "
                    f"(**{outcome['saves']}/{state['max_saves']}** banked).",
                )
            if outcome["new_high"] and state["high_score_alerts"]:
                await self._say(message, f"🏆 New high score: **{outcome['number']}**!")
            return

        why = "you can't count twice in a row" if outcome["reason"] == "repeat" else "that's not the next number"

        if result == "nudge":
            await self._react(message, "⚠️")
            await self._say(message, f"{why.capitalize()} - the count starts at **1**.")
        elif result == "saved":
            await self._react(message, "🛡️")
            await self._say(
                message,
                f"{who} {why}, but a server save kept the count alive! "
                f"**{outcome['saves_left']}** save(s) left. Still on **{outcome['current']}** - next up: **{outcome['expected']}**.",
            )
        else:  # reset
            await self._react(message, "❌")
            await self._say(
                message,
                f"{who} {why}. The count was at **{outcome['lost']}** and is back to **0** - next up: **1**.",
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Counting(bot))
