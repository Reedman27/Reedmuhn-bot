import asyncio
import logging
import os
import random
import time


import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

import utils

logger = logging.getLogger(__name__)

XP_COOLDOWN = 60
XP_PER_MESSAGE = 10
DAILY_AMOUNT = 250
DAILY_STREAK_BONUS = 25       # extra coins per consecutive daily streak day, capped below
DAILY_STREAK_BONUS_CAP = 500  # streak bonus never adds more than this on top of DAILY_AMOUNT
DAILY_STREAK_GRACE = 48 * 3600  # claim again within this long to keep the streak alive
WORK_COOLDOWN = 3600
WORK_MIN, WORK_MAX = 20, 80
WORK_FLAVOR = [
    "🛠️ You fixed some bugs for a local business.",
    "🍔 You flipped burgers all shift.",
    "📦 You delivered packages across town.",
    "🎨 You sold a few doodles at the market.",
    "🐕 You walked every dog on the block.",
    "🧹 You cleaned up after a big event.",
]


class Extras(commands.Cog):
    """XP, economy, giveaways, server counters, and external feed notifications."""

    def __init__(self, bot):
        self.bot = bot
        self._xp_cooldowns = {}
        self._work_cooldowns = {}
        self.counter_loop.start()
        self.notification_loop.start()
        self.giveaway_loop.start()

    def cog_unload(self):
        self.counter_loop.cancel()
        self.notification_loop.cancel()
        self.giveaway_loop.cancel()

    @property
    def db(self):
        return self.bot.db

    async def _ensure_schema(self):
        # Schema is created by Db._create_tables; kept as a no-op for clarity.
        return

    def _xp_for_level(self, level):
        """Total XP needed to reach `level`. Same curve as Db._extras_xp_for_level
        (see that method's docstring for why) - duplicated here rather than
        imported since the bot process owns this cog and the WebUI process
        owns its own copy of Db; kept in sync by hand."""
        return (5 * level * (2 * level * level + 27 * level + 91)) // 6

    def _level_from_xp(self, xp):
        level = 0
        while self._xp_for_level(level + 1) <= xp:
            level += 1
        return level

    async def _apply_level_role_rewards(self, member: discord.Member, new_level: int):
        """Stacking role rewards: grant every reward role at or below
        new_level that the member doesn't already have."""
        rewards = self.db.list_extras_level_roles(member.guild.id)
        if not rewards:
            return
        to_add = []
        for level, role_id in rewards:
            if level > new_level:
                continue
            role = member.guild.get_role(role_id)
            if role and role not in member.roles:
                to_add.append(role)
        if to_add:
            try:
                await member.add_roles(*to_add, reason=f"Reached level {new_level}")
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        if self.db.is_extras_noxp_channel(message.guild.id, message.channel.id):
            return
        now = time.time()
        key = (message.guild.id, message.author.id)
        if now - self._xp_cooldowns.get(key, 0) < XP_COOLDOWN:
            return
        self._xp_cooldowns[key] = now
        row = self.db.conn.execute(
            "SELECT xp, level FROM extras_xp WHERE guild_id=? AND user_id=?",
            (message.guild.id, message.author.id),
        ).fetchone()
        old_level = int(row[1]) if row else 0
        gained = random.randint(XP_PER_MESSAGE, XP_PER_MESSAGE + 5)
        multiplier = 1.0
        member_role_ids = {r.id for r in message.author.roles}
        for role_id, mult in self.db.list_extras_xp_boost_roles(message.guild.id):
            if role_id in member_role_ids:
                multiplier = max(multiplier, mult)
        gained = round(gained * multiplier)
        xp = (int(row[0]) if row else 0) + gained
        new_level = self._level_from_xp(xp)
        self.db.conn.execute(
            """INSERT INTO extras_xp(guild_id,user_id,xp,level) VALUES(?,?,?,?)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=excluded.xp,level=excluded.level""",
            (message.guild.id, message.author.id, xp, new_level),
        )
        self.db.conn.commit()
        if new_level > old_level:
            await self._apply_level_role_rewards(message.author, new_level)
            config = self.db.get_extras_level_config(message.guild.id)
            if config["enabled"]:
                channel = message.channel
                if config["channel_id"]:
                    channel = message.guild.get_channel(config["channel_id"]) or channel
                text = (
                    config["message"]
                    .replace("{user}", message.author.mention)
                    .replace("{level}", str(new_level))
                    .replace("{server}", message.guild.name)
                )
                try:
                    await channel.send(text, allowed_mentions=discord.AllowedMentions(users=[message.author]))
                except discord.HTTPException:
                    pass


    extras = app_commands.Group(name="extras", description="XP, economy, giveaways, counters, and notifications")

    def _progress_bar(self, current: int, needed: int, length: int = 14) -> str:
        filled = min(length, round((current / needed) * length)) if needed else length
        return "█" * filled + "░" * (length - filled)

    @extras.command(name="rank", description="Show your or another member's XP rank and level")
    @app_commands.describe(member="Member to inspect")
    @utils.toggleable("rank")
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        row = self.db.conn.execute(
            "SELECT xp,level FROM extras_xp WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, member.id),
        ).fetchone()
        xp, level = (int(row[0]), int(row[1])) if row else (0, 0)
        this_level_xp = self._xp_for_level(level)
        next_level_xp = self._xp_for_level(level + 1)
        into_level = xp - this_level_xp
        needed = next_level_xp - this_level_xp
        rank_pos = self.db.get_extras_rank(interaction.guild_id, member.id)
        rank_str = f"#{rank_pos}" if rank_pos else "unranked"
        embed = discord.Embed(color=member.color if member.color.value else discord.Color.blurple())
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        embed.add_field(name="Rank", value=rank_str, inline=True)
        embed.add_field(name="Level", value=str(level), inline=True)
        embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
        embed.add_field(
            name=f"Progress to level {level + 1}",
            value=f"`{self._progress_bar(into_level, needed)}` {into_level:,}/{needed:,} XP",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)

    @extras.command(name="leaderboard", description="Show the XP leaderboard")
    @utils.toggleable("leaderboard")
    async def leaderboard(self, interaction: discord.Interaction):
        rows = self.db.conn.execute(
            "SELECT user_id,xp,level FROM extras_xp WHERE guild_id=? ORDER BY xp DESC LIMIT 10",
            (interaction.guild_id,),
        ).fetchall()
        if not rows:
            return await interaction.response.send_message("No XP has been earned yet.")
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"**{medals.get(i, f'{i}.')}** <@{uid}> — Level {level} • {xp:,} XP" for i, (uid, xp, level) in enumerate(rows, 1)]
        embed = discord.Embed(title="🏆 XP Leaderboard", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    def _balance(self, guild_id, user_id):
        row = self.db.conn.execute(
            "SELECT balance,last_daily,daily_streak FROM extras_economy WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ).fetchone()
        return (int(row[0]), row[1], int(row[2])) if row else (0, None, 0)

    @extras.command(name="balance", description="Show an economy balance")
    @utils.toggleable("balance")
    async def balance(self, interaction: discord.Interaction, member: discord.Member | None = None):
        member = member or interaction.user
        bal, _, streak = self._balance(interaction.guild_id, member.id)
        streak_note = f" • 🔥 **{streak}**-day daily streak" if streak > 1 else ""
        await interaction.response.send_message(f"💰 **{member.display_name}** has **{bal:,} coins**{streak_note}.")

    @extras.command(name="daily", description="Claim your daily coins")
    @utils.toggleable("daily")
    async def daily(self, interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        bal, last, streak = self._balance(gid, uid)
        now = int(time.time())
        if last and now - int(last) < 86400:
            remaining = 86400 - (now - int(last))
            h, rem = divmod(remaining, 3600)
            m = rem // 60
            return await interaction.response.send_message(f"⏳ Your daily is on cooldown for **{h}h {m}m**.")
        # Streak continues if claimed again within the grace window; otherwise it resets to 1.
        streak = streak + 1 if last and now - int(last) < DAILY_STREAK_GRACE else 1
        bonus = min(DAILY_STREAK_BONUS_CAP, DAILY_STREAK_BONUS * (streak - 1))
        reward = DAILY_AMOUNT + bonus
        bal += reward
        self.db.conn.execute(
            """INSERT INTO extras_economy(guild_id,user_id,balance,last_daily,daily_streak) VALUES(?,?,?,?,?)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance,last_daily=excluded.last_daily,daily_streak=excluded.daily_streak""",
            (gid, uid, bal, now, streak),
        )
        self.db.conn.commit()
        bonus_note = f" (base {DAILY_AMOUNT:,} + 🔥{streak}-day streak bonus {bonus:,})" if bonus else ""
        await interaction.response.send_message(f"🎁 You claimed **{reward:,} coins**{bonus_note}. Balance: **{bal:,}**.")

    @extras.command(name="work", description="Work a shift for a smaller amount of coins (short cooldown)")
    @utils.toggleable("work")
    async def work(self, interaction: discord.Interaction):
        gid, uid = interaction.guild_id, interaction.user.id
        now = int(time.time())
        key = (gid, uid)
        last = self._work_cooldowns.get(key, 0)
        if now - last < WORK_COOLDOWN:
            remaining = WORK_COOLDOWN - (now - last)
            m, s = divmod(remaining, 60)
            return await interaction.response.send_message(f"⏳ You're still on the clock. Try again in **{m}m {s}s**.")
        self._work_cooldowns[key] = now
        bal, _, _ = self._balance(gid, uid)
        earned = random.randint(WORK_MIN, WORK_MAX)
        bal += earned
        self.db.conn.execute(
            "INSERT INTO extras_economy(guild_id,user_id,balance,last_daily,daily_streak) VALUES(?,?,?,NULL,0) "
            "ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance",
            (gid, uid, bal),
        )
        self.db.conn.commit()
        flavor = random.choice(WORK_FLAVOR)
        await interaction.response.send_message(f"{flavor} You earned **{earned:,} coins**. Balance: **{bal:,}**.")

    @extras.command(name="pay", description="Pay another member")
    @app_commands.describe(member="Recipient", amount="Positive amount of coins")
    @utils.toggleable("pay")
    async def pay(self, interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1, 1000000000]):
        if member.bot or member.id == interaction.user.id:
            return await interaction.response.send_message("You can only pay another human member.", ephemeral=True)
        gid, uid = interaction.guild_id, interaction.user.id
        sender, _, _ = self._balance(gid, uid)
        if sender < amount:
            return await interaction.response.send_message("You don't have enough coins.", ephemeral=True)
        receiver, _, _ = self._balance(gid, member.id)
        self.db.conn.execute(
            "INSERT INTO extras_economy(guild_id,user_id,balance,last_daily) VALUES(?,?,?,NULL) ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance",
            (gid, uid, sender - amount),
        )
        self.db.conn.execute(
            "INSERT INTO extras_economy(guild_id,user_id,balance,last_daily) VALUES(?,?,?,NULL) ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance",
            (gid, member.id, receiver + amount),
        )
        self.db.conn.commit()
        await interaction.response.send_message(f"💸 Paid **{amount:,} coins** to {member.mention}.", allowed_mentions=discord.AllowedMentions(users=[member]))

    @extras.command(name="richest", description="Show the richest members")
    @utils.toggleable("richest")
    async def richest(self, interaction: discord.Interaction):
        rows = self.db.conn.execute(
            "SELECT user_id,balance FROM extras_economy WHERE guild_id=? ORDER BY balance DESC LIMIT 10",
            (interaction.guild_id,),
        ).fetchall()
        if not rows:
            return await interaction.response.send_message("No economy balances exist yet.")
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines = [f"**{medals.get(i, f'{i}.')}** <@{uid}> — {bal:,} coins" for i, (uid, bal) in enumerate(rows, 1)]
        embed = discord.Embed(title="💰 Richest Members", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @extras.command(name="levelconfig", description="Configure level-up announcements")
    @app_commands.describe(
        enabled="Announce level-ups at all",
        channel="Channel to post in (leave empty to post wherever they leveled up)",
        message="Message template - {user}, {level}, {server} are replaced",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelconfig(
        self,
        interaction: discord.Interaction,
        enabled: bool | None = None,
        channel: discord.TextChannel | None = None,
        message: str | None = None,
    ):
        current = self.db.get_extras_level_config(interaction.guild_id)
        new_enabled = current["enabled"] if enabled is None else enabled
        new_channel_id = current["channel_id"] if channel is None else channel.id
        new_message = current["message"] if message is None else message
        self.db.set_extras_level_config(interaction.guild_id, new_enabled, new_channel_id, new_message)
        where = f"<#{new_channel_id}>" if new_channel_id else "wherever the member leveled up"
        await interaction.response.send_message(
            f"✅ Level-up announcements are **{'on' if new_enabled else 'off'}**, posting in {where}.\nMessage: {new_message}",
            ephemeral=True,
        )

    @extras.command(name="levelrole-add", description="Grant a role automatically when members reach a level")
    @app_commands.describe(level="Level required", role="Role to grant")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelrole_add(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 10000], role: discord.Role):
        if role >= interaction.guild.me.top_role:
            return await interaction.response.send_message("I can't assign a role positioned above or equal to my own top role.", ephemeral=True)
        self.db.set_extras_level_role(interaction.guild_id, level, role.id)
        await interaction.response.send_message(f"✅ Members reaching level **{level}** will now be given {role.mention}.", ephemeral=True)

    @extras.command(name="levelrole-remove", description="Remove a level role reward")
    @app_commands.describe(level="Level to remove the reward from")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def levelrole_remove(self, interaction: discord.Interaction, level: app_commands.Range[int, 1, 10000]):
        if self.db.remove_extras_level_role(interaction.guild_id, level):
            await interaction.response.send_message(f"🗑️ Removed the level **{level}** role reward.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No role reward is set for level {level}.", ephemeral=True)

    @extras.command(name="levelrole-list", description="List level role rewards")
    async def levelrole_list(self, interaction: discord.Interaction):
        rows = self.db.list_extras_level_roles(interaction.guild_id)
        if not rows:
            return await interaction.response.send_message("No level role rewards configured.")
        await interaction.response.send_message(
            "\n".join(f"Level **{level}** → <@&{role_id}>" for level, role_id in rows),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @extras.command(name="noxpchannel-add", description="Stop a channel from earning XP")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def noxpchannel_add(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.db.add_extras_noxp_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(f"✅ {channel.mention} no longer earns XP.", ephemeral=True)

    @extras.command(name="noxpchannel-remove", description="Let a channel earn XP again")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def noxpchannel_remove(self, interaction: discord.Interaction, channel: discord.TextChannel):
        if self.db.remove_extras_noxp_channel(interaction.guild_id, channel.id):
            await interaction.response.send_message(f"✅ {channel.mention} earns XP again.", ephemeral=True)
        else:
            await interaction.response.send_message(f"{channel.mention} wasn't excluded.", ephemeral=True)

    @extras.command(name="boostrole-add", description="Give a role a message-XP multiplier")
    @app_commands.describe(role="Role to boost", multiplier="e.g. 1.5 for +50% XP, 2 for double XP")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def boostrole_add(self, interaction: discord.Interaction, role: discord.Role, multiplier: app_commands.Range[float, 1.0, 10.0]):
        self.db.set_extras_xp_boost_role(interaction.guild_id, role.id, multiplier)
        await interaction.response.send_message(f"✅ {role.mention} now earns **{multiplier}x** message XP.", ephemeral=True)

    @extras.command(name="boostrole-remove", description="Remove a role's XP multiplier")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def boostrole_remove(self, interaction: discord.Interaction, role: discord.Role):
        if self.db.remove_extras_xp_boost_role(interaction.guild_id, role.id):
            await interaction.response.send_message(f"🗑️ Removed {role.mention}'s XP boost.", ephemeral=True)
        else:
            await interaction.response.send_message(f"{role.mention} doesn't have a boost set.", ephemeral=True)


    @extras.command(name="giveaway", description="Start a giveaway")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(duration_minutes="How long it runs", winners="Number of winners", prize="Prize text")
    async def giveaway(self, interaction: discord.Interaction, duration_minutes: app_commands.Range[int, 1, 10080], winners: app_commands.Range[int, 1, 20], prize: str):
        end_at = int(time.time()) + duration_minutes * 60
        embed = discord.Embed(title="🎉 Giveaway!", description=f"**{prize}**\nReact with 🎉 to enter.\nEnds <t:{end_at}:R>")
        msg = await interaction.channel.send(embed=embed)
        await msg.add_reaction("🎉")
        self.db.conn.execute(
            "INSERT INTO extras_giveaways(guild_id,channel_id,message_id,prize,winners,end_at,ended) VALUES(?,?,?,?,?,?,0)",
            (interaction.guild_id, interaction.channel.id, msg.id, prize, winners, end_at),
        )
        self.db.conn.commit()
        await interaction.response.send_message(f"Giveaway started: {msg.jump_url}", ephemeral=True)

    @extras.command(name="giveaway-end", description="End a giveaway now")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def giveaway_end(self, interaction: discord.Interaction, message_id: str):
        try:
            mid = int(message_id)
        except ValueError:
            return await interaction.response.send_message("Invalid message ID.", ephemeral=True)
        ok = await self._end_giveaway(mid)
        await interaction.response.send_message("Giveaway ended." if ok else "Giveaway not found or already ended.", ephemeral=True)

    @extras.command(name="counter", description="Configure a live server counter voice channel")
    @app_commands.checks.has_permissions(manage_channels=True)
    async def counter(self, interaction: discord.Interaction, kind: str, channel: discord.VoiceChannel):
        kind = kind.lower()
        if kind not in {"members", "online", "bots", "channels"}:
            return await interaction.response.send_message("Kind must be members, online, bots, or channels.", ephemeral=True)
        self.db.conn.execute(
            "INSERT INTO extras_counters(guild_id,channel_id,kind) VALUES(?,?,?) ON CONFLICT(guild_id,kind) DO UPDATE SET channel_id=excluded.channel_id",
            (interaction.guild_id, channel.id, kind),
        )
        self.db.conn.commit()
        await interaction.response.send_message(f"Counter configured for **{kind}**.", ephemeral=True)

    @extras.command(name="twitch-add", description="Add a Twitch channel notification")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def twitch_add(self, interaction: discord.Interaction, username: str, channel: discord.TextChannel):
        self.db.conn.execute(
            "INSERT INTO extras_twitch(guild_id,username,channel_id,last_live) VALUES(?,?,?,0) ON CONFLICT(guild_id,username) DO UPDATE SET channel_id=excluded.channel_id",
            (interaction.guild_id, username.lower().strip(), channel.id),
        )
        self.db.conn.commit()
        await interaction.response.send_message(f"Added Twitch notifications for **{username}**.", ephemeral=True)

    @extras.command(name="twitch-remove", description="Remove a Twitch channel notification")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def twitch_remove(self, interaction: discord.Interaction, username: str):
        cur = self.db.conn.execute("DELETE FROM extras_twitch WHERE guild_id=? AND username=?", (interaction.guild_id, username.lower().strip()))
        self.db.conn.commit()
        await interaction.response.send_message("Removed." if cur.rowcount else "Not found.", ephemeral=True)

    @extras.command(name="feed-add", description="Add an RSS or Atom feed")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def feed_add(self, interaction: discord.Interaction, url: str, channel: discord.TextChannel):
        if not (url.startswith("http://") or url.startswith("https://")):
            return await interaction.response.send_message("Feed URL must start with http:// or https://.", ephemeral=True)
        self.db.conn.execute(
            "INSERT INTO extras_feeds(guild_id,url,channel_id,last_id) VALUES(?,?,?,?) ON CONFLICT(guild_id,url) DO UPDATE SET channel_id=excluded.channel_id",
            (interaction.guild_id, url, channel.id, ""),
        )
        self.db.conn.commit()
        await interaction.response.send_message("Feed added.", ephemeral=True)

    @extras.command(name="feed-remove", description="Remove an RSS or Atom feed")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def feed_remove(self, interaction: discord.Interaction, url: str):
        cur = self.db.conn.execute("DELETE FROM extras_feeds WHERE guild_id=? AND url=?", (interaction.guild_id, url))
        self.db.conn.commit()
        await interaction.response.send_message("Removed." if cur.rowcount else "Not found.", ephemeral=True)

    async def _end_giveaway(self, message_id):
        row = self.db.conn.execute(
            "SELECT guild_id,channel_id,message_id,prize,winners,ended FROM extras_giveaways WHERE message_id=?",
            (message_id,),
        ).fetchone()
        if not row or row[5]:
            return False
        guild = self.bot.get_guild(row[0])
        channel = guild.get_channel(row[1]) if guild else None
        if not channel:
            return False
        try:
            msg = await channel.fetch_message(row[2])
        except discord.HTTPException:
            return False
        reaction = discord.utils.get(msg.reactions, emoji="🎉")
        users = []
        if reaction:
            try:
                users = [u async for u in reaction.users() if not u.bot]
            except discord.HTTPException:
                users = []
        random.shuffle(users)
        winners = users[:int(row[4])]
        self.db.conn.execute("UPDATE extras_giveaways SET ended=1 WHERE message_id=?", (message_id,))
        self.db.conn.commit()
        if winners:
            mentions = " ".join(u.mention for u in winners)
            await channel.send(f"🎉 Giveaway ended! Prize: **{row[3]}**\nWinner(s): {mentions}", allowed_mentions=discord.AllowedMentions(users=winners))
        else:
            await channel.send(f"🎉 Giveaway ended! Prize: **{row[3]}**\nNo valid entries.")
        return True

    @tasks.loop(minutes=1)
    async def giveaway_loop(self):
        await self.bot.wait_until_ready()
        now = int(time.time())
        rows = self.db.conn.execute("SELECT message_id FROM extras_giveaways WHERE ended=0 AND end_at<=?", (now,)).fetchall()
        for (mid,) in rows:
            try:
                await self._end_giveaway(mid)
            except Exception:
                logger.exception("Failed ending giveaway %s", mid)

    @tasks.loop(minutes=1)
    async def counter_loop(self):
        await self.bot.wait_until_ready()
        rows = self.db.conn.execute("SELECT guild_id,channel_id,kind FROM extras_counters").fetchall()
        for gid, cid, kind in rows:
            guild = self.bot.get_guild(gid)
            channel = guild.get_channel(cid) if guild else None
            if not guild or not isinstance(channel, discord.VoiceChannel):
                continue
            if kind == "members":
                count = guild.member_count
            elif kind == "online":
                count = sum(1 for m in guild.members if m.status != discord.Status.offline)
            elif kind == "bots":
                count = sum(1 for m in guild.members if m.bot)
            else:
                count = len(guild.channels)
            try:
                await channel.edit(name=f"{kind.title()}: {count}")
            except (discord.Forbidden, discord.HTTPException):
                pass

    @tasks.loop(minutes=5)
    async def notification_loop(self):
        await self.bot.wait_until_ready()
        await self._poll_twitch()
        await self._poll_feeds()

    async def _poll_twitch(self):
        client_id = os.getenv("TWITCH_CLIENT_ID")
        secret = os.getenv("TWITCH_CLIENT_SECRET")
        if not client_id or not secret:
            return
        rows = self.db.conn.execute("SELECT guild_id,username,channel_id,last_live FROM extras_twitch").fetchall()
        if not rows:
            return
        try:
            async with aiohttp.ClientSession() as session:
                token_resp = await session.post("https://id.twitch.tv/oauth2/token", params={"client_id": client_id, "client_secret": secret, "grant_type": "client_credentials"}, timeout=10)
                if token_resp.status != 200:
                    return
                token = (await token_resp.json()).get("access_token")
                for gid, username, cid, old_live in rows:
                    resp = await session.get("https://api.twitch.tv/helix/streams", params={"user_login": username}, headers={"Client-ID": client_id, "Authorization": f"Bearer {token}"}, timeout=10)
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    live = bool(data.get("data"))
                    if live and not old_live:
                        d = data["data"][0]
                        guild = self.bot.get_guild(gid); channel = guild.get_channel(cid) if guild else None
                        if channel:
                            await channel.send(f"🟣 **{username}** is live on Twitch: {d.get('title','')}")
                    self.db.conn.execute("UPDATE extras_twitch SET last_live=? WHERE guild_id=? AND username=?", (int(live), gid, username))
        except Exception:
            logger.exception("Twitch polling failed")
        self.db.conn.commit()

    async def _poll_feeds(self):
        rows = self.db.conn.execute("SELECT guild_id,url,channel_id,last_id FROM extras_feeds").fetchall()
        for gid, url, cid, last_id in rows:
            try:
                parsed = await asyncio.to_thread(feedparser.parse, url)
                entries = list(parsed.entries or [])
                if not entries:
                    continue
                # Stable IDs prevent repeats; initialize silently with the newest entry.
                newest = entries[0]
                newest_id = newest.get("id") or newest.get("guid") or newest.get("link") or newest.get("title","")
                if not last_id:
                    self.db.conn.execute("UPDATE extras_feeds SET last_id=? WHERE guild_id=? AND url=?", (newest_id, gid, url))
                    continue
                new_entries = []
                for entry in entries[:20]:
                    eid = entry.get("id") or entry.get("guid") or entry.get("link") or entry.get("title","")
                    if eid == last_id:
                        break
                    new_entries.append(entry)
                guild = self.bot.get_guild(gid); channel = guild.get_channel(cid) if guild else None
                for entry in reversed(new_entries):
                    if channel:
                        title = entry.get("title", "New post")
                        link = entry.get("link", "")
                        await channel.send(f"📰 **{title}**\n{link}" if link else f"📰 **{title}**")
                if new_entries:
                    newest_id = new_entries[0].get("id") or new_entries[0].get("guid") or new_entries[0].get("link") or new_entries[0].get("title","")
                    self.db.conn.execute("UPDATE extras_feeds SET last_id=? WHERE guild_id=? AND url=?", (newest_id, gid, url))
            except Exception:
                logger.exception("Feed polling failed for %s", url)
        self.db.conn.commit()


async def setup(bot):
    await bot.add_cog(Extras(bot))
