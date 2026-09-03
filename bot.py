import asyncio
import logging
import os
import re
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands

import scheduler
import utils
from db import Db
from framework import Feature, FeatureStore

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


class RedactingFormatter(logging.Formatter):
    """Prevent common secrets from being written to host/container logs."""

    _PATTERNS = (
        re.compile(r"(?i)(DISCORD_TOKEN|BOT_TOKEN|API_KEY|CLIENT_SECRET|PASSWORD|SECRET)\s*[=:]\s*([^\s,;]+)"),
        re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+"),
        re.compile(r"(?i)(https://(?:discord(?:app)?\.com/api/)?webhooks/)[^\s]+"),
    )

    @classmethod
    def redact(cls, text: str) -> str:
        for pattern in cls._PATTERNS:
            if pattern.pattern.startswith("(?i)(Bearer"):
                text = pattern.sub(r"\1[REDACTED]", text)
            elif "webhooks" in pattern.pattern:
                text = pattern.sub(r"\1[REDACTED]", text)
            else:
                text = pattern.sub(r"\1=[REDACTED]", text)
        return text

    def format(self, record):
        return self.redact(super().format(record))


logger = logging.getLogger("bot")


def configure_persistent_logging(db_path: str) -> str:
    """Persist technical logs beside the durable SQLite database.

    Audit events live in bot.db. Technical startup/gateway/application output
    is split into system.log and errors.log, both rotated on the mounted host
    volume so a runaway error loop cannot fill the disk.
    """
    data_dir = os.path.dirname(os.path.abspath(db_path)) or os.getcwd()
    log_dir = os.path.join(data_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    system_path = os.path.abspath(os.path.join(log_dir, "system.log"))
    error_path = os.path.abspath(os.path.join(log_dir, "errors.log"))
    root_logger = logging.getLogger()

    formatter = RedactingFormatter(LOG_FORMAT)
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)

    def add_rotating(path: str, level: int) -> None:
        if any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == path for h in root_logger.handlers):
            return
        handler = RotatingFileHandler(
            path,
            maxBytes=int(os.environ.get("LOG_MAX_BYTES", 10 * 1024 * 1024)),
            backupCount=int(os.environ.get("LOG_BACKUP_COUNT", 10)),
            encoding="utf-8",
        )
        handler.setLevel(level)
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)

    add_rotating(system_path, logging.INFO)
    add_rotating(error_path, logging.ERROR)
    return system_path

# Discord's hard cap on top-level application commands (per bot, global
# scope). See the command-count check in setup_hook() below.
DISCORD_COMMAND_LIMIT = 100
DISCORD_COMMAND_WARN_THRESHOLD = 90

INITIAL_COGS = [
    "cogs.fun",
    "cogs.extras",
    "cogs.moderation",
    "cogs.customcommands",
    "cogs.reminders",
    "cogs.welcome",
    "cogs.birthdays",
    "cogs.counting",
    "cogs.youtube",
    "cogs.tempvoice",
    "cogs.automod",
    "cogs.reactionroles",
    "cogs.stickyroles",
    "cogs.logging_cog",
    "cogs.dashboardtalk",
    "cogs.dashboardmoderation",
    "cogs.channelfeed",
    "cogs.verification",
    "cogs.tickets",
    "cogs.polls",
    "cogs.votekick",
    "cogs.rules",
    "cogs.reports",
    "cogs.emergency",
    "cogs.userinfo",
    "cogs.antinuke",
    "cogs.starboard",
    "cogs.suggestions",
    "cogs.ping",
    "cogs.cases",
    "cogs.raiddetection",
    "cogs.invites",
    "cogs.say",
    "cogs.modmail",
    "cogs.ai",
]

FEATURES = FeatureStore([
    Feature("moderation", "cogs.moderation", "Warnings, timeouts, kicks, bans, tempbans, and nickname moderation.", "moderation"),
    Feature("automod", "cogs.automod", "Automated message and spam protection.", "moderation"),
    Feature("welcome", "cogs.welcome", "Welcome messages, cards, and autoroles.", "community"),
    Feature("customcommands", "cogs.customcommands", "Per-server custom text commands.", "utility"),
    Feature("birthdays", "cogs.birthdays", "Birthday storage and announcements.", "community"),
    Feature("counting", "cogs.counting", "Counting channel and score tracking.", "community"),
    Feature("youtube", "cogs.youtube", "RSS-based YouTube upload alerts.", "notifications"),
    Feature("tempvoice", "cogs.tempvoice", "Temporary voice-channel management.", "utility"),
    Feature("reactionroles", "cogs.reactionroles", "Reaction-based role assignment.", "community"),
    Feature("reminders", "cogs.reminders", "Scheduled reminders.", "utility"),
    Feature("fun", "cogs.fun", "Fun and lightweight community commands.", "fun"),
    Feature("verification", "cogs.verification", "Button-based member verification gate.", "moderation"),
    Feature("tickets", "cogs.tickets", "Private support ticket channels.", "utility"),
    Feature("polls", "cogs.polls", "Button-based polls with live results.", "fun"),
    Feature("votekick", "cogs.votekick", "Community vote-to-kick with persistent voting and configurable thresholds.", "moderation"),
    Feature("rules", "cogs.rules", "Numbered server rules, citable from /warn.", "moderation"),
    Feature("reports", "cogs.reports", "Member-submitted reports triaged on the dashboard.", "moderation"),
    Feature("emergency", "cogs.emergency", "Dashboard-triggered server-wide lockdown, invite revoke, and mass timeout.", "moderation"),
    Feature("userinfo", "cogs.userinfo", "Member lookup with /whois.", "utility"),
    Feature("antinuke", "cogs.antinuke", "Automatic detection and punishment of mass-destructive action bursts (channel/role deletion, mass ban/kick, rogue webhooks/bots).", "moderation"),
    Feature("starboard", "cogs.starboard", "Highlight messages that receive enough ⭐ reactions.", "community"),
    Feature("suggestions", "cogs.suggestions", "Server suggestion intake and staff review.", "community"),
    Feature("ai", "cogs.ai", "Optional OpenAI-compatible AI assistant connections.", "utility"),
    Feature("stickyroles", "cogs.stickyroles", "Reapplies configured roles to members who leave and rejoin.", "community"),
    Feature("logging", "cogs.logging_cog", "Server activity logging (messages, members, moderation, server, voice).", "moderation"),
    Feature("dashboardtalk", "cogs.dashboardtalk", "Lets the WebUI send/relay messages through the bot.", "utility"),
    Feature("dashboardmoderation", "cogs.dashboardmoderation", "Applies moderation actions queued from the WebUI.", "moderation"),
    Feature("channelfeed", "cogs.channelfeed", "Mirrors a channel's messages to the dashboard for read-only monitoring.", "utility"),
    Feature("cases", "cogs.cases", "Numbered case/infraction system and private staff notes.", "moderation"),
    Feature("raiddetection", "cogs.raiddetection", "Detects and responds to join-raid bursts.", "moderation"),
    Feature("invites", "cogs.invites", "Attributes joins to invite codes and tracks leaderboards/milestones.", "community"),
    Feature("say", "cogs.say", "Owner/Administrator command to post a message as the bot.", "utility"),
    Feature("modmail", "cogs.modmail", "DM-based modmail threads with staff.", "moderation"),
    Feature("ping", "cogs.ping", "Basic latency check command.", "utility"),
])

intents = discord.Intents.default()
intents.message_content = True  # needed for custom commands to see message text
intents.members = True  # needed for on_member_join (welcome/autorole)
intents.presences = True  # needed for live online/idle/dnd analytics
intents.moderation = True  # needed for on_audit_log_entry_create (anti-nuke)
intents.invites = True  # needed for on_invite_create/on_invite_delete (invite tracking)


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        db_path = os.environ.get("DB_PATH", "data/bot.db")
        configure_persistent_logging(db_path)
        self.db = Db(db_path)

    async def setup_hook(self):
        self.db.record_bot_event("bot.startup", None, None, None, "process starting")
        for cog in INITIAL_COGS:
            try:
                await self.load_extension(cog)
            except commands.ExtensionNotFound:
                logger.error(
                    "Skipping %s: no such cog file. If this was intentional, "
                    "also remove it from INITIAL_COGS in bot.py.",
                    cog,
                )
            except Exception:
                logger.exception("Skipping %s: failed to load", cog)

        # Discord hard-caps a bot at 100 top-level application commands
        # (global or per-guild). Subcommands/subcommand-groups inside a
        # Group don't count against this - only the Group itself does -
        # which is why fun/moderation/automod etc. are organized as single
        # groups rather than dozens of flat top-level commands. Warn loudly
        # well before that cap is hit so it gets noticed on a normal
        # restart instead of only when Discord starts rejecting the sync.
        command_count = len(self.tree.get_commands())
        logger.info("%d top-level application command(s) registered locally", command_count)
        if command_count > DISCORD_COMMAND_LIMIT:
            logger.error(
                "%d top-level commands registered - over Discord's %d-command limit. "
                "Sync below will likely be rejected. Group more commands into an "
                "app_commands.Group (see the README's 'Disabling or removing a cog' "
                "section for how fun/moderation/automod were done) before adding more.",
                command_count, DISCORD_COMMAND_LIMIT,
            )
        elif command_count >= DISCORD_COMMAND_WARN_THRESHOLD:
            logger.warning(
                "%d/%d top-level application commands in use - getting close to "
                "Discord's limit. Consider grouping new commands instead of adding "
                "them as new top-level commands.",
                command_count, DISCORD_COMMAND_LIMIT,
            )

        # DEV_GUILD_ID (optional): if set, commands sync instantly to that
        # one server instead of waiting on Discord's global propagation
        # (which can take up to an hour). Set it in .env to your server's
        # ID while actively adding/testing commands; leave it unset for
        # normal global sync once things are stable.
        dev_guild_id = os.environ.get("DEV_GUILD_ID")
        try:
            if dev_guild_id:
                guild = discord.Object(id=int(dev_guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                logger.info("Synced %d commands instantly to guild %s", len(synced), dev_guild_id)
            else:
                synced = await self.tree.sync()
                logger.info("Synced %d commands globally (can take up to an hour to appear)", len(synced))
        except discord.HTTPException:
            # Most commonly: over the 100-command limit, or a malformed
            # command definition. Don't let a bad sync take the whole bot
            # down - it can still run (moderation, automod message
            # filtering, the WebUI, etc. don't depend on a fresh sync)
            # with whatever commands Discord last had registered.
            logger.exception(
                "Command sync failed - the bot will keep starting anyway and run "
                "with whatever commands Discord last had synced. Fix the command "
                "list (likely the %d-command limit above) and restart to retry.",
                DISCORD_COMMAND_LIMIT,
            )

        self.tree.on_error = self.on_app_command_error
        self.loop.create_task(scheduler.run_loop(self, self.db))

    async def on_ready(self):
        logger.info("Logged in as %s", self.user)
        # Full resync on every connect. Cached Discord objects let the WebUI
        # show human-readable names instead of making administrators copy IDs.
        self.db.sync_bot_guilds([(g.id, g.name) for g in self.guilds])
        for guild in self.guilds:
            self._sync_guild_cache(guild)

    def _sync_guild_cache(self, guild: discord.Guild) -> None:
        self.db.sync_guild_channels(guild.id, list(guild.channels))
        self.db.sync_guild_roles(guild.id, list(guild.roles))
        self.db.sync_guild_members(guild.id, list(guild.members))
        self.db.set_everyone_permissions(guild.id, guild.default_role.permissions.value)

    async def on_guild_join(self, guild: discord.Guild):
        self.db.upsert_bot_guild(guild.id, guild.name)
        self.db.record_bot_event("guild.join", guild.id, None, None, f"name={guild.name}")
        logger.info("joined guild %s (%s)", guild.name, guild.id)
        self._sync_guild_cache(guild)

    async def on_guild_remove(self, guild: discord.Guild):
        self.db.record_bot_event("guild.remove", guild.id, None, None, f"name={guild.name}")
        logger.info("removed from guild %s (%s)", guild.name, guild.id)
        self.db.remove_bot_guild(guild.id)
        self.db.remove_guild_cache(guild.id)

    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        self.db.upsert_bot_channel(channel.guild.id, channel.id, channel.name, channel.type.name, getattr(channel, "position", 0))

    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        self.db.upsert_bot_channel(after.guild.id, after.id, after.name, after.type.name, getattr(after, "position", 0))

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        self.db.remove_bot_channel(channel.guild.id, channel.id)

    async def on_guild_role_create(self, role: discord.Role):
        if not role.is_default():
            self.db.upsert_bot_role(role.guild.id, role.id, role.name, role.position, role.permissions.value, role.managed)

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if after.is_default():
            self.db.set_everyone_permissions(after.guild.id, after.permissions.value)
        else:
            self.db.upsert_bot_role(after.guild.id, after.id, after.name, after.position, after.permissions.value, after.managed)

    async def on_guild_role_delete(self, role: discord.Role):
        self.db.remove_bot_role(role.guild.id, role.id)

    async def on_member_join(self, member: discord.Member):
        self.db.upsert_bot_member(member.guild.id, member.id, member.name, member.display_name, getattr(getattr(member, 'status', None), 'value', 'offline'))

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        self.db.upsert_bot_member(after.guild.id, after.id, after.name, after.display_name, getattr(getattr(after, 'status', None), 'value', 'offline'))

    async def on_member_remove(self, member: discord.Member):
        self.db.remove_bot_member(member.guild.id, member.id)

    async def on_presence_update(self, before: discord.Member, after: discord.Member):
        self.db.update_member_status(
            after.guild.id,
            after.id,
            getattr(getattr(after, "status", None), "value", "offline"),
        )

    async def on_app_command_completion(
        self, interaction: discord.Interaction, command: discord.app_commands.Command | discord.app_commands.ContextMenu
    ):
        command_name = command.qualified_name if command else "unknown"
        guild_id = interaction.guild.id if interaction.guild else None
        actor_id = interaction.user.id if interaction.user else None
        details = {"command": f"/{command_name}"}
        duration_ms = None
        if interaction.created_at:
            duration_ms = max(0, int((discord.utils.utcnow() - interaction.created_at).total_seconds() * 1000))
        correlation_id = f"cmd_{interaction.id}"
        self.db.record_bot_event(
            "command.completed",
            guild_id,
            actor_id,
            None,
            details,
            source="command",
            status="success",
            duration_ms=duration_ms,
            correlation_id=correlation_id,
        )
        logger.info("command completed: /%s guild=%s user=%s", command_name, guild_id, actor_id)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ):
        command_name = interaction.command.qualified_name if interaction.command else "unknown"
        if isinstance(error, discord.app_commands.MissingPermissions):
            perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            content = f"You need the **{perms}** permission to do that."
        elif isinstance(error, utils.CommandDisabledError):
            content = "That command is disabled in this server."
        elif isinstance(error, discord.app_commands.CommandOnCooldown):
            content = f"Slow down - try that again in {error.retry_after:.1f}s."
        else:
            if getattr(error, "__traceback__", None) is not None:
                logger.error(
                    "command '%s' failed",
                    command_name,
                    exc_info=(type(error), error, error.__traceback__),
                )
            else:
                logger.error("command '%s' failed: %s", command_name, error)
            content = "Something went wrong running that command."

        duration_ms = None
        if interaction.created_at:
            duration_ms = max(0, int((discord.utils.utcnow() - interaction.created_at).total_seconds() * 1000))
        self.db.record_bot_event(
            "command.failed",
            interaction.guild.id if interaction.guild else None,
            interaction.user.id if interaction.user else None,
            None,
            {"command": f"/{command_name}", "error": type(error).__name__},
            source="command",
            status="failed",
            duration_ms=duration_ms,
            correlation_id=f"cmd_{interaction.id}",
        )

        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=True)
            else:
                await interaction.response.send_message(content, ephemeral=True)
        except discord.HTTPException:
            pass  # interaction likely already expired, nothing more we can do


    async def close(self):
        # Runs on any clean shutdown (SIGTERM from `docker compose down`/
        # restart, Ctrl+C locally, or an unhandled error propagating out of
        # main()). Logged before the connection actually closes so the
        # timestamp reflects when shutdown was requested, not when the
        # process finally exits.
        self.db.record_bot_event("bot.shutdown", None, None, None, "process stopping")
        logger.info("Shutting down")
        await super().close()


async def main():
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("Set the DISCORD_TOKEN env var")

    bot = MyBot()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
