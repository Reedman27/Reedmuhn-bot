"""Channel Feed - mirrors messages into the shared DB so the WebUI dashboard
can show a live view of server channels without opening Discord.

Off by default per server (see guild_config.message_feed_enabled, toggled
from the dashboard's Channel Feed page). Unlike logging_cog's edit/delete
audit trail, which intentionally persists metadata only, this stores message
content - that's the point of the feature - so it's opt-in and each server's
messages are pruned down to a fixed number per channel to keep it from
growing without bound.
"""
import logging

import discord
from discord.ext import commands, tasks

logger = logging.getLogger("channelfeed")

KEEP_PER_CHANNEL = 300
PRUNE_INTERVAL_MINUTES = 15


class ChannelFeed(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.prune_loop.start()

    def cog_unload(self):
        self.prune_loop.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.id == self.bot.user.id:
            return
        if not self.bot.db.is_feed_enabled(message.guild.id):
            return
        if not message.content and not message.attachments:
            return  # nothing worth showing (e.g. a bare embed-only message)
        attachments = [a.url for a in message.attachments]
        self.bot.db.record_feed_message(
            message.guild.id,
            message.channel.id,
            message.id,
            message.author.id,
            str(message.author),
            message.author.display_avatar.url if message.author.display_avatar else None,
            message.content,
            attachments,
            int(message.created_at.timestamp()),
        )

    @tasks.loop(minutes=PRUNE_INTERVAL_MINUTES)
    async def prune_loop(self):
        try:
            self.bot.db.prune_feed_messages(KEEP_PER_CHANNEL)
        except Exception:
            logger.exception("Failed to prune message_feed")

    @prune_loop.before_loop
    async def before_prune_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(ChannelFeed(bot))
