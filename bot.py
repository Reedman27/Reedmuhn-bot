import asyncio
import logging
import os
import re
import time
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands

from core import scheduler
from core import utils
from core.db import Db
from core.framework import (
    BootReport, CogResult, Feature, FeatureStore,
    discover_extensions, is_disabled, missing_requirement, parse_disabled,
)
from core.redis_client import RedisClient

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

FEATURES = FeatureStore([
    Feature("moderation", "cogs.moderation.moderation", "Warnings, timeouts, kicks, bans, tempbans, and nickname moderation.", "moderation"),
    Feature("automod", "cogs.moderation.automod", "Automated message and spam protection.", "moderation"),
    Feature("welcome", "cogs.community.welcome", "Welcome messages, cards, and autoroles.", "community"),
    Feature("customcommands", "cogs.utility.customcommands", "Per-server custom text commands.", "utility"),
    Feature("birthdays", "cogs.community.birthdays", "Birthday storage and announcements.", "community"),
    Feature("counting", "cogs.community.counting", "Counting channel and score tracking.", "community"),
    Feature("youtube", "cogs.notifications.youtube", "RSS-based YouTube upload alerts.", "notifications"),
    Feature("tempvoice", "cogs.utility.tempvoice", "Temporary voice-channel management.", "utility"),
    Feature("reactionroles", "cogs.community.reactionroles", "Reaction-based role assignment.", "community"),
    Feature("reminders", "cogs.utility.reminders", "Scheduled reminders.", "utility"),
    Feature("fun", "cogs.fun.fun", "Fun and lightweight community commands.", "fun"),
    Feature("music", "cogs.music.music", "Vocard-derived music playback with queues, playlists, lyrics, effects, and Lavalink.", "utility"),
    Feature("verification", "cogs.moderation.verification", "Button-based member verification gate.", "moderation"),
    Feature("tickets", "cogs.utility.tickets", "Private support ticket channels.", "utility"),
    Feature("polls", "cogs.fun.polls", "Button-based polls with live results.", "fun"),
    Feature("votekick", "cogs.moderation.votekick", "Community vote-to-kick with persistent voting and configurable thresholds.", "moderation"),
    Feature("rules", "cogs.moderation.rules", "Numbered server rules, citable from /warn.", "moderation"),
    Feature("reports", "cogs.moderation.reports", "Member-submitted reports triaged on the dashboard.", "moderation"),
    Feature("emergency", "cogs.moderation.emergency", "Dashboard-triggered server-wide lockdown, invite revoke, and mass timeout.", "moderation"),
    Feature("userinfo", "cogs.utility.userinfo", "Member lookup with /whois.", "utility"),
    Feature("serverinfo", "cogs.utility.serverinfo", "/serverinfo: people vs bots, channels, roles, boosts.", "utility"),
    Feature("antinuke", "cogs.moderation.antinuke", "Automatic detection and punishment of mass-destructive action bursts (channel/role deletion, mass ban/kick, rogue webhooks/bots).", "moderation"),
    Feature("starboard", "cogs.community.starboard", "Highlight messages that receive enough ⭐ reactions.", "community"),
    Feature("suggestions", "cogs.community.suggestions", "Server suggestion intake and staff review.", "community"),
    Feature("ai", "cogs.utility.ai", "Optional OpenAI-compatible AI assistant connections.", "utility"),
    Feature("stickyroles", "cogs.community.stickyroles", "Reapplies configured roles to members who leave and rejoin.", "community"),
    Feature("logging", "cogs.moderation.logging_cog", "Server activity logging (messages, members, moderation, server, voice).", "moderation"),
    Feature("dashboardtalk", "cogs.dashboard.dashboardtalk", "Lets the WebUI send/relay messages through the bot.", "utility"),
    Feature("dashboardmoderation", "cogs.dashboard.dashboardmoderation", "Applies moderation actions queued from the WebUI.", "moderation"),
    Feature("channelfeed", "cogs.dashboard.channelfeed", "Mirrors a channel's messages to the dashboard for read-only monitoring.", "utility"),
    Feature("cases", "cogs.moderation.cases", "Numbered case/infraction system and private staff notes.", "moderation"),
    Feature("raiddetection", "cogs.moderation.raiddetection", "Detects and responds to join-raid bursts.", "moderation"),
    Feature("invites", "cogs.community.invites", "Attributes joins to invite codes and tracks leaderboards/milestones.", "community"),
    Feature("say", "cogs.utility.say", "Owner/Administrator command to post a message as the bot.", "utility"),
    Feature("modmail", "cogs.moderation.modmail", "DM-based modmail threads with staff.", "moderation"),
    Feature("ping", "cogs.utility.ping", "Basic latency check command.", "utility"),
    Feature("extras", "cogs.fun.extras", "XP/leveling, economy, giveaways, live counters, Twitch/RSS notifications.", "fun"),
])

intents = discord.Intents.default()
intents.message_content = True  # needed for custom commands to see message text
intents.members = True  # needed for on_member_join (welcome/autorole)
intents.presences = True  # needed for live online/idle/dnd analytics
intents.moderation = True  # needed for on_audit_log_entry_create (anti-nuke)
intents.invites = True  # needed for on_invite_create/on_invite_delete (invite tracking)


class _NullMusicIPC:
    """No-op IPC adapter used when ReedMuhn does not run Vocard's optional web controller."""

    def __init__(self):
        # voicelink.Player.is_ipc_connected checks self._ipc_client._is_connected
        # directly, so this stub needs the attribute even though it never
        # actually connects anywhere.
        self._is_connected = False

    async def send(self, payload):
        return None


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        db_path = os.environ.get("DB_PATH", "data/bot.db")
        configure_persistent_logging(db_path)
        self.db = Db(db_path)
        # The Vocard-derived player can optionally publish controller events
        # over IPC. ReedMuhn does not need that optional service, so provide a
        # safe no-op adapter rather than letting player construction fail.
        self.ipc_client = _NullMusicIPC()
        # Fast/temporary state (cooldowns, rate limits, automod counters,
        # future webui<->bot signaling) - see core/redis_client.py. SQLite (self.db
        # above) remains the source of truth for anything permanent. Works
        # with REDIS_URL unset or unreachable; it just falls back to an
        # in-memory store for that run.
        self.redis = RedisClient(os.environ.get("REDIS_URL"))
        self.webui_wake_handlers: dict[str, list] = {}
        self._webui_wake_task = None
        self.boot_report = BootReport()
        self._boot_report_posted = False

    def register_webui_wake(self, topic: str, handler) -> None:
        """Register a one-shot poll handler for WebUI -> bot wakeups."""
        self.webui_wake_handlers.setdefault(topic, []).append(handler)

    async def _webui_wake_loop(self):
        """Listen for WebUI queue notifications and run matching pollers."""
        while not self.is_closed():
            if not self.redis.enabled:
                await self.redis.connect()
                if not self.redis.enabled:
                    await asyncio.sleep(5)
                    continue
            try:
                async for payload in self.redis.listen("webui:wake"):
                    topic = payload.get("topic")
                    if not isinstance(topic, str):
                        continue
                    for handler in tuple(self.webui_wake_handlers.get(topic, ())):
                        try:
                            await handler()
                        except Exception:
                            logger.exception("WebUI wake handler failed for topic %s", topic)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("WebUI Redis wake listener failed; reconnecting in 2 seconds")
                await self.redis.close()
                await asyncio.sleep(2)

    async def _load_all_cogs(self) -> BootReport:
        """Find every cog under cogs/<folder>/ and load it, recording the result.

        No list to maintain: a new file in a feature folder is picked up on the
        next boot. A cog that fails to import is logged and skipped - it never
        stops the others from loading.
        """
        report = BootReport()
        disabled = parse_disabled(os.environ.get("DISABLED_COGS"))
        extensions = discover_extensions()
        registered = {f.extension for f in FEATURES.all()}
        report.unregistered = [e for e in extensions if e not in registered]
        for extension in extensions:
            result = CogResult(extension)
            report.results.append(result)
            if is_disabled(extension, disabled):
                result.skipped = True
                logger.info("Cog %s disabled via DISABLED_COGS", extension)
                continue
            needs = missing_requirement(extension, os.environ)
            if needs:
                result.skipped = True
                result.reason = f"off - set {needs} in .env to turn on"
                logger.info("Cog %s is off: %s is not set", extension, needs)
                continue
            started = time.monotonic()
            try:
                await self.load_extension(extension)
                result.ok = True
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"[:300]
                logger.exception("Skipping %s: failed to load", extension)
            result.seconds = time.monotonic() - started
        for extension in report.unregistered:
            logger.warning("%s loaded but is not described in FEATURES in bot.py", extension)
        logger.info("Cogs: %s", report.summary_line())
        return report

    def build_boot_embed(self) -> discord.Embed:
        report = self.boot_report
        failed = bool(report.failed)
        embed = discord.Embed(
            title="ReedMuhn started" if not failed else "ReedMuhn started with errors",
            description=f"**{report.summary_line()}** across {len(report.by_folder())} folders",
            colour=discord.Colour.red() if failed else discord.Colour.green(),
            timestamp=discord.utils.utcnow(),
        )
        for folder, results in report.by_folder().items():
            lines = []
            for r in results:
                if r.skipped:
                    lines.append(f"\u23f8\ufe0f `{r.file}` {r.reason or 'disabled'}")
                elif r.ok:
                    lines.append(f"\u2705 `{r.file}` {r.seconds * 1000:.0f}ms")
                else:
                    lines.append(f"\u274c `{r.file}`\n\u2003{r.error}")
            value = "\n".join(lines)
            if len(value) > 1000:
                value = value[:990] + "\n\u2026"
            embed.add_field(name=f"cogs/{folder}/", value=value, inline=False)
        if report.unregistered:
            embed.add_field(
                name="Not in FEATURES registry",
                value="\n".join(f"`{e}`" for e in report.unregistered)[:1000],
                inline=False,
            )
        embed.set_footer(text=f"{len(self.tree.get_commands())} top-level commands | boot took {time.monotonic() - report.started:.1f}s")
        return embed

    async def _post_boot_report(self) -> None:
        """Post the cog load report to BOOT_LOG_CHANNEL_ID (once per process)."""
        if self._boot_report_posted:
            return
        self._boot_report_posted = True
        raw = os.environ.get("BOOT_LOG_CHANNEL_ID", "").strip()
        if not raw:
            return
        try:
            channel = self.get_channel(int(raw)) or await self.fetch_channel(int(raw))
            await channel.send(embed=self.build_boot_embed())
        except (ValueError, discord.HTTPException, AttributeError):
            logger.warning("Could not post the boot report to BOOT_LOG_CHANNEL_ID=%r (bad ID, or no access)", raw)

    async def setup_hook(self):
        await self.redis.connect()
        self.db.record_bot_event("bot.startup", None, None, None, "process starting")
        self.boot_report = await self._load_all_cogs()

        # Always start the wake listener. The listener itself reconnects when
        # Redis is unavailable, so a temporary Redis outage during bot startup
        # does not permanently disable WebUI wakeups for the lifetime of the bot.
        self._webui_wake_task = self.loop.create_task(
            self._webui_wake_loop(), name="webui-redis-wake"
        )

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
        await self._post_boot_report()
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
        # Server facts the dashboard shows read-only (Overview > Server info).
        me = guild.me
        self.db.set_guild_info(
            guild.id,
            owner_id=guild.owner_id or 0,
            created_at=int(guild.created_at.timestamp()),
            boost_level=guild.premium_tier,
            boost_count=guild.premium_subscription_count or 0,
            verification_level=str(guild.verification_level.name),
            member_total=guild.member_count or 0,
            emoji_count=len(guild.emojis),
            bot_permissions=me.guild_permissions.value if me else 0,
        )

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
        self.db.upsert_bot_member(member.guild.id, member.id, member.name, member.display_name, getattr(getattr(member, 'status', None), 'value', 'offline'), bool(member.bot))

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        self.db.upsert_bot_member(after.guild.id, after.id, after.name, after.display_name, getattr(getattr(after, 'status', None), 'value', 'offline'), bool(after.bot))

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
        if self._webui_wake_task is not None:
            self._webui_wake_task.cancel()
            try:
                await self._webui_wake_task
            except asyncio.CancelledError:
                pass
            self._webui_wake_task = None
        await self.redis.close()
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
