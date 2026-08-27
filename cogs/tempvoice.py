"""Temp voice channels: an admin designates one or more "hub" voice
channels. Joining any hub creates a fresh voice channel for that member
(with elevated permissions on it - rename, move/mute/deafen others in their
own channel), moves them into it, and deletes it automatically once
everyone leaves.

Implemented entirely via on_voice_state_update - no slash command needed to
use it as a member, just join a hub channel.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger("tempvoice")


from utils import manager_or_permission

class TempVoice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cleaned_up = False
        self._delete_request_worker.start()
        self._limit_request_worker.start()

    def cog_unload(self):
        self._delete_request_worker.cancel()
        self._limit_request_worker.cancel()

    @tasks.loop(seconds=2)
    async def _delete_request_worker(self):
        for guild in self.bot.guilds:
            for _request_id, channel_id in self.bot.db.pop_temp_voice_delete_requests(guild.id):
                channel = guild.get_channel(channel_id)
                if channel is None:
                    # Discord channel was deleted externally; clean stale DB state.
                    self.bot.db.remove_temp_voice_channel(channel_id)
                    continue
                if isinstance(channel, discord.VoiceChannel) and self.bot.db.is_temp_voice_channel(channel_id, guild.id):
                    if not await self._delete_temp_channel(channel):
                        # Keep the request queued if Discord temporarily refuses
                        # the deletion; the worker will retry on its next pass.
                        self.bot.db.request_temp_voice_delete(guild.id, channel_id)

    @_delete_request_worker.before_loop
    async def _before_delete_request_worker(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=2)
    async def _limit_request_worker(self):
        # Dashboard has no live Discord connection, so a user-limit change
        # made from the WebUI is queued here and applied by the bot - same
        # pattern as _delete_request_worker above.
        for guild in self.bot.guilds:
            for channel_id, user_limit in self.bot.db.pop_temp_voice_limit_requests(guild.id):
                channel = guild.get_channel(channel_id)
                if channel is None:
                    self.bot.db.remove_temp_voice_channel(channel_id)
                    continue
                if not (isinstance(channel, discord.VoiceChannel) and self.bot.db.is_temp_voice_channel(channel_id, guild.id)):
                    continue
                try:
                    await channel.edit(user_limit=user_limit, reason="Dashboard-requested channel limit change")
                except discord.NotFound:
                    self.bot.db.remove_temp_voice_channel(channel_id)
                except discord.HTTPException:
                    logger.exception("failed to apply dashboard user-limit change to temp voice channel %s", channel_id)
                    # Keep it queued so the worker retries on its next pass.
                    self.bot.db.request_temp_voice_limit(guild.id, channel_id, user_limit)
                else:
                    self.bot.db.update_temp_voice_channel_limit(channel_id, user_limit)

    @_limit_request_worker.before_loop
    async def _before_limit_request_worker(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="setvoicehub", description="Joining this voice channel gives you your own temporary channel")
    @app_commands.describe(
        channel="A voice channel to add as a 'create a channel' hub",
        user_limit="Max people allowed in channels created from this hub (0 = unlimited). Omit to leave unchanged.",
    )
    @manager_or_permission("manage_channels")
    async def setvoicehub(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel,
        user_limit: app_commands.Range[int, 0, 99] | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        added = self.bot.db.add_voice_hub(interaction.guild.id, channel.id, user_limit or 0)
        if added:
            limit_note = f" New channels will be capped at {user_limit} people." if user_limit else ""
            await interaction.response.send_message(
                f"Added - joining {channel.mention} now creates a personal voice channel for whoever joins it.{limit_note}"
            )
        elif user_limit is not None:
            self.bot.db.set_voice_hub_limit(interaction.guild.id, channel.id, user_limit)
            await interaction.response.send_message(
                f"{channel.mention} is already a voice hub - updated its default channel limit to "
                f"{user_limit if user_limit else 'unlimited'}."
            )
        else:
            await interaction.response.send_message(f"{channel.mention} is already a voice hub.")

    @app_commands.command(name="removevoicehub", description="Turn off temp voice channel creation for a hub")
    @app_commands.describe(channel="The hub voice channel to remove")
    @manager_or_permission("manage_channels")
    async def removevoicehub(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        removed = self.bot.db.remove_voice_hub(interaction.guild.id, channel.id)
        await interaction.response.send_message("Removed." if removed else f"{channel.mention} wasn't a hub.")

    @app_commands.command(name="listvoicehubs", description="List the voice hubs configured in this server")
    @manager_or_permission("manage_channels")
    async def listvoicehubs(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        hubs = self.bot.db.list_voice_hubs(interaction.guild.id)
        if not hubs:
            await interaction.response.send_message("No voice hubs are set.")
            return
        lines = []
        for hub_id, user_limit in hubs:
            channel = interaction.guild.get_channel(hub_id)
            name = channel.mention if channel is not None else f"*(deleted channel {hub_id})*"
            lines.append(f"{name} - limit: {user_limit if user_limit else 'unlimited'}")
        await interaction.response.send_message("Voice hubs:\n" + "\n".join(lines))

    @app_commands.command(name="vclimit", description="Set how many people can join your temporary voice channel")
    @app_commands.describe(user_limit="Max people allowed in your channel (0 = unlimited)")
    async def vclimit(self, interaction: discord.Interaction, user_limit: app_commands.Range[int, 0, 99]):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        channel = interaction.user.voice.channel if interaction.user.voice else None
        if channel is None or not self.bot.db.is_temp_voice_channel(channel.id, interaction.guild.id):
            await interaction.response.send_message(
                "Join your temporary voice channel first, then run this again.", ephemeral=True
            )
            return
        if not channel.permissions_for(interaction.user).manage_channels:
            await interaction.response.send_message(
                "Only that channel's owner (or a moderator) can change its limit.", ephemeral=True
            )
            return
        try:
            await channel.edit(user_limit=user_limit, reason=f"Set via /vclimit by {interaction.user}")
        except discord.Forbidden:
            await interaction.response.send_message("I don't have permission to edit that channel.", ephemeral=True)
            return
        except discord.HTTPException:
            logger.exception("failed to set user_limit on temp voice channel %s", channel.id)
            await interaction.response.send_message("Something went wrong changing the limit.", ephemeral=True)
            return
        self.bot.db.update_temp_voice_channel_limit(channel.id, user_limit)
        await interaction.response.send_message(
            f"Set {channel.mention}'s limit to {user_limit if user_limit else 'unlimited'}."
        )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        guild_id = member.guild.id

        # Joined a hub channel -> create them a channel and move them in.
        if after.channel is not None and self.bot.db.is_voice_hub(guild_id, after.channel.id):
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
        hub_limit = self.bot.db.get_voice_hub_limit(member.guild.id, hub.id)
        try:
            temp_channel = await member.guild.create_voice_channel(name=name, category=hub.category, user_limit=hub_limit)
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

        self.bot.db.add_temp_voice_channel(member.guild.id, temp_channel.id, member.id, hub_limit)

    async def _delete_temp_channel(self, channel: discord.VoiceChannel) -> bool:
        try:
            await channel.delete(reason="Temp voice channel emptied")
        except discord.NotFound:
            # It is already gone, so its database record can safely disappear.
            self.bot.db.remove_temp_voice_channel(channel.id)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to delete temp voice channel %s", channel.id)
            return False

        self.bot.db.remove_temp_voice_channel(channel.id)
        return True

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
            for channel_id, _owner_id, _user_limit in self.bot.db.list_temp_voice_channels(guild.id):
                channel = guild.get_channel(channel_id)
                if channel is None:
                    self.bot.db.remove_temp_voice_channel(channel_id)
                elif len(channel.members) == 0:
                    await self._delete_temp_channel(channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoice(bot))
