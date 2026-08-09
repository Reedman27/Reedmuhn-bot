"""Birthday tracking + automatic announcements.

No year is stored, on purpose - we only need month/day to know when to
announce, and it keeps this from ever becoming an age-tracking feature.

The announcement loop ticks hourly and compares against each guild's
current local calendar date in UTC. `last_announced_year` on each row is
what makes this restart-safe: even if the bot was offline exactly at
midnight, the next tick still finds the "unannounced" birthday and posts
it, and won't double-post later the same day.
"""
import calendar
import datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

CHECK_INTERVAL_MINUTES = 60

MONTH_CHOICES = [
    app_commands.Choice(name=calendar.month_name[m], value=m) for m in range(1, 13)
]


from utils import manager_or_permission

class Birthdays(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_birthdays.start()

    def cog_unload(self):
        self.check_birthdays.cancel()

    birthday = app_commands.Group(name="birthday", description="Birthday tracking")

    @birthday.command(name="set", description="Set your birthday (no year - just month and day)")
    @app_commands.describe(month="Birth month", day="Day of the month (1-31)")
    @app_commands.choices(month=MONTH_CHOICES)
    async def set_birthday(
        self, interaction: discord.Interaction, month: app_commands.Choice[int], day: int
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        days_in_month = calendar.monthrange(2024, month.value)[1]  # 2024: leap year, allows Feb 29
        if not 1 <= day <= days_in_month:
            await interaction.response.send_message(
                f"{month.name} only has {days_in_month} days.", ephemeral=True
            )
            return

        self.bot.db.set_birthday(interaction.guild.id, interaction.user.id, month.value, day)
        await interaction.response.send_message(
            f"Got it - your birthday is set to {month.name} {day}.", ephemeral=True
        )

    @birthday.command(name="remove", description="Remove your saved birthday")
    async def remove_birthday(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        removed = self.bot.db.remove_birthday(interaction.guild.id, interaction.user.id)
        msg = "Removed your birthday." if removed else "You don't have a birthday saved."
        await interaction.response.send_message(msg, ephemeral=True)

    @birthday.command(name="view", description="View a birthday")
    @app_commands.describe(user="Whose birthday to check (defaults to you)")
    async def view_birthday(self, interaction: discord.Interaction, user: discord.Member = None):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        target = user or interaction.user
        bday = self.bot.db.get_birthday(interaction.guild.id, target.id)
        if bday is None:
            await interaction.response.send_message(
                f"{target.mention} hasn't set a birthday." if user else "You haven't set a birthday yet.",
                ephemeral=True,
            )
            return

        month, day = bday
        await interaction.response.send_message(f"{target.mention}'s birthday is {calendar.month_name[month]} {day}.")

    @birthday.command(name="upcoming", description="List the next few upcoming birthdays in this server")
    async def upcoming_birthdays(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        all_bdays = self.bot.db.list_birthdays(interaction.guild.id)
        if not all_bdays:
            await interaction.response.send_message("No birthdays saved yet.")
            return

        today = datetime.date.today()

        def days_until(month: int, day: int) -> int:
            days_in_month = calendar.monthrange(today.year, month)[1]
            day = min(day, days_in_month)
            target = datetime.date(today.year, month, day)
            if target < today:
                days_in_next_month = calendar.monthrange(today.year + 1, month)[1]
                target = datetime.date(today.year + 1, month, min(day, days_in_next_month))
            return (target - today).days

        ranked = sorted(all_bdays, key=lambda row: days_until(row[1], row[2]))[:10]

        lines = []
        for user_id, month, day in ranked:
            member = interaction.guild.get_member(user_id)
            name = member.display_name if member else f"<@{user_id}>"
            lines.append(f"**{calendar.month_name[month]} {day}** - {name}")

        embed = discord.Embed(title="🎂 Upcoming Birthdays", description="\n".join(lines))
        await interaction.response.send_message(embed=embed)

    @birthday.command(name="channel", description="Set the channel where birthday announcements are posted")
    @app_commands.describe(channel="Channel for birthday announcements")
    @manager_or_permission("manage_guild")
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        self.bot.db.set_birthday_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"Birthday announcements will now post in {channel.mention}.")

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_birthdays(self):
        today = datetime.date.today()
        for guild_id in self.bot.db.all_guild_ids_with_birthdays():
            config = self.bot.db.get_guild_config(guild_id)
            channel_id = config["birthday_channel_id"]
            if channel_id is None:
                continue  # no announcement channel configured - skip silently

            due = self.bot.db.birthdays_today(guild_id, today.month, today.day, today.year)
            if not due:
                continue

            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except discord.HTTPException:
                    continue

            for user_id in due:
                await channel.send(f"🎉 Happy birthday, <@{user_id}>! 🎂")
                self.bot.db.mark_birthday_announced(guild_id, user_id, today.year)

    @check_birthdays.before_loop
    async def before_check_birthdays(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Birthdays(bot))
