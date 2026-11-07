"""/serverinfo - the numbers an admin usually wants at a glance: members
(people vs bots), channels, roles, boosts, owner and age of the server."""
import discord
from discord import app_commands
from discord.ext import commands


class ServerInfo(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="serverinfo", description="Member, bot, channel and boost counts for this server")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        guild = interaction.guild
        bots = sum(1 for m in guild.members if m.bot)
        total = guild.member_count or len(guild.members)
        embed = discord.Embed(title=guild.name, color=discord.Color.blurple())
        embed.add_field(name="Members", value=f"{total} total\n{max(total - bots, 0)} people, {bots} bots")
        embed.add_field(
            name="Channels",
            value=f"{len(guild.text_channels)} text\n{len(guild.voice_channels)} voice\n{len(guild.categories)} categories",
        )
        embed.add_field(name="Roles / Emoji", value=f"{len(guild.roles)} roles\n{len(guild.emojis)} emoji")
        embed.add_field(name="Boosts", value=f"Level {guild.premium_tier}\n{guild.premium_subscription_count or 0} boosts")
        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>")
        embed.add_field(name="Created", value=discord.utils.format_dt(guild.created_at, "D"))
        embed.set_footer(text=f"Verification: {guild.verification_level.name.replace('_', ' ').title()}")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot):
    await bot.add_cog(ServerInfo(bot))
