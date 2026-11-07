"""Modmail - lets a member DM the bot directly instead of navigating to a
ticket panel inside the server. The bot relays the DM into a private
channel for staff, staff reply in that channel like normal chat, and the
bot relays those replies back to the user as DMs. Closing/blocking is
staff-only.

New channels intentionally don't get explicit permission overwrites - they
inherit whatever's set on the configured category, so keeping that
category staff-only is what keeps modmail threads private. This mirrors
how /setuptickets' category works.

Since DMs have no guild context, a returning DM either goes to the one
open thread the user already has, or - for a new thread - to whichever
mutual server has modmail enabled. If more than one does, the user is
asked to pick.
"""
import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands

from utils import manager_or_permission


def _channel_safe_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")
    return cleaned or "member"


class Modmail(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---- DM side ----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if message.guild is None:
            await self._handle_dm(message)
        else:
            await self._handle_guild_reply(message)

    async def _handle_dm(self, message: discord.Message) -> None:
        existing = self.bot.db.get_open_modmail_thread_for_user(message.author.id)
        if existing is not None:
            _id, guild_id, channel_id, _user_id, _status, _created_at = existing
            guild = self.bot.get_guild(guild_id)
            channel = guild.get_channel(channel_id) if guild else None
            if channel is not None:
                await self._relay_to_channel(channel, message)
                return
            # The channel's gone but the DB row never got closed - fall
            # through and let a fresh thread get created instead of
            # dropping the message.
            self.bot.db.close_modmail_thread(_id, None)

        candidates = [
            guild for guild in self.bot.guilds
            if guild.get_member(message.author.id) is not None
            and self.bot.db.get_modmail_config(guild.id)["enabled"]
            and not self.bot.db.is_modmail_blocked(guild.id, message.author.id)
        ]
        if not candidates:
            await message.channel.send("Modmail isn't set up on any server we share, or you're not able to use it there right now.")
            return

        guild = candidates[0]
        if len(candidates) > 1:
            guild = await self._ask_which_server(message, candidates)
            if guild is None:
                return

        await self._open_thread(guild, message)

    async def _ask_which_server(self, message: discord.Message, candidates: list[discord.Guild]) -> discord.Guild | None:
        options = "\n".join(f"{i+1}. {g.name}" for i, g in enumerate(candidates))
        await message.channel.send(f"You share more than one server with modmail set up - which one is this about? Reply with a number:\n{options}")

        def check(m: discord.Message) -> bool:
            return m.author.id == message.author.id and m.guild is None and m.content.strip().isdigit()

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=60)
        except asyncio.TimeoutError:
            await message.channel.send("Didn't get a pick in time - send your message again when you're ready.")
            return None

        index = int(reply.content.strip()) - 1
        if not (0 <= index < len(candidates)):
            await message.channel.send("That wasn't one of the numbers listed - send your message again to restart.")
            return None
        return candidates[index]

    async def _open_thread(self, guild: discord.Guild, message: discord.Message) -> None:
        cfg = self.bot.db.get_modmail_config(guild.id)
        category = guild.get_channel(cfg["category_id"]) if cfg["category_id"] else None
        if category is None or not isinstance(category, discord.CategoryChannel):
            await message.channel.send(f"Modmail is enabled on {guild.name} but its category isn't configured right - a staff member needs to check the dashboard.")
            return

        try:
            channel = await category.create_text_channel(
                name=f"modmail-{_channel_safe_name(message.author.name)}",
                reason=f"Modmail opened by {message.author} ({message.author.id})",
            )
        except discord.Forbidden:
            await message.channel.send(f"I don't have permission to open a modmail channel on {guild.name} - a staff member needs to check my permissions.")
            return
        except discord.HTTPException as exc:
            await message.channel.send(f"Something went wrong opening that on {guild.name}: {exc}")
            return

        self.bot.db.create_modmail_thread(guild.id, channel.id, message.author.id)

        account_age_days = (discord.utils.utcnow() - message.author.created_at).days
        header = discord.Embed(
            title="New modmail thread",
            description=f"{message.author.mention} ({message.author}) - account created {account_age_days} days ago.",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        header.set_footer(text=f"User ID: {message.author.id} · Reply here to respond · /modmail close when done")
        await channel.send(embed=header)
        await self._relay_to_channel(channel, message)

        await message.channel.send(f"Your message was sent to the staff of **{guild.name}**. They'll reply here.")

    async def _relay_to_channel(self, channel: discord.abc.Messageable, message: discord.Message) -> None:
        files = []
        for attachment in message.attachments[:10]:
            try:
                files.append(await attachment.to_file())
            except discord.HTTPException:
                pass
        content = message.content or ("*(no text - attachment only)*" if files else "*(empty message)*")
        await channel.send(f"**{message.author}:** {content}", files=files or None, allowed_mentions=discord.AllowedMentions.none())

    # ---- guild side ----

    async def _handle_guild_reply(self, message: discord.Message) -> None:
        if message.content.startswith(("/", "!")):
            return
        thread = self.bot.db.get_modmail_thread_by_channel(message.channel.id)
        if thread is None or thread[4] != "open":
            return

        _id, guild_id, _channel_id, user_id, _status, _created_at = thread
        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        cfg = self.bot.db.get_modmail_config(guild_id)
        sender_label = "Staff" if cfg["anonymous_staff"] else str(message.author)

        files = []
        for attachment in message.attachments[:10]:
            try:
                files.append(await attachment.to_file())
            except discord.HTTPException:
                pass

        try:
            await user.send(
                f"**{sender_label}:** {message.content}" if message.content else f"**{sender_label}** sent an attachment.",
                files=files or None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await message.channel.send("⚠️ Couldn't deliver that - they likely have DMs closed or have blocked the bot.")
            return
        await message.add_reaction("✅")

    # ---- staff commands ----

    modmail = app_commands.Group(name="modmail", description="Manage modmail threads")

    @modmail.command(name="close", description="Close the modmail thread in this channel")
    @app_commands.describe(reason="Optional reason shown to the member")
    @manager_or_permission("manage_guild")
    async def modmail_close(self, interaction: discord.Interaction, reason: str = None):
        thread = self.bot.db.get_modmail_thread_by_channel(interaction.channel.id)
        if thread is None or thread[4] != "open":
            await interaction.response.send_message("This isn't an open modmail channel.", ephemeral=True)
            return

        thread_id, _guild_id, _channel_id, user_id, _status, _created_at = thread
        self.bot.db.close_modmail_thread(thread_id, interaction.user.id)

        user = self.bot.get_user(user_id)
        if user is not None:
            try:
                closing_note = f" Reason: {reason}" if reason else ""
                await user.send(f"Your modmail thread on **{interaction.guild.name}** was closed.{closing_note} Send another message any time to open a new one.")
            except discord.Forbidden:
                pass

        try:
            await interaction.channel.edit(name=f"closed-{interaction.channel.name}"[:100])
        except (discord.Forbidden, discord.HTTPException):
            pass

        await interaction.response.send_message("Thread closed.")

    @modmail.command(name="block", description="Block a member from opening modmail threads")
    @manager_or_permission("manage_guild")
    async def modmail_block(self, interaction: discord.Interaction, user: discord.User, reason: str = None):
        self.bot.db.block_modmail_user(interaction.guild.id, user.id, interaction.user.id)
        await interaction.response.send_message(f"Blocked {user.mention} from opening modmail here." + (f" Reason: {reason}" if reason else ""))

    @modmail.command(name="unblock", description="Allow a previously blocked member to use modmail again")
    @manager_or_permission("manage_guild")
    async def modmail_unblock(self, interaction: discord.Interaction, user: discord.User):
        self.bot.db.unblock_modmail_user(interaction.guild.id, user.id)
        await interaction.response.send_message(f"Unblocked {user.mention}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Modmail(bot))
