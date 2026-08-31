"""Member reports. Any member can flag another with /report; staff triage
them on the dashboard's Reports page (mark reviewing, resolve - optionally
by issuing a warning through the same form, which links the two records -
or dismiss). Reviewing/resolving is dashboard-only rather than also having
Discord-side commands for it, since triage is inherently a "read a queue,
pick one, act on it" workflow the WebUI is already built for; /reports
below is a read-only shortcut for staff who'd rather not leave Discord to
see what's open.

If a reports channel is configured, opening a report also posts a plain
notification there for visibility - actual triage still happens on the
dashboard, this is just so staff don't have to remember to check it.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import manager_or_permission

logger = logging.getLogger("reports")

MAX_REASON_LENGTH = 500


class Reports(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="report", description="Report a member to the staff team")
    @app_commands.describe(user="Who you're reporting", reason="What happened")
    async def report(self, interaction: discord.Interaction, user: discord.Member, reason: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message("You can't report yourself.", ephemeral=True)
            return
        if user.bot:
            await interaction.response.send_message("You can't report a bot.", ephemeral=True)
            return
        reason = reason[:MAX_REASON_LENGTH]

        report_id = self.bot.db.create_report(interaction.guild.id, interaction.user.id, user.id, reason)
        await interaction.response.send_message(
            "Thanks - your report has been sent to the staff team.", ephemeral=True
        )

        cfg = self.bot.db.get_report_config(interaction.guild.id)
        channel_id = cfg.get("channel_id")
        if channel_id:
            channel = interaction.guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                embed = discord.Embed(
                    title=f"New report #{report_id}",
                    description=reason,
                    color=discord.Color.gold(),
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="Reported user", value=user.mention, inline=True)
                embed.add_field(name="Reported by", value=interaction.user.mention, inline=True)
                embed.set_footer(text="Review this on the dashboard's Reports page")
                try:
                    await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
                except discord.Forbidden:
                    logger.warning("missing permission to post report notification in guild %s", interaction.guild.id)

    @app_commands.command(name="reports", description="List open member reports")
    @manager_or_permission("moderate_members")
    async def reports(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        reports = self.bot.db.list_reports(interaction.guild.id, status="open", limit=15)
        if not reports:
            await interaction.response.send_message("No open reports.", ephemeral=True)
            return
        lines = []
        for r in reports:
            lines.append(f"`#{r['id']}` <@{r['target_user_id']}> - reported by <@{r['reporter_id']}>: {r['reason']}")
        embed = discord.Embed(
            title="Open reports",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Review and resolve these on the dashboard's Reports page")
        await interaction.response.send_message(
            embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @app_commands.command(name="setreportschannel", description="Set where new report notifications get posted")
    @app_commands.describe(channel="Channel for report notifications - omit to turn notifications off")
    @manager_or_permission("manage_guild")
    async def setreportschannel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_report_channel(interaction.guild.id, channel.id if channel else None)
        if channel:
            await interaction.response.send_message(f"New reports will be posted in {channel.mention}.")
        else:
            await interaction.response.send_message("Report notifications turned off. Reports still land on the dashboard.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Reports(bot))
