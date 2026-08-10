"""Temp voice channels: an admin designates a "hub" voice channel. Joining
the hub creates a fresh voice channel for that member (with elevated
permissions on it - rename, move/mute/deafen others in their own channel),
moves them into it, and deletes it automatically once everyone leaves.

Implemented entirely via on_voice_state_update - no slash command needed to
use it as a member, just join the hub channel.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("tempvoice")


from utils import manager_or_permission

class TempVoice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cleaned_up = False

    @app_commands.command(name="setvoicehub", description="Joining this voice channel gives you your own temporary channel")
    @app_commands.describe(channel="The voice channel that acts as the 'create a channel' hub")
    @manager_or_permission("manage_channels")
    async def setvoicehub(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_voice_hub(interaction.guild.id, channel.id)
        await interaction.response.send_message(
            f"Set - joining {channel.mention} now creates a personal voice channel for whoever joins it."
        )

    @app_commands.command(name="removevoicehub", description="Turn off temp voice channel creation")
    @manager_or_permission("manage_channels")
    async def removevoicehub(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        removed = self.bot.db.remove_voice_hub(interaction.guild.id)
        await interaction.response.send_message("Removed." if removed else "No hub was set.")

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        guild_id = member.guild.id
        hub_channel_id = self.bot.db.get_voice_hub(guild_id)

        # Joined the hub channel -> create them a channel and move them in.
        if hub_channel_id is not None and after.channel is not None and after.channel.id == hub_channel_id:
            await self._create_temp_channel(member, after.channel)

        # Left a bot-created temp channel and it's now empty -> delete it.
        # Checked on every voice state change where someone left a channel,
        # so it also cleans up when the last person just disconnects
        # entirely (not only when moving elsewhere).
        if before.channel is not None and self.bot.db.is_temp_voice_channel(before.channel.id):
            if len(before.channel.members) == 0:
                await self._delete_temp_channel(before.channel)

    async def _create_temp_channel(self, member: discord.Member, hub: discord.VoiceChannel):
        name = f"{member.display_name}'s Channel"[:100]
        try:
            temp_channel = await member.guild.create_voice_channel(name=name, category=hub.category)
            await temp_channel.set_permissions(
                member,
                manage_channels=True,
                move_members=True,
                mute_members=True,
                deafen_members=True,
            )
            await member.move_to(temp_channel)
        except discord.Forbidden:
            logger.warning("missing permissions to create/move into temp voice channel in guild %s", member.guild.id)
            return
        except discord.HTTPException:
            logger.exception("failed to create temp voice channel for %s", member.id)
            return

        self.bot.db.add_temp_voice_channel(member.guild.id, temp_channel.id, member.id)

    async def _delete_temp_channel(self, channel: discord.VoiceChannel):
        self.bot.db.remove_temp_voice_channel(channel.id)
        try:
            await channel.delete(reason="Temp voice channel emptied")
        except (discord.Forbidden, discord.NotFound):
            pass  # already gone, or we lost permission - either way nothing more to do

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready can fire more than once (e.g. after a reconnect) - only
        # sweep for stale channels the first time, not on every reconnect.
        if self._cleaned_up:
            return
        self._cleaned_up = True
        await self.cleanup_stale_channels()

    async def cleanup_stale_channels(self):
        """Called once on startup - if the bot was offline when a temp
        channel emptied out, it never got deleted. Sweep for any tracked
        channel that's gone or currently empty."""
        for guild in self.bot.guilds:
            for channel_id, _owner_id in self.bot.db.list_temp_voice_channels(guild.id):
                channel = guild.get_channel(channel_id)
                if channel is None:
                    self.bot.db.remove_temp_voice_channel(channel_id)
                elif len(channel.members) == 0:
                    await self._delete_temp_channel(channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoice(bot))
