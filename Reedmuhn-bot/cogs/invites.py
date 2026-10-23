"""Invite tracking - attributes each join to whichever invite code was
used. Discord's API doesn't tell you directly, so the standard technique
is to snapshot every invite's use count, wait for a join, take a fresh
snapshot, and see whose count went up. Vanity URLs are included in the
snapshot too since they don't show up in guild.invites() otherwise, but
they have no inviter to credit.

The naive version of that assumes one join == exactly one observed
use-count increment, which breaks during bursts: three members joining in
the same second can all be processed after Discord has already reported
+3 on one code, so the first join takes the whole delta and the other two
look like "no invite changed" and become unknown. This version keeps a
short-lived ledger of *unclaimed* invite uses per guild instead: each
snapshot adds whatever new uses it observed to the ledger, and each join
consumes exactly one use from it. A +3 delta therefore feeds three joins.

Ambiguity is still never resolved by guessing - if two different codes
have unclaimed uses when a join is processed, there is genuinely no way to
know which member used which, so the join is recorded as unknown and the
ledger is left alone. Unclaimed uses expire after a short window so a
stale delta can't be credited to an unrelated join minutes later.

Needs Manage Server on the bot to list invites in the first place - if
that's missing, joins are logged as plain "unknown" rather than attributed
to anything, same principle as the security scanner not making up findings
it can't back with real data. Joins that happen while the bot is offline
can't be reconstructed from Discord's API at all (there's no historical
"before" count), so they stay unknown too and earn no milestones.
"""
import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import utils
from utils import manager_or_permission

logger = logging.getLogger("invites")

# How long an observed-but-unclaimed invite use stays creditable. Long enough
# to cover a burst of joins landing out of order, short enough that a use from
# minutes ago never gets attached to an unrelated join.
PENDING_USE_TTL = 120


class InviteTracking(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> {invite_code: uses}
        self.invite_cache: dict[int, dict[str, int]] = {}
        # guild_id -> {invite_code: [timestamp, ...]} - observed invite uses
        # that no join has been credited with yet (one entry per use).
        self.pending_uses: dict[int, dict[str, list[float]]] = {}
        # guild_id -> {invite_code: inviter_id} from the last successful
        # snapshot, so attribution needs no second guild.invites() round trip.
        self.inviter_cache: dict[int, dict[str, int | None]] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}

    async def _snapshot(self, guild: discord.Guild) -> dict[str, int] | None:
        snap: dict[str, int] = {}
        inviters: dict[str, int | None] = {}
        try:
            for invite in await guild.invites():
                snap[invite.code] = invite.uses or 0
                inviters[invite.code] = invite.inviter.id if invite.inviter else None
        except (discord.Forbidden, discord.HTTPException):
            logger.info("invite tracking: can't list invites for guild %s (missing Manage Server?)", guild.id)
            return None
        if "VANITY_URL" in (guild.features or []):
            try:
                vanity = await guild.vanity_invite()
                if vanity is not None:
                    snap[vanity.code] = vanity.uses or 0
                    inviters[vanity.code] = None  # a vanity URL has no inviter to credit
            except (discord.Forbidden, discord.HTTPException):
                pass
        # Merge rather than replace: a code that hit its max uses and was
        # deleted between snapshots still needs its inviter for the join it
        # is about to be credited with.
        self.inviter_cache.setdefault(guild.id, {}).update(inviters)
        return snap

    def _record_deltas(self, guild_id: int, before: dict[str, int], after: dict[str, int], now: float) -> None:
        """Add every newly observed invite use to the guild's unclaimed ledger,
        one entry per use, so a delta bigger than 1 can feed several joins."""
        ledger = self.pending_uses.setdefault(guild_id, {})
        for code, uses in after.items():
            if code not in before:
                # A code the bot hadn't seen before (created while offline, or
                # this is the first snapshot). Its existing uses don't belong
                # to any join being handled now, so they don't enter the ledger.
                continue
            delta = uses - before[code]
            if delta > 0:
                ledger.setdefault(code, []).extend([now] * delta)

    def _expire_pending(self, guild_id: int, now: float) -> None:
        ledger = self.pending_uses.get(guild_id)
        if not ledger:
            return
        for code in list(ledger):
            fresh = [ts for ts in ledger[code] if now - ts <= PENDING_USE_TTL]
            if fresh:
                ledger[code] = fresh
            else:
                del ledger[code]

    def _claim_use(self, guild_id: int, now: float) -> tuple[str | None, bool]:
        """Consume one unclaimed invite use for a join.

        Returns (code, ambiguous). `code` is None when nothing is claimable, or
        when more than one code has unclaimed uses - in that case the ledger is
        left untouched and the join is recorded as unknown rather than guessed.
        """
        self._expire_pending(guild_id, now)
        ledger = self.pending_uses.get(guild_id, {})
        codes = [code for code, stamps in ledger.items() if stamps]
        if not codes:
            return None, False
        if len(codes) > 1:
            return None, True
        code = codes[0]
        ledger[code].pop(0)
        if not ledger[code]:
            del ledger[code]
        return code, False

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            snap = await self._snapshot(guild)
            if snap is not None:
                self.invite_cache[guild.id] = snap
                # Whatever happened while the bot was down isn't attributable
                # (no historical "before" count exists), so start clean.
                self.pending_uses.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        snap = await self._snapshot(guild)
        if snap is not None:
            self.invite_cache[guild.id] = snap
            self.pending_uses.pop(guild.id, None)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        self.invite_cache.setdefault(invite.guild.id, {})[invite.code] = invite.uses or 0
        self.inviter_cache.setdefault(invite.guild.id, {})[invite.code] = invite.inviter.id if invite.inviter else None

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        self.invite_cache.get(invite.guild.id, {}).pop(invite.code, None)
        # Unclaimed uses are deliberately kept: an invite can hit its max-uses
        # limit and be deleted by Discord in the same moment somebody joins
        # through it, and that join still deserves the credit.

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        lock = self._guild_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            now = time.time()
            before = self.invite_cache.get(guild.id, {})
            after = await self._snapshot(guild)
            if after is None:
                self.bot.db.record_invite_join(guild.id, member.id, None, None)
                self.bot.db.record_bot_event(
                    "member.invited", guild.id, None, member.id,
                    "invite_code=unknown;invite_snapshot_failed=true",
                )
                return
            self._record_deltas(guild.id, before, after, now)
            self.invite_cache[guild.id] = after

            # One join consumes one unclaimed use. A burst where Discord has
            # already reported +3 on a code leaves two uses in the ledger for
            # the next two joins instead of stranding them as unknown.
            used_code, ambiguous = self._claim_use(guild.id, now)

            inviter_id = self.inviter_cache.get(guild.id, {}).get(used_code) if used_code else None

            join_id = self.bot.db.record_invite_join(guild.id, member.id, inviter_id, used_code)
            details = f"invite_code={used_code or 'unknown'};invite_join_id={join_id}"
            if ambiguous:
                details += ";invite_attribution=ambiguous"
                logger.info(
                    "invite tracking: ambiguous attribution for join %s in guild %s - recorded as unknown",
                    member.id, guild.id,
                )
            self.bot.db.record_bot_event("member.invited", guild.id, inviter_id, member.id, details)

            if inviter_id:
                await self._check_milestones(guild, inviter_id)

    async def _check_milestones(self, guild: discord.Guild, inviter_id: int) -> None:
        """Grant every milestone role the inviter has reached.

        Idempotent by design: it re-checks all milestones at or below the
        current count rather than only the one just crossed, so a role that
        couldn't be granted earlier is picked up the next time this runs. Any
        grant Discord refuses is queued for retry (see utils) instead of only
        being logged - the invite count is permanent, so there may never be
        another milestone crossing to trigger a second attempt.
        """
        milestones = self.bot.db.list_invite_milestones(guild.id)
        if not milestones:
            return
        count = self.bot.db.count_invites_for_user(guild.id, inviter_id)
        member = guild.get_member(inviter_id)
        if member is None:
            return
        for invite_count, role_id in milestones:
            if count < invite_count:
                continue
            role = guild.get_role(role_id)
            if role is None:
                logger.warning(
                    "invite tracking: milestone role %s (at %s invites) no longer exists in guild %s",
                    role_id, invite_count, guild.id,
                )
                continue
            if role in member.roles:
                continue
            await utils.grant_role_reward(
                self.bot.db, member, role, f"Invite milestone: {invite_count} invites", "invite", logger,
            )

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

    @invites.command(name="sync-milestones", description="Re-check and re-apply invite milestone roles for a member")
    @app_commands.describe(user="Whose milestone roles to repair (defaults to you)")
    @manager_or_permission("manage_guild")
    async def invites_sync_milestones(self, interaction: discord.Interaction, user: discord.Member = None):
        """Manual repair path for milestone roles that failed when they were
        earned (wrong role hierarchy at the time, bot offline, and so on)."""
        target = user or interaction.user
        await interaction.response.defer(ephemeral=True)
        before = {r.id for r in target.roles}
        await self._check_milestones(interaction.guild, target.id)
        current = interaction.guild.get_member(target.id) or target
        granted = [r for r in current.roles if r.id not in before]
        if granted:
            await interaction.followup.send("Granted: " + ", ".join(r.mention for r in granted), ephemeral=True)
        else:
            await interaction.followup.send("No missing milestone roles to grant.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteTracking(bot))
