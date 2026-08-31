import io
import logging

import discord
from discord import app_commands
from discord.ext import commands

import welcome_card

logger = logging.getLogger("welcome")


from utils import format_welcome_message, manager_or_permission

class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="setwelcome",
        description="Set the welcome channel and message. {mention}, {server}, {server(members)} and more work too.",
    )
    @app_commands.describe(
        channel="Where to post welcome messages",
        message="e.g. Welcome {mention} to {server}! We're now {server(members)} strong.",
    )
    @manager_or_permission("manage_guild")
    async def setwelcome(self, interaction: discord.Interaction, channel: discord.TextChannel, message: str):
        self.bot.db.set_welcome(interaction.guild.id, channel.id, message)
        await interaction.response.send_message("Welcome message set.")

    @app_commands.command(name="setautorole", description="Set a role to automatically give new members")
    @app_commands.describe(role="Role to auto-assign")
    @manager_or_permission("manage_guild")
    async def setautorole(self, interaction: discord.Interaction, role: discord.Role):
        self.bot.db.set_autorole(interaction.guild.id, role.id)
        await interaction.response.send_message(
            "Autorole set. Make sure my role is above it in the role list."
        )

    @app_commands.command(name="welcomecard", description="Turn generated welcome card images on or off")
    @app_commands.describe(enabled="Show a generated image card alongside the welcome message")
    @manager_or_permission("manage_guild")
    async def welcomecard(self, interaction: discord.Interaction, enabled: bool):
        self.bot.db.set_welcome_card_enabled(interaction.guild.id, enabled)
        await interaction.response.send_message(f"Welcome card images are now {'on' if enabled else 'off'}.")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = self.bot.db.get_guild_config(member.guild.id)

        if cfg["welcome_channel_id"] and cfg["welcome_message"]:
            channel = member.guild.get_channel(cfg["welcome_channel_id"])
            if channel is not None:
                text = format_welcome_message(cfg["welcome_message"], member)

                file = None
                if cfg["welcome_card_enabled"]:
                    file = await self._build_welcome_card_file(member)

                await channel.send(text, file=file, allowed_mentions=discord.AllowedMentions(users=[member]))

        if cfg["autorole_id"]:
            role = member.guild.get_role(cfg["autorole_id"])
            if role is not None:
                try:
                    await member.add_roles(role, reason="Autorole")
                except discord.Forbidden:
                    logger.warning(
                        "autorole: missing permission to assign role %s in guild %s "
                        "(check the bot's role is above it in the role list)",
                        role.id, member.guild.id,
                    )
                except discord.HTTPException:
                    logger.exception("autorole: failed to assign role %s to %s", role.id, member.id)

    async def _build_welcome_card_file(self, member: discord.Member) -> discord.File | None:
        """Fetches the member's avatar and renders a welcome card image.
        Returns None (rather than raising) on any failure - a missing image
        shouldn't take down the whole welcome message."""
        try:
            avatar_bytes = await member.display_avatar.replace(size=256, format="png").read()
            png_bytes = welcome_card.render_welcome_card(
                avatar_bytes=avatar_bytes,
                member_name=member.display_name,
                server_name=member.guild.name,
                member_count=member.guild.member_count,
            )
            return discord.File(io.BytesIO(png_bytes), filename="welcome.png")
        except Exception:
            logger.exception("failed to build welcome card for %s", member.id)
            return None


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
