"""/say - makes the bot post an arbitrary message in a channel. Restricted
to the server owner or Administrators only (not the general Bot Manager
role tier other commands use) since this is a bigger blast radius than a
typical config command: whoever can run it can make the bot say anything,
anywhere it can post. Every use is logged to bot_events for accountability,
and mentions are always suppressed so it can't be used to ping @everyone,
a role, or a user.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils import owner_or_administrator


class Say(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="say", description="Make the bot post a message in a channel (server owner/Administrator only)")
    @app_commands.describe(message="What the bot should say", channel="Which channel to post in (defaults to this one)")
    @owner_or_administrator()
    async def say(self, interaction: discord.Interaction, message: str, channel: discord.TextChannel = None):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        target = channel or interaction.channel
        perms = target.permissions_for(interaction.guild.me)
        if not perms.send_messages:
            await interaction.response.send_message(f"I don't have permission to send messages in {target.mention}.", ephemeral=True)
            return

        try:
            await target.send(message, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            await interaction.response.send_message(f"Couldn't send that: {e}", ephemeral=True)
            return

        self.bot.db.record_bot_event(
            "say.sent", interaction.guild.id, interaction.user.id, target.id,
            {"length": len(message)}, source="discord_event",
        )
        await interaction.response.send_message(f"Sent to {target.mention}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Say(bot))
