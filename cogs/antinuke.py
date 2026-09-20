"""Anti-nuke - watches the audit log for a burst of destructive actions
(channel/role deletes, bans, kicks, webhook creation, new bots joining)
from a single actor and punishes them automatically, the way Wick/栅栏-style
"anti-nuke" bots do.

This is a burst detector, not a permission system: it doesn't stop any
single action (Discord's own audit log only tells us *after* something
happened), it reacts once the same actor crosses a configurable threshold
within a short rolling window. A compromised admin account deleting one
channel looks like an admin doing admin things; deleting five channels in
ten seconds looks like a nuke - that's the signal this cog keys on.

The burst counters are in-memory only (per guild/user, like automod's
UserMessageTracker) - a restart resets everyone's count to zero, which is
the right failure mode for a bot that runs one process, no worse than
automod's own violation window losing state on restart.

Requires Intents.moderation for on_audit_log_entry_create - see bot.py.
"""
import datetime
import logging
import time
from collections import defaultdict, deque

import discord
from discord import app_commands
from discord.ext import commands

from utils import can_moderate, manager_or_permission, removable_roles_for_strip

logger = logging.getLogger("antinuke")

# Audit log actions this cog knows how to watch for and (where possible)
# recover from. Kept as a plain dict rather than every AuditLogAction so
# the WebUI/slash command surface only offers meaningful toggles.
WATCHABLE_ACTIONS = {
    "channel_delete": discord.AuditLogAction.channel_delete,
    "role_delete": discord.AuditLogAction.role_delete,
    "ban": discord.AuditLogAction.ban,
    "kick": discord.AuditLogAction.kick,
    "webhook_create": discord.AuditLogAction.webhook_create,
    "bot_add": discord.AuditLogAction.bot_add,
}
ACTION_LABELS = {
    "channel_delete": "Channel Deletion",
    "role_delete": "Role Deletion",
    "ban": "Mass Ban",
    "kick": "Mass Kick",
    "webhook_create": "Webhook Creation",
    "bot_add": "Bot Added",
}
PUNISHMENTS = ("BAN", "KICK", "TIMEOUT", "STRIP_ROLES")
MAX_TIMEOUT_SECONDS = 28 * 86400  # Discord's own timeout ceiling


class AntiNuke(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, user_id) -> deque[timestamp] of watched-action hits in
        # the current rolling window. Same shape as automod's per-user
        # tracker, just keyed on audit-log actor instead of message author.
        self.hits: dict[tuple[int, int], deque] = defaultdict(deque)

    antinuke = app_commands.Group(
        name="antinuke", description="Configure automatic protection against mass-destructive actions"
    )

    # ---- audit log listener ----

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        cfg = self.bot.db.get_antinuke_config(guild.id)
        if not cfg["enabled"]:
            return

        action_key = next((k for k, v in WATCHABLE_ACTIONS.items() if v == entry.action), None)
        if action_key is None or action_key not in cfg["watched_actions"]:
            return

        actor = entry.user
        if actor is None:
            return  # can't attribute this entry to a user
        if self.bot.user is not None and actor.id == self.bot.user.id:
            return  # ignore the bot's own actions
        if actor.id == guild.owner_id:
            return  # never act against the server owner
        if actor.id in self.bot.db.list_antinuke_whitelist(guild.id):
            return

        now = time.time()
        key = (guild.id, actor.id)
        window = self.hits[key]
        window.append(now)
        cutoff = now - cfg["window_seconds"]
        while window and window[0] < cutoff:
            window.popleft()

        if len(window) < cfg["threshold"]:
            return

        # Threshold crossed - reset so a single punished burst doesn't keep
        # re-triggering as later hits within the same window still arrive.
        window.clear()

        member = guild.get_member(actor.id)
        if member is None:
            logger.info("antinuke: actor %s left before punishment could be applied", actor.id)
            return

        await self._punish(guild, member, action_key, cfg)
        recovery_result = None
        if cfg["auto_recovery"] and action_key in ("channel_delete", "role_delete"):
            recovery_result = await self._attempt_recovery(guild, entry, action_key)
        if recovery_result is not None:
            await self._log_recovery(guild, action_key, recovery_result, cfg)

    async def _punish(self, guild: discord.Guild, member: discord.Member, action_key: str, cfg: dict) -> None:
        me = guild.me
        punishment = cfg["default_punishment"]

        # Hierarchy/self-protection checks mirror the rest of the codebase's
        # moderation actions (utils.can_moderate) - if the bot genuinely
        # can't act on this member (e.g. their role outranks the bot's),
        # fall back to the least-privileged thing we CAN still do: strip
        # roles the bot has authority over, rather than doing nothing.
        if me is None or not can_moderate(me, member):
            punishment = "STRIP_ROLES"

        reason = f"Anti-nuke: {ACTION_LABELS.get(action_key, action_key)} threshold exceeded"
        applied = punishment
        try:
            if punishment == "BAN":
                await member.ban(reason=reason, delete_message_seconds=0)
            elif punishment == "KICK":
                await member.kick(reason=reason)
            elif punishment == "TIMEOUT":
                until = discord.utils.utcnow() + datetime.timedelta(seconds=MAX_TIMEOUT_SECONDS)
                await member.timeout(until, reason=reason)
            elif punishment == "STRIP_ROLES":
                stripped = removable_roles_for_strip(guild, member)
                if stripped:
                    self.bot.db.save_stripped_roles(guild.id, member.id, [r.id for r in stripped])
                    await member.remove_roles(*stripped, reason=reason)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("antinuke: failed to apply %s to %s in guild %s", punishment, member.id, guild.id)
            applied = f"{punishment} (failed - check bot permissions/role position)"

        self.bot.db.record_antinuke_incident(guild.id, member.id, action_key, applied, cfg["threshold"])
        await self._log(guild, member, action_key, applied, cfg)

    async def _attempt_recovery(self, guild: discord.Guild, entry: discord.AuditLogEntry, action_key: str) -> str:
        """Best-effort recreation of what the burst just destroyed, using
        the 'before' state the audit log entry already carries for delete
        actions - no separate delete listener needed to remember it.

        This is explicitly *basic* recovery, not a full restore, and the
        string it returns says so: Discord's audit-log delete diff only
        exposes a handful of a channel's properties (name, type, topic,
        nsfw, slowmode, bitrate/user limit for voice) - not permission
        overwrites, category, or exact position - so those aren't
        reconstructed. Earlier wording ("auto-recovery") implied more than
        that; callers should show this string rather than a bare "recovered".
        """
        before = entry.before
        try:
            if action_key == "channel_delete":
                return await self._recover_channel(guild, before)
            elif action_key == "role_delete":
                name = getattr(before, "name", None) or "recovered-role"
                await guild.create_role(
                    name=name,
                    color=getattr(before, "colour", discord.Color.default()),
                    hoist=bool(getattr(before, "hoist", False)),
                    mentionable=bool(getattr(before, "mentionable", False)),
                    permissions=getattr(before, "permissions", discord.Permissions.none()),
                    reason="Anti-nuke auto-recovery",
                )
                return f"recreated role **{name}** (permissions restored; role position may differ)"
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("antinuke: auto-recovery failed for %s in guild %s", action_key, guild.id)
            return "recovery attempted but failed - check the bot's permissions"
        return "nothing to recover"

    async def _recover_channel(self, guild: discord.Guild, before) -> str:
        """Recreates a deleted channel using whatever the audit log's before-
        state exposes for its type. Type is checked first since text, voice,
        and forum channels expose different recoverable fields."""
        name = getattr(before, "name", None) or "recovered-channel"
        channel_type = getattr(before, "type", None)
        kwargs = {"reason": "Anti-nuke auto-recovery"}

        if channel_type == discord.ChannelType.voice:
            if getattr(before, "bitrate", None):
                kwargs["bitrate"] = before.bitrate
            if getattr(before, "user_limit", None):
                kwargs["user_limit"] = before.user_limit
            await guild.create_voice_channel(name=name, **kwargs)
        elif channel_type == discord.ChannelType.forum:
            if getattr(before, "topic", None):
                kwargs["topic"] = before.topic
            if getattr(before, "nsfw", None):
                kwargs["nsfw"] = before.nsfw
            await guild.create_forum(name=name, **kwargs)
        else:
            # Text (and anything else recognizable) falls back to a text
            # channel - the same behavior as before this fix, just carrying
            # over more of the recoverable fields when the audit log has them.
            if getattr(before, "topic", None):
                kwargs["topic"] = before.topic
            if getattr(before, "nsfw", None):
                kwargs["nsfw"] = before.nsfw
            if getattr(before, "slowmode_delay", None):
                kwargs["slowmode_delay"] = before.slowmode_delay
            await guild.create_text_channel(name=name, **kwargs)

        return f"recreated channel **{name}** (basic recovery - permissions, category, and exact position not restored)"

    async def _log_recovery(self, guild: discord.Guild, action_key: str, result: str, cfg: dict) -> None:
        """Separate from _log's punishment embed since recovery happens after
        it and can fail or succeed independently. Says exactly what came back
        (including "basic recovery" wording) rather than a bare on/off toggle,
        so nobody reads "Auto-Recovery: On" as "fully restored"."""
        channel = guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None
        if channel is None:
            return
        try:
            await channel.send(f"🛠️ Anti-nuke auto-recovery ({action_key.replace('_', ' ')}): {result}")
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("antinuke: couldn't post recovery result in guild %s", guild.id)

    async def _log(self, guild: discord.Guild, member: discord.Member, action_key: str, applied: str, cfg: dict) -> None:
        embed = discord.Embed(
            title="Anti-Nuke Triggered",
            description=(
                f"**{ACTION_LABELS.get(action_key, action_key)}** threshold exceeded "
                f"({cfg['threshold']} hits / {cfg['window_seconds']}s)"
            ),
            color=discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Actor", value=f"{member.mention} ({member.id})", inline=True)
        embed.add_field(name="Punishment Applied", value=applied, inline=True)
        embed.add_field(name="Auto-Recovery (basic)", value="On" if cfg["auto_recovery"] else "Off", inline=True)

        channel = None
        if cfg["log_channel_id"]:
            channel = guild.get_channel(cfg["log_channel_id"])
        if channel is not None:
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                pass

        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is not None:
            await logging_cog.log_event(guild, "moderation", embed)

    # ---- slash commands ----

    @antinuke.command(name="settings", description="View or open the anti-nuke configuration panel")
    @manager_or_permission("manage_guild")
    async def antinuke_settings(self, interaction: discord.Interaction):
        cfg = self.bot.db.get_antinuke_config(interaction.guild.id)
        log_channel = interaction.guild.get_channel(cfg["log_channel_id"]) if cfg["log_channel_id"] else None

        embed = discord.Embed(title="Anti-Nuke - Advanced Configuration", color=discord.Color.blurple())
        embed.add_field(
            name="System State",
            value=f"**{'Enabled' if cfg['enabled'] else 'Disabled'}**\nAuto-Recovery: {'On' if cfg['auto_recovery'] else 'Off'}",
            inline=False,
        )
        embed.add_field(
            name="Enforcement Policy",
            value=(
                f"Default Punishment: **{cfg['default_punishment']}**\n"
                f"Trigger: **{cfg['threshold']}** actions / **{cfg['window_seconds']}s**\n"
                f"Available Actions: {', '.join(PUNISHMENTS)}"
            ),
            inline=False,
        )
        embed.add_field(
            name="Watched Events",
            value=", ".join(ACTION_LABELS.get(a, a) for a in cfg["watched_actions"]) or "None",
            inline=False,
        )
        embed.add_field(
            name="Logging",
            value=log_channel.mention if log_channel else "No log channel set",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @antinuke.command(name="enable", description="Turn anti-nuke protection on for this server")
    @manager_or_permission("manage_guild")
    async def antinuke_enable(self, interaction: discord.Interaction):
        self.bot.db.set_antinuke_enabled(interaction.guild.id, True)
        await interaction.response.send_message("Anti-nuke protection is now **enabled**.", ephemeral=True)

    @antinuke.command(name="disable", description="Turn anti-nuke protection off for this server")
    @manager_or_permission("manage_guild")
    async def antinuke_disable(self, interaction: discord.Interaction):
        self.bot.db.set_antinuke_enabled(interaction.guild.id, False)
        await interaction.response.send_message("Anti-nuke protection is now **disabled**.", ephemeral=True)

    @antinuke.command(name="autorecovery", description="Toggle basic auto-recreation of deleted channels/roles (name/type only, not permissions or position)")
    @app_commands.describe(enabled="Whether auto-recovery should be on")
    @manager_or_permission("manage_guild")
    async def antinuke_autorecovery(self, interaction: discord.Interaction, enabled: bool):
        self.bot.db.set_antinuke_auto_recovery(interaction.guild.id, enabled)
        await interaction.response.send_message(
            f"Auto-recovery is now **{'on' if enabled else 'off'}**.", ephemeral=True
        )

    @antinuke.command(name="punishment", description="Set the default punishment applied when anti-nuke triggers")
    @app_commands.describe(action="Punishment to apply")
    @app_commands.choices(action=[app_commands.Choice(name=p, value=p) for p in PUNISHMENTS])
    @manager_or_permission("manage_guild")
    async def antinuke_punishment(self, interaction: discord.Interaction, action: app_commands.Choice[str]):
        self.bot.db.set_antinuke_punishment(interaction.guild.id, action.value)
        await interaction.response.send_message(f"Default punishment set to **{action.value}**.", ephemeral=True)

    @antinuke.command(name="threshold", description="Set how many watched actions in a window trigger anti-nuke")
    @app_commands.describe(count="Number of actions", seconds="Rolling window, in seconds")
    @manager_or_permission("manage_guild")
    async def antinuke_threshold(
        self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 50], seconds: app_commands.Range[int, 1, 3600]
    ):
        self.bot.db.set_antinuke_threshold(interaction.guild.id, count, seconds)
        await interaction.response.send_message(
            f"Anti-nuke now triggers at **{count}** watched action(s) within **{seconds}s**.", ephemeral=True
        )

    @antinuke.command(name="logchannel", description="Set where anti-nuke incidents are reported")
    @app_commands.describe(channel="Text channel for anti-nuke logs")
    @manager_or_permission("manage_guild")
    async def antinuke_logchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        self.bot.db.set_antinuke_log_channel(interaction.guild.id, channel.id)
        await interaction.response.send_message(f"Anti-nuke incidents will be logged to {channel.mention}.", ephemeral=True)

    @antinuke.command(name="whitelist", description="Exempt a member from anti-nuke detection")
    @app_commands.describe(user="Member to exempt", remove="Remove them from the whitelist instead")
    @manager_or_permission("manage_guild")
    async def antinuke_whitelist(self, interaction: discord.Interaction, user: discord.Member, remove: bool = False):
        if remove:
            self.bot.db.remove_antinuke_whitelist(interaction.guild.id, user.id)
            await interaction.response.send_message(f"Removed {user.mention} from the anti-nuke whitelist.", ephemeral=True)
        else:
            self.bot.db.add_antinuke_whitelist(interaction.guild.id, user.id, interaction.user.id)
            await interaction.response.send_message(f"{user.mention} is now exempt from anti-nuke detection.", ephemeral=True)

    @antinuke.command(name="incidents", description="Show recent anti-nuke incidents")
    @manager_or_permission("manage_guild")
    async def antinuke_incidents(self, interaction: discord.Interaction):
        rows = self.bot.db.list_antinuke_incidents(interaction.guild.id, limit=10)
        if not rows:
            await interaction.response.send_message("No anti-nuke incidents recorded.", ephemeral=True)
            return
        lines = []
        for _id, user_id, trigger_action, punishment, hit_count, created_at in rows:
            lines.append(
                f"<t:{created_at}:R> - <@{user_id}> - {ACTION_LABELS.get(trigger_action, trigger_action)} - {punishment}"
            )
        embed = discord.Embed(title="Recent Anti-Nuke Incidents", description="\n".join(lines), color=discord.Color.dark_red())
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiNuke(bot))
