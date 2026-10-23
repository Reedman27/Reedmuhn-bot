"""Numbered case/infraction system, layered on top of the existing
member_history log rather than duplicating it - every /warn, /kick, /mute,
and /tempban already writes to member_history (see moderation.py); this cog
just gives the moderation-relevant subset of those entries a sequential
per-guild case number and a set of commands to browse/manage them, plus a
separate private-notes system for staff observations that aren't tied to
any one incident.
"""
import time
import datetime

import discord
from discord import app_commands
from discord.ext import commands

from utils import manager_or_permission

CASE_ACTION_LABELS = {
    "warn": "Warn",
    "kick": "Kick",
    "mute": "Mute",
    "tempban": "Temporary Ban",
    "timeout": "Timeout",
}


def _case_line(case_number: int, event_type: str, actor_id, reason: str, created_at: int, voided: bool) -> str:
    when = time.strftime("%Y-%m-%d", time.localtime(created_at))
    label = CASE_ACTION_LABELS.get(event_type, event_type.replace("_", " ").title())
    actor = f"<@{actor_id}>" if actor_id else "the system"
    voided_tag = " `[VOIDED]`" if voided else ""
    return f"`#{case_number}` **{label}**{voided_tag} - {when} by {actor}: {reason or 'No reason given'}"


class Cases(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    case = app_commands.Group(name="case", description="View and manage moderation cases")
    notes = app_commands.Group(name="notes", description="Private staff notes on a member")

    # ---- /case ----

    @case.command(name="view", description="View a single case by number")
    @app_commands.describe(case_number="The case number, e.g. 12")
    @manager_or_permission("moderate_members")
    async def case_view(self, interaction: discord.Interaction, case_number: int):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        row = self.bot.db.get_case(interaction.guild.id, case_number)
        if row is None:
            await interaction.response.send_message(f"No case #{case_number} here.", ephemeral=True)
            return

        _id, user_id, event_type, actor_id, reason, details, created_at, voided = row
        label = CASE_ACTION_LABELS.get(event_type, event_type.replace("_", " ").title())

        embed = discord.Embed(
            title=f"Case #{case_number}{' [VOIDED]' if voided else ''}",
            description=reason or "No reason given",
            color=discord.Color.red() if voided else discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Action", value=label, inline=True)
        embed.add_field(name="Member", value=f"<@{user_id}>", inline=True)
        embed.add_field(name="Moderator", value=f"<@{actor_id}>" if actor_id else "System", inline=True)
        embed.add_field(name="Date", value=discord.utils.format_dt(datetime.datetime.fromtimestamp(created_at, tz=datetime.timezone.utc), style="F"), inline=False)
        if details:
            embed.add_field(name="Details", value=details, inline=False)

        await interaction.response.send_message(embed=embed)

    @case.command(name="search", description="List a member's cases")
    @app_commands.describe(user="Whose cases to list")
    @manager_or_permission("moderate_members")
    async def case_search(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        rows = self.bot.db.list_cases_for_user(interaction.guild.id, user.id, limit=25)
        if not rows:
            await interaction.response.send_message(f"{user.mention} has no cases.")
            return

        lines = [_case_line(*row) for row in rows]
        active = self.bot.db.count_active_cases_for_user(interaction.guild.id, user.id)
        embed = discord.Embed(
            title=f"Cases for {user.display_name}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text=f"{active} active case{'s' if active != 1 else ''}")
        await interaction.response.send_message(embed=embed)

    @case.command(name="edit", description="Edit a case's reason")
    @app_commands.describe(case_number="The case number", reason="The new reason")
    @manager_or_permission("moderate_members")
    async def case_edit(self, interaction: discord.Interaction, case_number: int, reason: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        ok = self.bot.db.edit_case_reason(interaction.guild.id, case_number, reason)
        if not ok:
            await interaction.response.send_message(f"No case #{case_number} here.", ephemeral=True)
            return
        await interaction.response.send_message(f"Updated case #{case_number}'s reason to: {reason}")

    @case.command(name="delete", description="Void a case (kept for audit purposes, excluded from active counts)")
    @app_commands.describe(case_number="The case number to void")
    @manager_or_permission("moderate_members")
    async def case_delete(self, interaction: discord.Interaction, case_number: int):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        ok = self.bot.db.void_case(interaction.guild.id, case_number, True)
        if not ok:
            await interaction.response.send_message(f"No case #{case_number} here.", ephemeral=True)
            return
        await interaction.response.send_message(f"Voided case #{case_number}. It's kept on record but no longer counts as active.")

    # ---- /history ----

    @app_commands.command(name="history", description="Quick view of a member's case history")
    @app_commands.describe(user="Whose history to check")
    @manager_or_permission("moderate_members")
    async def history(self, interaction: discord.Interaction, user: discord.Member):
        await self.case_search.callback(self, interaction, user)

    # ---- /notes ----

    @notes.command(name="add", description="Add a private staff note about a member")
    @app_commands.describe(user="Who the note is about", note="The note text")
    @manager_or_permission("moderate_members")
    async def notes_add(self, interaction: discord.Interaction, user: discord.Member, note: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        self.bot.db.add_mod_note(interaction.guild.id, user.id, interaction.user.id, note)
        await interaction.response.send_message(f"Noted about {user.mention}.", ephemeral=True)

    @notes.command(name="view", description="View private staff notes about a member")
    @app_commands.describe(user="Whose notes to view")
    @manager_or_permission("moderate_members")
    async def notes_view(self, interaction: discord.Interaction, user: discord.Member):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return

        rows = self.bot.db.list_mod_notes(interaction.guild.id, user.id)
        if not rows:
            await interaction.response.send_message(f"No notes on {user.mention}.", ephemeral=True)
            return

        lines = []
        for _note_id, moderator_id, note, created_at in rows:
            when = time.strftime("%Y-%m-%d", time.localtime(created_at))
            lines.append(f"{when} by <@{moderator_id}>: {note}")

        embed = discord.Embed(
            title=f"Notes on {user.display_name}",
            description="\n".join(lines),
            color=discord.Color.dark_gray(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Cases(bot))
