import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import manager_or_permission

logger = logging.getLogger("stickyroles")


class StickyRoles(commands.Cog):
    """Persist selected member roles across leaves/rejoins.

    Only roles the bot can currently manage are saved/restored. Administrators
    can explicitly exclude roles (for example Staff/Admin roles) so they are
    never restored by this feature.
    """

    sticky = app_commands.Group(
        name="stickyroles",
        description="Configure roles that persist when members leave and rejoin.",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @sticky.command(name="enable", description="Enable sticky roles for this server")
    @manager_or_permission("manage_guild")
    async def enable(self, interaction: discord.Interaction):
        self.bot.db.set_sticky_roles_enabled(interaction.guild.id, True)
        await interaction.response.send_message(
            "Sticky roles are now **enabled**. Members' manageable roles will be "
            "saved when they leave and restored when they rejoin."
        )

    @sticky.command(name="disable", description="Disable sticky roles for this server")
    @manager_or_permission("manage_guild")
    async def disable(self, interaction: discord.Interaction):
        self.bot.db.set_sticky_roles_enabled(interaction.guild.id, False)
        await interaction.response.send_message("Sticky roles are now **disabled**.")

    @sticky.command(name="exclude", description="Never restore a specific role with sticky roles")
    @app_commands.describe(role="Role that sticky roles must never restore")
    @manager_or_permission("manage_guild")
    async def exclude(self, interaction: discord.Interaction, role: discord.Role):
        self.bot.db.add_sticky_role_exclusion(interaction.guild.id, role.id)
        await interaction.response.send_message(
            f"{role.mention} is now **excluded** from sticky roles and will never be restored."
        )

    @sticky.command(name="include", description="Allow a previously excluded role to be restored")
    @app_commands.describe(role="Role to remove from the sticky-role exclusion list")
    @manager_or_permission("manage_guild")
    async def include(self, interaction: discord.Interaction, role: discord.Role):
        self.bot.db.remove_sticky_role_exclusion(interaction.guild.id, role.id)
        await interaction.response.send_message(
            f"{role.mention} is no longer excluded from sticky roles."
        )

    @sticky.command(name="list", description="Show roles excluded from sticky roles")
    @manager_or_permission("manage_guild")
    async def list_excluded(self, interaction: discord.Interaction):
        role_ids = self.bot.db.list_sticky_role_exclusions(interaction.guild.id)
        roles = [interaction.guild.get_role(rid) for rid in role_ids]
        roles = [role.mention for role in roles if role is not None]
        if not roles:
            text = "No roles are excluded."
        else:
            text = "Excluded roles:\n" + "\n".join(f"• {role}" for role in roles)
        await interaction.response.send_message(text, ephemeral=True)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = self.bot.db.get_guild_config(member.guild.id)
        if not cfg["sticky_roles_enabled"]:
            return

        excluded = set(self.bot.db.list_sticky_role_exclusions(member.guild.id))
        bot_member = member.guild.me
        if bot_member is None:
            return

        # Never persist @everyone, managed integration/bot roles, excluded
        # roles, or roles above/equal to the bot's highest role.
        role_ids = [
            role.id
            for role in member.roles
            if role.id != member.guild.id
            and not role.managed
            and role.id not in excluded
            and role < bot_member.top_role
        ]
        self.bot.db.set_sticky_roles(member.guild.id, member.id, role_ids)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        cfg = self.bot.db.get_guild_config(member.guild.id)
        if not cfg["sticky_roles_enabled"]:
            return

        saved = self.bot.db.get_sticky_roles(member.guild.id, member.id)
        if not saved:
            return

        excluded = set(self.bot.db.list_sticky_role_exclusions(member.guild.id))
        bot_member = member.guild.me
        if bot_member is None:
            return

        roles = []
        for role_id in saved:
            if role_id in excluded:
                continue
            role = member.guild.get_role(role_id)
            if (
                role is not None
                and not role.managed
                and role < bot_member.top_role
                and role not in member.roles
            ):
                roles.append(role)

        if roles:
            try:
                await member.add_roles(*roles, reason="Sticky roles restore")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("failed to restore sticky roles for member %s", member.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(StickyRoles(bot))
