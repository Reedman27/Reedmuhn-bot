import time

import discord
from discord import app_commands
from discord.ext import commands

import scheduler
from utils import format_duration, parse_duration


class Reminders(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="remindme", description="Get a reminder later")
    @app_commands.describe(duration="e.g. 10m, 2h, 3d", message="What to remind you about")
    @app_commands.checks.cooldown(3, 10.0)
    async def remindme(self, interaction: discord.Interaction, duration: str, message: str):
        try:
            seconds = parse_duration(duration)
        except ValueError:
            await interaction.response.send_message(
                "Couldn't parse that duration. Try something like `10m`, `2h`, `3d`.", ephemeral=True
            )
            return

        run_at = int(time.time()) + seconds
        scheduler.schedule_reminder(
            self.bot.db,
            interaction.guild_id or 0,
            run_at,
            interaction.user.id,
            interaction.channel_id,
            message,
        )
        await interaction.response.send_message(f"Got it, I'll remind you in {format_duration(seconds)}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Reminders(bot))
