"""Reaction roles: an admin binds an emoji on a specific message to a role.
Members who react with that emoji get the role; removing the reaction takes
it back away. Implemented on the raw reaction events (not the cached
on_reaction_add/remove) so it still works for messages that predate the
bot's message cache or were sent before the bot last restarted - the same
reason discord.py bots generally prefer the raw variants for anything
reaction-role-like.
"""
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("reactionroles")

MESSAGE_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def parse_message_reference(raw: str) -> tuple[int | None, int | None]:
    """Accepts either a full Discord message link - from right-click ->
    Copy Message Link, which unlike Copy Message ID doesn't require
    Developer Mode - or a bare message ID for anyone who already has one.
    Returns (channel_id, message_id); channel_id is None when only a bare
    ID was given, since an ID alone doesn't carry a channel with it.
    Returns (None, None) if raw is neither.
    """
    raw = raw.strip()
    match = MESSAGE_LINK_RE.search(raw)
    if match:
        _guild_id, channel_id, message_id = match.groups()
        return int(channel_id), int(message_id)
    if raw.isdigit():
        return None, int(raw)
    return None, None


def resolve_emoji_key(raw: str) -> str | None:
    """Normalizes a user-typed emoji into the same string form
    str(payload.emoji) produces on incoming reaction events, so a stored
    binding and an incoming reaction can be compared with a plain string
    match. Returns None if the input isn't a parseable emoji at all (e.g.
    plain text typed into the option by mistake)."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        partial = discord.PartialEmoji.from_str(raw)
    except Exception:
        return None
    # from_str() happily accepts arbitrary text as a "unicode emoji" (it has
    # no way to validate real unicode emoji vs. random characters) - the
    # actual validation happens later when we try to react with it and
    # Discord's API either accepts or rejects it.
    return str(partial)


from utils import manager_or_permission

class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _fetch_target_message(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None, message_id: int
    ) -> discord.Message | None:
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message(
                "That needs to be a text channel.", ephemeral=True
            )
            return None
        try:
            return await target_channel.fetch_message(message_id)
        except discord.NotFound:
            await interaction.response.send_message(
                f"Couldn't find that message in {target_channel.mention}. Make sure you pasted the "
                "right link (right-click the message -> Copy Message Link) or that the channel "
                "argument matches where it actually is.",
                ephemeral=True,
            )
            return None
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to read messages in {target_channel.mention}.",
                ephemeral=True,
            )
            return None

    @app_commands.command(name="addreactionrole", description="React to a message with an emoji to give/remove a role")
    @app_commands.describe(
        message="Paste the message link (right-click the message -> Copy Message Link), or its ID",
        emoji="The emoji members will react with",
        role="Role to give when someone reacts, and remove when they un-react",
        channel="Channel the message is in - only needed if you pasted an ID instead of a link (defaults to this channel)",
    )
    @manager_or_permission("manage_roles")
    async def addreactionrole(
        self,
        interaction: discord.Interaction,
        message: str,
        emoji: str,
        role: discord.Role,
        channel: discord.TextChannel | None = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        link_channel_id, parsed_message_id = parse_message_reference(message)
        if parsed_message_id is None:
            await interaction.response.send_message(
                "That doesn't look like a message link or ID. Right-click the message and choose "
                "Copy Message Link.",
                ephemeral=True,
            )
            return

        # Giving out a role the bot can't actually grant would silently do
        # nothing later when someone reacts - catch it up front instead.
        if role.position >= interaction.guild.me.top_role.position:
            await interaction.response.send_message(
                f"I can't assign {role.mention} - it's above (or equal to) my highest role. "
                "Move my role above it in Server Settings > Roles.",
                ephemeral=True,
            )
            return
        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "That role can't be used for reaction roles (it's either @everyone or managed by an integration/bot).",
                ephemeral=True,
            )
            return

        target_channel: discord.TextChannel | discord.Thread | None
        if link_channel_id is not None:
            resolved = interaction.guild.get_channel_or_thread(link_channel_id)
            if resolved is None:
                await interaction.response.send_message(
                    "I can't find the channel that message link points to.", ephemeral=True
                )
                return
            target_channel = resolved
        else:
            target_channel = channel

        message_obj = await self._fetch_target_message(interaction, target_channel, parsed_message_id)
        if message_obj is None:
            return  # _fetch_target_message already responded

        emoji_key = resolve_emoji_key(emoji)
        if emoji_key is None:
            await interaction.response.send_message("That doesn't look like a valid emoji.", ephemeral=True)
            return

        try:
            await message_obj.add_reaction(emoji_key)
        except discord.HTTPException:
            await interaction.response.send_message(
                "I couldn't react with that emoji - if it's a custom emoji, make sure it's from "
                "this server (or one I'm also in).",
                ephemeral=True,
            )
            return

        self.bot.db.add_reaction_role(interaction.guild.id, message_obj.id, message_obj.channel.id, emoji_key, role.id)
        await interaction.response.send_message(
            f"Done - reacting {emoji_key} on [that message]({message_obj.jump_url}) now gives {role.mention}."
        )

    @app_commands.command(name="removereactionrole", description="Remove a reaction role binding from a message")
    @app_commands.describe(
        message="The message link or ID the reaction role is on",
        emoji="The emoji to unbind",
    )
    @manager_or_permission("manage_roles")
    async def removereactionrole(self, interaction: discord.Interaction, message: str, emoji: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        _link_channel_id, parsed_message_id = parse_message_reference(message)
        if parsed_message_id is None:
            await interaction.response.send_message("That doesn't look like a message link or ID.", ephemeral=True)
            return

        emoji_key = resolve_emoji_key(emoji)
        if emoji_key is None:
            await interaction.response.send_message("That doesn't look like a valid emoji.", ephemeral=True)
            return

        removed = self.bot.db.remove_reaction_role(interaction.guild.id, parsed_message_id, emoji_key)
        await interaction.response.send_message(
            "Removed - that reaction no longer gives a role." if removed
            else "No reaction role found for that message and emoji."
        )

    @app_commands.command(name="listreactionroles", description="List this server's reaction role bindings")
    @manager_or_permission("manage_roles")
    async def listreactionroles(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        rows = self.bot.db.list_reaction_roles(interaction.guild.id)
        if not rows:
            await interaction.response.send_message("No reaction roles configured yet.", ephemeral=True)
            return

        lines = []
        for msg_id, chan_id, emoji, role_id in rows:
            jump_url = f"https://discord.com/channels/{interaction.guild.id}/{chan_id}/{msg_id}"
            lines.append(f"{emoji} -> <@&{role_id}> on [message]({jump_url})")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction_change(payload, giving=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction_change(payload, giving=False)

    async def _handle_reaction_change(self, payload: discord.RawReactionActionEvent, giving: bool) -> None:
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return  # DM reaction, or the bot's own initial reaction on the message

        role_id = self.bot.db.get_reaction_role(payload.guild_id, payload.message_id, str(payload.emoji))
        if role_id is None:
            return  # this emoji/message combo isn't a configured reaction role

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(role_id)
        if role is None:
            return  # role was deleted since the binding was created

        # payload.member is only populated on reaction ADD, not remove.
        member = payload.member or guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return  # they left the server
        if member.bot:
            return

        try:
            if giving:
                await member.add_roles(role, reason="Reaction role")
            else:
                await member.remove_roles(role, reason="Reaction role removed")
        except discord.Forbidden:
            logger.warning(
                "missing permissions to change role %s for %s in guild %s (reaction role)",
                role_id, member.id, guild.id,
            )
        except discord.HTTPException:
            logger.exception("failed to change reaction role for %s in guild %s", member.id, guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReactionRoles(bot))
