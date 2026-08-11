import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands

import scheduler
from db import Db
from framework import Feature, FeatureStore

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("bot")


def configure_persistent_logging(db_path: str) -> str:
    """Persist application logs beside the durable SQLite database.

    The directory is normally a Docker-mounted volume, so replacing the bot
    code does not replace the log. Rotation prevents a runaway error loop from
    filling the disk while keeping ten historical 10 MB log files.
    """
    data_dir = os.path.dirname(os.path.abspath(db_path)) or os.getcwd()
    os.makedirs(data_dir, exist_ok=True)
    log_path = os.path.join(data_dir, "bot.log")
    root_logger = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", None) == os.path.abspath(log_path) for h in root_logger.handlers):
        handler = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8")
        handler.setFormatter(logging.Formatter(LOG_FORMAT))
        root_logger.addHandler(handler)
    return log_path

INITIAL_COGS = [
    "cogs.fun",
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
    "cogs.logging_cog",
    "cogs.dashboardtalk",
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
])

intents = discord.Intents.default()
intents.message_content = True  # needed for custom commands to see message text
intents.members = True  # needed for on_member_join (welcome/autorole)


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        db_path = os.environ.get("DB_PATH", "data/bot.db")
        configure_persistent_logging(db_path)
        self.db = Db(db_path)

    async def setup_hook(self):
        self.db.record_bot_event("bot.startup", None, None, None, "process starting")
        for cog in INITIAL_COGS:
            await self.load_extension(cog)

        # DEV_GUILD_ID (optional): if set, commands sync instantly to that
        # one server instead of waiting on Discord's global propagation
        # (which can take up to an hour). Set it in .env to your server's
        # ID while actively adding/testing commands; leave it unset for
        # normal global sync once things are stable.
        dev_guild_id = os.environ.get("DEV_GUILD_ID")
        if dev_guild_id:
            guild = discord.Object(id=int(dev_guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info("Synced %d commands instantly to guild %s", len(synced), dev_guild_id)
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d commands globally (can take up to an hour to appear)", len(synced))

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
            self.db.upsert_bot_role(role.guild.id, role.id, role.name, role.position)

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if not after.is_default():
            self.db.upsert_bot_role(after.guild.id, after.id, after.name, after.position)

    async def on_guild_role_delete(self, role: discord.Role):
        self.db.remove_bot_role(role.guild.id, role.id)

    async def on_member_join(self, member: discord.Member):
        self.db.upsert_bot_member(member.guild.id, member.id, member.name, member.display_name)

    async def on_member_update(self, before: discord.Member, after: discord.Member):
        self.db.upsert_bot_member(after.guild.id, after.id, after.name, after.display_name)

    async def on_member_remove(self, member: discord.Member):
        self.db.remove_bot_member(member.guild.id, member.id)

    async def on_app_command_completion(self, interaction: discord.Interaction):
        command = interaction.command.qualified_name if interaction.command else "unknown"
        guild_id = interaction.guild.id if interaction.guild else None
        actor_id = interaction.user.id if interaction.user else None
        details = f"command=/{command}"
        self.db.record_bot_event("command.completed", guild_id, actor_id, None, details)
        logger.info("command completed: /%s guild=%s user=%s", command, guild_id, actor_id)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError
    ):
        if isinstance(error, discord.app_commands.MissingPermissions):
            perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            content = f"You need the **{perms}** permission to do that."
        elif isinstance(error, discord.app_commands.CommandOnCooldown):
            content = f"Slow down - try that again in {error.retry_after:.1f}s."
        else:
            logger.exception(
                "command '%s' failed", interaction.command.name if interaction.command else "?", exc_info=error
            )
            content = "Something went wrong running that command."

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
