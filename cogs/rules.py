"""Numbered server rules. An admin defines them with /addrule; anyone can
list them with /rules; /warn can optionally cite one by number so a
warning's reason carries "which rule" alongside the free-text explanation.

A rule's displayed number is just its position in the list (see
db.list_rules) rather than a stored field, so deleting rule #2 naturally
renumbers what was #3 down to #2 instead of leaving a gap - simpler than
keeping a position column in sync, and fine at the scale a rules list
runs at (tens of rules, not thousands).
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils import manager_or_permission


class Rules(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    rule = app_commands.Group(name="rule", description="Server rules")

    @rule.command(name="list", description="List this server's rules")
    async def rules(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        rules = self.bot.db.list_rules(interaction.guild.id)
        if not rules:
            await interaction.response.send_message("No rules have been set for this server yet.")
            return
        lines = [f"**{idx}.** {text}" for idx, (_rule_id, text) in enumerate(rules, start=1)]
        embed = discord.Embed(
            title=f"Rules for {interaction.guild.name}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @rule.command(name="add", description="Add a numbered server rule")
    @app_commands.describe(text="The rule's text")
    @manager_or_permission("manage_guild")
    async def addrule(self, interaction: discord.Interaction, text: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        if len(text) > 500:
            await interaction.response.send_message("Keep a rule under 500 characters.", ephemeral=True)
            return
        self.bot.db.add_rule(interaction.guild.id, text)
        count = len(self.bot.db.list_rules(interaction.guild.id))
        await interaction.response.send_message(f"Added as rule #{count}.")

    @rule.command(name="remove", description="Remove a server rule by its number")
    @app_commands.describe(number="The rule's number, as shown in /rule list")
    @manager_or_permission("manage_guild")
    async def removerule(self, interaction: discord.Interaction, number: int):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        found = self.bot.db.get_rule_by_number(interaction.guild.id, number)
        if found is None:
            await interaction.response.send_message(f"There's no rule #{number}.", ephemeral=True)
            return
        self.bot.db.delete_rule(interaction.guild.id, found[0])
        await interaction.response.send_message(f"Removed rule #{number}. Later rules have shifted down by one.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Rules(bot))
