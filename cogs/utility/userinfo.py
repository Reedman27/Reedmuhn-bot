"""User lookup - a single /whois command that surfaces the same basics
Discord's own profile popout shows (plus a couple of things it doesn't),
in one embed a moderator can pull up without leaving the channel.
"""
import discord
from discord import app_commands
from discord.ext import commands

# Presence-status colors, closest thing to a "vibe" for the embed without
# requiring anything from the database.
STATUS_COLORS = {
    discord.Status.online: discord.Color.green(),
    discord.Status.idle: discord.Color.gold(),
    discord.Status.dnd: discord.Color.red(),
    discord.Status.offline: discord.Color.greyple(),
}

# Public user flags worth calling out - staff/partner/verified-bot-developer
# badges rather than every internal flag Discord tracks.
BADGE_LABELS = {
    discord.UserFlags.staff: "Discord Staff",
    discord.UserFlags.partner: "Partner",
    discord.UserFlags.hypesquad: "HypeSquad Events",
    discord.UserFlags.hypesquad_bravery: "HypeSquad Bravery",
    discord.UserFlags.hypesquad_brilliance: "HypeSquad Brilliance",
    discord.UserFlags.hypesquad_balance: "HypeSquad Balance",
    discord.UserFlags.bug_hunter: "Bug Hunter",
    discord.UserFlags.bug_hunter_level_2: "Bug Hunter Level 2",
    discord.UserFlags.early_supporter: "Early Supporter",
    discord.UserFlags.verified_bot_developer: "Verified Bot Developer",
    discord.UserFlags.active_developer: "Active Developer",
}


class UserInfo(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="whois", description="Look up a member's account and server info")
    @app_commands.describe(user="Who to look up (defaults to you)")
    async def whois(self, interaction: discord.Interaction, user: discord.Member = None):
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return

        member: discord.Member = user or interaction.user

        badges = [label for flag, label in BADGE_LABELS.items() if flag in member.public_flags]

        # Prefer the member's own role color (the thing people actually
        # associate with them in the server) - only fall back to a
        # presence-based color for members with no colored role, where
        # member.color is just Discord's flat default grey.
        color = member.color if member.color != discord.Color.default() else STATUS_COLORS.get(member.status, discord.Color.blurple())

        embed = discord.Embed(
            title=str(member),
            description="Here's a look at their account and server info.",
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        embed.add_field(name="User ID", value=str(member.id), inline=True)
        embed.add_field(name="Mention", value=member.mention, inline=True)
        embed.add_field(name="Bot?", value="Yes" if member.bot else "No", inline=True)

        embed.add_field(
            name="Account Created",
            value=discord.utils.format_dt(member.created_at, style="R"),
            inline=True,
        )
        if member.joined_at:
            embed.add_field(
                name="Joined Server",
                value=discord.utils.format_dt(member.joined_at, style="R"),
                inline=True,
            )
        if member.premium_since:
            embed.add_field(
                name="Boosting Since",
                value=discord.utils.format_dt(member.premium_since, style="R"),
                inline=True,
            )

        if member.nick:
            embed.add_field(name="Nickname", value=member.nick, inline=True)

        # Skip the field entirely for the common case (no badges) instead of
        # cluttering every lookup with a "Badges: None" line - the field
        # only earns its place when there's actually something to show.
        if badges:
            embed.add_field(name="Badges", value=", ".join(badges), inline=False)

        # Roles excluding @everyone, highest first, capped so a member with
        # dozens of roles doesn't blow past the embed field limit.
        roles = [r for r in reversed(member.roles) if r != interaction.guild.default_role]
        if roles:
            shown = roles[:20]
            role_text = " ".join(r.mention for r in shown)
            if len(roles) > len(shown):
                role_text += f" …and {len(roles) - len(shown)} more"
            embed.add_field(name=f"Roles ({len(roles)})", value=role_text, inline=False)
        else:
            embed.add_field(name="Roles (0)", value="None", inline=False)

        embed.add_field(name="Top Role", value=member.top_role.mention, inline=True)
        embed.add_field(
            name="Server Owner?", value="Yes" if member.id == interaction.guild.owner_id else "No", inline=True
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(UserInfo(bot))
