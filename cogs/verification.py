"""Button-based verification gate. An admin points /setverification at a
channel and a role; the bot posts a persistent embed with a Verify button
there. Clicking it grants the configured role - no math captcha, just
enough friction to filter out the crudest bot-join spam, same tradeoff
Carl-bot's simplest verification mode makes.

The WebUI can configure this too, but posting/editing the actual Discord
message needs a live gateway connection the dashboard process doesn't have,
so WebUI saves queue a request here (same queue/claim/complete pattern used
throughout this codebase - see dashboardmoderation.py) and the bot posts or
edits the message on its next poll tick.
"""
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import manager_or_permission

logger = logging.getLogger("verification")


class VerifyView(discord.ui.View):
    """One persistent button. custom_id is static (not per-guild) because
    the guild is available from the interaction itself - registered once in
    cog_load via bot.add_view() so it keeps working across bot restarts
    without needing to re-fetch or re-send the message."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Verify", style=discord.ButtonStyle.success, custom_id="verification:verify_click", emoji="✅")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        cfg = self.bot.db.get_verification_config(interaction.guild.id)
        if not cfg["enabled"] or not cfg["role_id"]:
            await interaction.response.send_message("Verification isn't set up on this server right now.", ephemeral=True)
            return
        role = interaction.guild.get_role(cfg["role_id"])
        if role is None:
            await interaction.response.send_message("The verification role no longer exists - let a mod know.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("You're already verified.", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Self-verified via verification button")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I couldn't give you that role - my role needs to be above it. Let a mod know.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Discord rejected that: {exc}", ephemeral=True)
            return
        self.bot.db.record_member_history(interaction.guild.id, interaction.user.id, "verify", interaction.user.id, "Self-verified")
        await interaction.response.send_message("You're verified! Welcome to the server. 🎉", ephemeral=True)
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is not None:
            embed = discord.Embed(
                description=f"**Verified** - {interaction.user.mention} ({interaction.user})",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow(),
            )
            await logging_cog.log_event(interaction.guild, "members", embed)


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_verify_post_requests.start()

    def cog_unload(self):
        self.poll_verify_post_requests.cancel()

    async def cog_load(self):
        # Static custom_id, no message binding needed - this one view
        # handles the button on every guild's verify message.
        self.bot.add_view(VerifyView(self.bot))

    async def _post_or_update_message(self, guild_id: int) -> str | None:
        """Posts the verify embed+button, or edits the existing one in
        place if this guild already has one. Returns an error string on
        failure, or None on success."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return "The bot is no longer in that server."
        cfg = self.bot.db.get_verification_config(guild_id)
        if not cfg["channel_id"] or not cfg["role_id"]:
            return "Set a channel and role first."
        channel = guild.get_channel(cfg["channel_id"])
        if channel is None or not isinstance(channel, discord.TextChannel):
            return "The configured verification channel no longer exists."
        me = guild.me
        if me is not None and not channel.permissions_for(me).send_messages:
            return "The bot can't send messages in that channel."

        embed = discord.Embed(
            title="Verification",
            description=cfg["message"] or "Click the button below to verify.",
            color=discord.Color.blurple(),
        )
        view = VerifyView(self.bot)

        if cfg["message_id"]:
            try:
                message = await channel.fetch_message(cfg["message_id"])
                await message.edit(embed=embed, view=view)
                return None
            except (discord.NotFound, discord.Forbidden):
                pass  # original message is gone - fall through and post a new one

        try:
            message = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return "The bot can't send messages in that channel."
        except discord.HTTPException as exc:
            return f"Discord rejected the message: {exc}"
        self.bot.db.set_verification_message_id(guild_id, message.id)
        return None

    @tasks.loop(seconds=2)
    async def poll_verify_post_requests(self):
        try:
            requests = self.bot.db.claim_verify_post_requests(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI verify-post requests")
            return
        for request_id, guild_id in requests:
            try:
                error = await self._post_or_update_message(guild_id)
                self.bot.db.complete_verify_post(request_id, error)
            except Exception as exc:
                logger.exception("WebUI verify-post request %s failed", request_id)
                self.bot.db.complete_verify_post(request_id, str(exc)[:500])

    @poll_verify_post_requests.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    verification = app_commands.Group(name="verification", description="Member verification gate")

    @verification.command(name="set", description="Configure the verification button (channel + role members get on click)")
    @app_commands.describe(channel="Where to post the Verify button", role="Role given when someone verifies",
                            message="Text shown above the button")
    @manager_or_permission("manage_guild")
    async def setverification(
        self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role,
        message: str = "Click the button below to verify and unlock the rest of the server.",
    ):
        me = interaction.guild.me
        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "That role can't be used for verification. Choose a normal, non-managed role.", ephemeral=True
            )
            return
        if me is not None and role >= me.top_role:
            await interaction.response.send_message(
                "I can't assign that role - my role needs to be above it in the role list.", ephemeral=True
            )
            return
        self.bot.db.set_verification_config(interaction.guild.id, True, channel.id, role.id, message)
        await interaction.response.defer(ephemeral=True)
        error = await self._post_or_update_message(interaction.guild.id)
        if error:
            await interaction.followup.send(f"Saved, but couldn't post the message: {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"Verification is set up in {channel.mention}.", ephemeral=True)

    @verification.command(name="off", description="Turn off the verification button")
    @manager_or_permission("manage_guild")
    async def verificationoff(self, interaction: discord.Interaction):
        cfg = self.bot.db.get_verification_config(interaction.guild.id)
        self.bot.db.set_verification_config(interaction.guild.id, False, cfg["channel_id"], cfg["role_id"], cfg["message"])
        await interaction.response.send_message("Verification is now off. The button will tell people it's disabled if clicked.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Verification(bot))
