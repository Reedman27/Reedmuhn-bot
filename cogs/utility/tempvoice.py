"""Temp voice channels: an admin designates one or more "hub" voice
channels. Joining any hub creates a fresh voice channel for that member
(with elevated permissions on it - rename, move/mute/deafen others in their
own channel), moves them into it, and deletes it automatically once
everyone leaves.

What the creator gets is set per server on the dashboard (Temporary Voice >
Creator permissions): which of a short, fixed list of channel-only permissions
they hold, an optional temporary role (shown separately in the member list),
and a log channel. The list in db.TEMP_VOICE_OWNER_PERMS is a hard ceiling that
the bot re-applies when it builds the permission overwrite, so the creator can
never be handed server-wide powers. Anyone can read the current setup with
/voicehub perms.

Guard rails so it cannot spiral: one live channel per person, a per-server cap,
a short per-person cooldown, and invites that expire.
"""
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from db import TEMP_VOICE_OWNER_MASK, TEMP_VOICE_OWNER_PERMS
from utils import manager_or_permission

logger = logging.getLogger("tempvoice")

CREATE_COOLDOWN_SECONDS = 5
INVITE_MAX_AGE_SECONDS = 3600
INVITE_MAX_USES = 10


def owner_perm_names(owner_perms: int) -> list[str]:
    """Names of the permissions set in ``owner_perms``, after clamping to the
    allowed list. Anything outside TEMP_VOICE_OWNER_PERMS is dropped."""
    owner_perms &= TEMP_VOICE_OWNER_MASK
    return [name for name, (bit, _label) in TEMP_VOICE_OWNER_PERMS.items() if owner_perms & bit]


def owner_has(owner_perms: int, perm_name: str) -> bool:
    return perm_name in owner_perm_names(owner_perms)


def build_owner_overwrite(owner_perms: int) -> discord.PermissionOverwrite:
    return discord.PermissionOverwrite(**{name: True for name in owner_perm_names(owner_perms)})


def channel_name_for(template: str, display_name: str) -> str:
    template = template.strip() or "{user}'s Channel"
    if "{user}" not in template:
        template += " {user}"
    return template.replace("{user}", display_name)[:100]

class TempVoice(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        bot.register_webui_wake("tempvoice_delete", self._delete_request_worker)
        bot.register_webui_wake("tempvoice_limit", self._limit_request_worker)
        self._cleaned_up = False
        self._last_create: dict[int, float] = {}
        self._delete_request_worker.start()
        self._limit_request_worker.start()

    def cog_unload(self):
        self._delete_request_worker.cancel()
        self._limit_request_worker.cancel()

    @tasks.loop(seconds=2)
    async def _delete_request_worker(self):
        for guild in self.bot.guilds:
            try:
                requests = self.bot.db.pop_temp_voice_delete_requests(guild.id)
            except Exception:
                logger.exception("failed to pop temp voice delete requests for guild %s", guild.id)
                continue
            for _request_id, channel_id in requests:
                try:
                    channel = guild.get_channel(channel_id)
                    if channel is None:
                        # Discord channel was deleted externally; clean stale DB state.
                        await self._forget_channel(guild, channel_id)
                        continue
                    if isinstance(channel, discord.VoiceChannel) and self.bot.db.is_temp_voice_channel(channel_id, guild.id):
                        if not await self._delete_temp_channel(channel):
                            # Keep the request queued if Discord temporarily refuses
                            # the deletion; the worker will retry on its next pass.
                            self.bot.db.request_temp_voice_delete(guild.id, channel_id)
                except Exception:
                    # One bad request must not stop the rest of this guild's
                    # queue, or every later guild's, from being processed - and
                    # must not kill the tasks.Loop for the rest of the process.
                    logger.exception("failed to process temp voice delete request for channel %s", channel_id)

    @_delete_request_worker.before_loop
    async def _before_delete_request_worker(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=2)
    async def _limit_request_worker(self):
        # Dashboard has no live Discord connection, so a user-limit change
        # made from the WebUI is queued here and applied by the bot - same
        # pattern as _delete_request_worker above.
        for guild in self.bot.guilds:
            try:
                requests = self.bot.db.pop_temp_voice_limit_requests(guild.id)
            except Exception:
                logger.exception("failed to pop temp voice limit requests for guild %s", guild.id)
                continue
            for channel_id, user_limit in requests:
                try:
                    channel = guild.get_channel(channel_id)
                    if channel is None:
                        await self._forget_channel(guild, channel_id)
                        continue
                    if not (isinstance(channel, discord.VoiceChannel) and self.bot.db.is_temp_voice_channel(channel_id, guild.id)):
                        continue
                    try:
                        await channel.edit(user_limit=user_limit, reason="Dashboard-requested channel limit change")
                    except discord.NotFound:
                        await self._forget_channel(guild, channel_id)
                    except discord.HTTPException:
                        logger.exception("failed to apply dashboard user-limit change to temp voice channel %s", channel_id)
                        # Keep it queued so the worker retries on its next pass.
                        self.bot.db.request_temp_voice_limit(guild.id, channel_id, user_limit)
                    else:
                        self.bot.db.update_temp_voice_channel_limit(channel_id, user_limit)
                except Exception:
                    logger.exception("failed to process temp voice limit request for channel %s", channel_id)

    @_limit_request_worker.before_loop
    async def _before_limit_request_worker(self):
        await self.bot.wait_until_ready()

    voicehub = app_commands.Group(name="voicehub", description="Temporary voice-channel hubs")

    @voicehub.command(name="set", description="Joining this voice channel gives you your own temporary channel")
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

    @voicehub.command(name="remove", description="Turn off temp voice channel creation for a hub")
    @app_commands.describe(channel="The hub voice channel to remove")
    @manager_or_permission("manage_channels")
    async def removevoicehub(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        removed = self.bot.db.remove_voice_hub(interaction.guild.id, channel.id)
        await interaction.response.send_message("Removed." if removed else f"{channel.mention} wasn't a hub.")

    @voicehub.command(name="list", description="List the voice hubs configured in this server")
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

    @voicehub.command(name="limit", description="Set how many people can join your temporary voice channel")
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
        if not self._may_manage(interaction.user, channel, "manage_channels"):
            await interaction.response.send_message(
                "Only that channel's owner (or a moderator) can change its limit.", ephemeral=True
            )
            return
        try:
            await channel.edit(user_limit=user_limit, reason=f"Set via /voicehub limit by {interaction.user}")
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

    # ---- creator tools -------------------------------------------------

    def _may_manage(self, member: discord.Member, channel: discord.VoiceChannel, perm_name: str) -> bool:
        """True for the channel's creator (when the server's creator
        permissions include ``perm_name``) and for staff with Manage Channels."""
        if member.guild_permissions.manage_channels:
            return True
        info = self.bot.db.get_temp_voice_info(channel.id)
        if not info or info["owner_id"] != member.id:
            return False
        return owner_has(self.bot.db.get_temp_voice_settings(member.guild.id)["owner_perms"], perm_name)

    def _own_channel(self, interaction: discord.Interaction) -> discord.VoiceChannel | None:
        member = interaction.user
        if interaction.guild is None or not isinstance(member, discord.Member):
            return None
        channel = member.voice.channel if member.voice else None
        if channel is None or not self.bot.db.is_temp_voice_channel(channel.id, interaction.guild.id):
            return None
        return channel

    @voicehub.command(name="rename", description="Rename your temporary voice channel")
    @app_commands.describe(name="New channel name")
    async def vcrename(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 100]):
        channel = self._own_channel(interaction)
        if channel is None:
            await interaction.response.send_message("Join your temporary voice channel first.", ephemeral=True)
            return
        if not self._may_manage(interaction.user, channel, "manage_channels"):
            await interaction.response.send_message("You can't rename this channel.", ephemeral=True)
            return
        try:
            await channel.edit(name=name.strip()[:100], reason=f"Renamed by {interaction.user}")
        except discord.HTTPException:
            await interaction.response.send_message(
                "Discord refused the rename (it allows only a couple per 10 minutes). Try again shortly.", ephemeral=True
            )
            return
        await interaction.response.send_message(f"Renamed to **{channel.name}**.", ephemeral=True)

    @voicehub.command(name="invite", description="Make a temporary invite link to your voice channel")
    async def vcinvite(self, interaction: discord.Interaction):
        channel = self._own_channel(interaction)
        if channel is None:
            await interaction.response.send_message("Join your temporary voice channel first.", ephemeral=True)
            return
        if not self._may_manage(interaction.user, channel, "create_instant_invite"):
            await interaction.response.send_message("This server doesn't let channel owners create invites.", ephemeral=True)
            return
        try:
            invite = await channel.create_invite(
                max_age=INVITE_MAX_AGE_SECONDS, max_uses=INVITE_MAX_USES, unique=True,
                reason=f"Voice invite by {interaction.user}",
            )
        except discord.HTTPException:
            await interaction.response.send_message("I couldn't create an invite for that channel.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"{invite.url}\nExpires in {INVITE_MAX_AGE_SECONDS // 60} minutes, up to {INVITE_MAX_USES} uses.",
            ephemeral=True,
        )

    @voicehub.command(name="perms", description="See what the creator of a temporary voice channel can do here")
    async def vcperms(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        settings = self.bot.db.get_temp_voice_settings(interaction.guild.id)
        granted = set(owner_perm_names(settings["owner_perms"]))
        lines = [
            f"{'✅' if name in granted else '❌'} {label}"
            for name, (_bit, label) in TEMP_VOICE_OWNER_PERMS.items()
        ]
        lines.append(f"{'✅' if settings['create_role'] else '❌'} A temporary role while the channel exists")
        lines.append(f"Max live channels in this server: {settings['max_channels']} (one per person)")
        lines.append("These only apply inside your own channel. Admins change them on the dashboard.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

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

    async def _log(self, guild: discord.Guild, settings: dict, text: str) -> None:
        channel_id = settings.get("log_channel_id") or 0
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None or not hasattr(channel, "send"):
            return
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            logger.warning("temp voice: could not write to log channel %s in guild %s", channel_id, guild.id)

    async def _create_temp_channel(self, member: discord.Member, hub: discord.VoiceChannel):
        guild = member.guild
        db = self.bot.db
        settings = db.get_temp_voice_settings(guild.id)

        # One live channel per person: send them back to it instead of making
        # another, so nobody can fill the server with channels.
        existing_id = db.find_temp_voice_by_owner(guild.id, member.id)
        if existing_id is not None:
            existing = guild.get_channel(existing_id)
            if isinstance(existing, discord.VoiceChannel):
                try:
                    await member.move_to(existing)
                except discord.HTTPException:
                    pass
                return
            await self._forget_channel(guild, existing_id)

        now = time.monotonic()
        if now - self._last_create.get(member.id, -1e9) < CREATE_COOLDOWN_SECONDS:
            return
        self._last_create[member.id] = now
        if len(self._last_create) > 1000:
            self._last_create = {k: v for k, v in self._last_create.items() if now - v < CREATE_COOLDOWN_SECONDS}

        if db.count_temp_voice_channels(guild.id) >= settings["max_channels"]:
            await self._log(guild, settings, f"⚠️ {member.mention} tried to create a voice channel but the server limit ({settings['max_channels']}) is reached.")
            return

        name = channel_name_for(settings["name_template"], member.display_name)
        hub_limit = db.get_voice_hub_limit(guild.id, hub.id)
        try:
            temp_channel = await guild.create_voice_channel(name=name, category=hub.category, user_limit=hub_limit)
        except discord.Forbidden:
            logger.warning("missing permissions to create temp voice channel in guild %s", guild.id)
            return
        except discord.HTTPException:
            logger.exception("failed to create temp voice channel for %s", member.id)
            return

        # Record the channel the moment it exists. Anything that fails after
        # this point leaves a row the startup sweep can find - previously the
        # row was only written at the very end, so a failed permission edit or
        # move left an untracked, permanently orphaned voice channel in Discord.
        db.add_temp_voice_channel(guild.id, temp_channel.id, member.id, hub_limit)

        try:
            await temp_channel.set_permissions(member, overwrite=build_owner_overwrite(settings["owner_perms"]))
            await member.move_to(temp_channel)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning(
                "temp voice: setup failed after creating channel %s in guild %s (%s) - removing it",
                temp_channel.id, guild.id, exc,
            )
            # Nobody is in it and the member never got moved, so take it back
            # down rather than leaving an empty stray channel behind. If the
            # delete itself fails, the DB row (written above) means the startup
            # sweep will clear it later.
            await self._delete_temp_channel(temp_channel)
            return

        role_note = ""
        if settings["create_role"]:
            try:
                role = await guild.create_role(
                    name=f"VC: {member.display_name}"[:100], hoist=settings["role_hoist"],
                    permissions=discord.Permissions.none(), reason="Temporary voice channel role",
                )
                await member.add_roles(role, reason="Temporary voice channel creator")
                db.set_temp_voice_role(temp_channel.id, role.id)
                role_note = f" with role {role.name}"
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("temp voice: could not create the creator role in guild %s", guild.id)
        await self._log(
            guild, settings,
            f"🎙️ {member.mention} created **{temp_channel.name}**{role_note} "
            f"(can: {', '.join(owner_perm_names(settings['owner_perms'])) or 'nothing extra'}).",
        )

    async def _forget_channel(self, guild: discord.Guild, channel_id: int) -> None:
        """Drop a channel's database row and delete its temporary role."""
        info = self.bot.db.get_temp_voice_info(channel_id)
        self.bot.db.remove_temp_voice_channel(channel_id)
        if info and info["role_id"]:
            role = guild.get_role(info["role_id"])
            if role is not None:
                try:
                    await role.delete(reason="Temporary voice channel removed")
                except discord.HTTPException:
                    logger.warning("temp voice: could not delete role %s in guild %s", role.id, guild.id)

    async def _delete_temp_channel(self, channel: discord.VoiceChannel) -> bool:
        try:
            await channel.delete(reason="Temp voice channel emptied")
        except discord.NotFound:
            # It is already gone, so its database record can safely disappear.
            await self._forget_channel(channel.guild, channel.id)
            return True
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("failed to delete temp voice channel %s", channel.id)
            return False

        await self._forget_channel(channel.guild, channel.id)
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
                    await self._forget_channel(guild, channel_id)
                elif len(channel.members) == 0:
                    await self._delete_temp_channel(channel)


async def setup(bot: commands.Bot):
    await bot.add_cog(TempVoice(bot))
