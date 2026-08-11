"""Lets the web dashboard "talk" through the bot: an admin types a message
on the dashboard, picks a channel, and this cog's polling loop picks it up
from the DB and sends it into Discord as the bot.

The dashboard and the bot are separate processes (separate Docker
containers) sharing only the SQLite DB, so this is a queue-and-poll handoff
rather than a direct function call - see Db.queue_outbound_message /
list_unsent_outbound_messages / mark_outbound_message_sent in db.py.
"""
import logging

import discord
from discord.ext import commands, tasks

logger = logging.getLogger("dashboardtalk")

POLL_INTERVAL_SECONDS = 2


class DashboardTalk(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_outbound_messages.start()

    def cog_unload(self):
        self.poll_outbound_messages.cancel()

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_outbound_messages(self):
        for message_id, guild_id, channel_id, content in self.bot.db.list_unsent_outbound_messages():
            # Mark sent first - if channel.send() below fails for a reason
            # that won't resolve on its own (channel deleted, no perms), we
            # don't want to retry the same message forever every 2 seconds.
            self.bot.db.mark_outbound_message_sent(message_id)
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                logger.warning("dashboard talk: channel %s not found (guild %s)", channel_id, guild_id)
                continue
            try:
                await channel.send(content)
            except discord.Forbidden:
                logger.warning("dashboard talk: missing permission to send in channel %s", channel_id)
            except discord.HTTPException:
                logger.exception("dashboard talk: failed to send in channel %s", channel_id)

    @poll_outbound_messages.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardTalk(bot))
