"""Raid detection - watches join velocity (joins per rolling window) the
same way antinuke.py watches audit-log bursts, and reacts once a
configurable threshold is crossed within the window. Also checks account
age on new joiners during an active raid window, since raid accounts are
almost always created minutes or hours before they're used.

Deliberately configured through the WebUI (Moderation > Raid Detection)
rather than a pile of slash-command options - see /raidmode status for a
quick in-Discord check of what's configured and whether raid mode is
currently active.
"""
import logging
import time
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

from utils import manager_or_permission

logger = logging.getLogger("raiddetection")

ACTION_LABELS = {
    "alert": "Alert only - no automatic action taken",
    "kick_new": "Auto-kick new joiners with young accounts",
    "lockdown": "Lock all text channels",
}


class RaidDetection(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> deque[timestamp] of joins in the current window
        self.join_times: dict[int, deque] = defaultdict(deque)
        # guild_id -> unix timestamp raid mode stays active until (extended
        # on every further join while it's active, so a raid that keeps
        # trickling in doesn't get treated as "over" the moment the burst
        # that first tripped it ages out of the window)
        self.raid_until: dict[int, float] = {}

    raidmode = app_commands.Group(name="raidmode", description="Check the raid protection system")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        cfg = self.bot.db.get_raid_config(guild.id)
        if not cfg["enabled"]:
            return

        now = time.time()
        window = self.join_times[guild.id]
        window.append(now)
        cutoff = now - cfg["window_seconds"]
        while window and window[0] < cutoff:
            window.popleft()

        was_active = self.raid_until.get(guild.id, 0) > now
        newly_triggered = not was_active and len(window) >= cfg["join_threshold"]

        if newly_triggered:
            self.raid_until[guild.id] = now + cfg["cooldown_seconds"]
            await self._trigger_alert(guild, cfg, len(window))
            if cfg["action"] == "lockdown":
                await self._apply_lockdown(guild)
        elif was_active:
            # Keep the raid window open as long as joins keep coming in.
            self.raid_until[guild.id] = now + cfg["cooldown_seconds"]

        raid_active = newly_triggered or was_active
        if raid_active and cfg["action"] == "kick_new":
            await self._maybe_kick_new_account(guild, member, cfg)

    async def _maybe_kick_new_account(self, guild: discord.Guild, member: discord.Member, cfg: dict) -> None:
        account_age_hours = (time.time() - member.created_at.timestamp()) / 3600
        if account_age_hours >= cfg["new_account_hours"]:
            return
        try:
            await member.kick(reason=f"Raid protection: account created {account_age_hours:.1f}h ago")
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("raid detection: failed to kick %s in guild %s", member.id, guild.id)
            return
        self.bot.db.record_member_history(
            guild.id, member.id, "raid_kick", self.bot.user.id if self.bot.user else None,
            f"Account created {account_age_hours:.1f}h before joining during an active raid alert",
        )

    async def _apply_lockdown(self, guild: discord.Guild) -> None:
        emergency_cog = self.bot.get_cog("Emergency")
        if emergency_cog is None:
            logger.warning("raid detection: Emergency cog not loaded, can't lock down guild %s", guild.id)
            return
        try:
            await emergency_cog._lockdown(guild, self.bot.user.id if self.bot.user else 0)
        except Exception:
            logger.exception("raid detection: lockdown failed for guild %s", guild.id)

    async def _trigger_alert(self, guild: discord.Guild, cfg: dict, join_count: int) -> None:
        self.bot.db.record_raid_incident(guild.id, join_count, cfg["window_seconds"], cfg["action"])

        embed = discord.Embed(
            title="🚨 Possible raid detected",
            description=f"{join_count} members joined within {cfg['window_seconds']}s (threshold: {cfg['join_threshold']}).",
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Response", value=ACTION_LABELS.get(cfg["action"], cfg["action"]), inline=False)
        if cfg["action"] == "lockdown":
            embed.add_field(name="Note", value="Channels are now locked - use /unlock or the dashboard's Emergency page once this settles.", inline=False)

        channel = guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        if channel is not None:
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is not None:
            await logging_cog.log_event(guild, "moderation", embed)

    @raidmode.command(name="status", description="Check the raid detection system's current status")
    @manager_or_permission("manage_guild")
    async def raidmode_status(self, interaction: discord.Interaction):
        cfg = self.bot.db.get_raid_config(interaction.guild.id)
        active = self.raid_until.get(interaction.guild.id, 0) > time.time()

        embed = discord.Embed(title="Raid Detection", color=discord.Color.orange() if active else discord.Color.blurple())
        embed.add_field(name="Enabled", value="Yes" if cfg["enabled"] else "No", inline=True)
        embed.add_field(name="Currently Active", value="🚨 Yes" if active else "No", inline=True)
        embed.add_field(name="Response", value=ACTION_LABELS.get(cfg["action"], cfg["action"]), inline=False)
        embed.add_field(name="Trigger", value=f"{cfg['join_threshold']} joins / {cfg['window_seconds']}s", inline=True)
        if cfg["action"] == "kick_new":
            embed.add_field(name="Account age cutoff", value=f"{cfg['new_account_hours']}h", inline=True)
        embed.set_footer(text="Configure this from the dashboard's Moderation > Raid Detection page.")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidDetection(bot))
