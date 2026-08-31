"""Invite tracking - attributes each join to whichever invite code was
used, the standard technique for this since Discord's API doesn't tell you
directly: snapshot every invite's use count, wait for a join, take a fresh
snapshot, and whichever code's count went up by one is the one that was
used. Vanity URLs are included in the snapshot too since they don't show
up in guild.invites() otherwise, but they have no inviter to credit.

Needs Manage Server on the bot to list invites in the first place - checks
that aren't the case just fail quietly and log a plain "unknown" join
rather than attributing anything, same principle as the security scanner
not making up findings it can't back with real data.
"""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import manager_or_permission

logger = logging.getLogger("invites")


class InviteTracking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> {invite_code: uses}
        self.invite_cache: dict[int, dict[str, int]] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}

    async def _snapshot(self, guild: discord.Guild) -> dict[str, int] | None:
        snap: dict[str, int] = {}
        try:
            for invite in await guild.invites():
                snap[invite.code] = invite.uses or 0
        except (discord.Forbidden, discord.HTTPException):
            logger.info("invite tracking: can't list invites for guild %s (missing Manage Server?)", guild.id)
            return None
        if "VANITY_URL" in (guild.features or []):
            try:
                vanity = await guild.vanity_invite()
                if vanity is not None:
                    snap[vanity.code] = vanity.uses or 0
            except (discord.Forbidden, discord.HTTPException):
                pass
        return snap

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            snap = await self._snapshot(guild)
            if snap is not None:
                self.invite_cache[guild.id] = snap

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        snap = await self._snapshot(guild)
        if snap is not None:
            self.invite_cache[guild.id] = snap

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        self.invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        self.invite_cache.get(invite.guild.id, {}).pop(invite.code, None)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        lock = self._guild_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            before = self.invite_cache.get(guild.id, {})
            after = await self._snapshot(guild)
            if after is None:
                self.bot.db.record_invite_join(guild.id, member.id, None, None)
                self.bot.db.record_bot_event(
                    "member.invited", guild.id, None, member.id,
                    "invite_code=unknown;invite_snapshot_failed=true",
                )
                return
            self.invite_cache[guild.id] = after

            changed = [(code, uses - before.get(code, 0)) for code, uses in after.items()
                       if uses > before.get(code, 0)]
            # The normal case is exactly one invite whose use count rose by one.
            # When Discord/API timing makes attribution ambiguous, don't guess the
            # inviter (a false reward is worse than an unknown join).
            exact = [code for code, delta in changed if delta == 1]
            used_code = exact[0] if len(exact) == 1 else (changed[0][0] if len(changed) == 1 else None)

            inviter_id = None
            if used_code:
                try:
                    match = next((i for i in await guild.invites() if i.code == used_code), None)
                    if match is not None and match.inviter is not None:
                        inviter_id = match.inviter.id
                except (discord.Forbidden, discord.HTTPException):
                    pass

            join_id = self.bot.db.record_invite_join(guild.id, member.id, inviter_id, used_code)
            self.bot.db.record_bot_event(
                "member.invited", guild.id, inviter_id, member.id,
                f"invite_code={used_code or 'unknown'};invite_join_id={join_id}",
            )

            if inviter_id:
                await self._check_milestones(guild, inviter_id)

    async def _check_milestones(self, guild: discord.Guild, inviter_id: int) -> None:
        milestones = self.bot.db.list_invite_milestones(guild.id)
        if not milestones:
            return
        count = self.bot.db.count_invites_for_user(guild.id, inviter_id)
        member = guild.get_member(inviter_id)
        if member is None:
            return
        # Award every milestone reached, not just the newest, in case the
        # bot missed some joins while offline and count jumped past one.
        for invite_count, role_id in milestones:
            if count < invite_count:
                continue
            role = guild.get_role(role_id)
            if role is None or role in member.roles:
                continue
            try:
                await member.add_roles(role, reason=f"Invite milestone: {invite_count} invites")
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("invite tracking: couldn't grant milestone role %s to %s in guild %s", role_id, inviter_id, guild.id)

    invites = app_commands.Group(name="invites", description="Check invite counts")

    @invites.command(name="check", description="Check how many members someone has invited")
    @app_commands.describe(user="Whose invite count to check (defaults to you)")
    async def invites_check(self, interaction: discord.Interaction, user: discord.Member = None):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        target = user or interaction.user
        count = self.bot.db.count_invites_for_user(interaction.guild.id, target.id)
        await interaction.response.send_message(f"{target.mention} has invited **{count}** member{'s' if count != 1 else ''} still tracked here.")

    @invites.command(name="leaderboard", description="Top inviters in this server")
    @manager_or_permission("manage_guild")
    async def invites_leaderboard(self, interaction: discord.Interaction):
        rows = self.bot.db.list_invite_leaderboard(interaction.guild.id, 10)
        if not rows:
            await interaction.response.send_message("No tracked invites yet.")
            return
        lines = [f"{i+1}. <@{inviter_id}> - {count}" for i, (inviter_id, count) in enumerate(rows)]
        embed = discord.Embed(title="🔗 Invite Leaderboard", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteTracking(bot))
