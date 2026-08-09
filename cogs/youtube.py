"""Announces new YouTube uploads. Uses YouTube's public per-channel RSS feed
(no API key, no quota) rather than the official Data API - the feed only
exposes the ~15 most recent videos and doesn't include everything the real
API would (view counts, etc.), but for "tell me when a new video drops"
that's all we need.
"""
import logging

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger("youtube")

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
CHECK_INTERVAL_MINUTES = 10


def extract_latest_video(feed_text: str):
    """Parses feed XML text and returns (video_id, title, author, url) for
    the newest entry, or None if the feed has no entries / didn't parse.
    Pulled out as a standalone function so it's testable against a fixed
    XML string without needing a live network call.
    """
    feed = feedparser.parse(feed_text)
    if not feed.entries:
        return None
    entry = feed.entries[0]

    video_id = entry.get("yt_videoid")
    if not video_id:
        # fall back to parsing "yt:video:VIDEO_ID" out of the entry id
        entry_id = entry.get("id", "")
        video_id = entry_id.split(":")[-1] if entry_id else None
    if not video_id:
        return None

    title = entry.get("title", "New video")
    author = entry.get("author", "the channel")
    url = f"https://www.youtube.com/watch?v={video_id}"
    return video_id, title, author, url


from utils import manager_or_permission

class YouTube(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self.check_feeds.start()

    def cog_unload(self):
        self.check_feeds.cancel()

    @app_commands.command(name="setyoutube", description="Announce new uploads from a YouTube channel")
    @app_commands.describe(
        channel_id="The YouTube channel's ID (starts with UC...) - not the @handle",
        announce_channel="Where to post new-video announcements",
    )
    @manager_or_permission("manage_guild")
    async def setyoutube(
        self, interaction: discord.Interaction, channel_id: str, announce_channel: discord.TextChannel
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        channel_id = channel_id.strip()
        if not channel_id.startswith("UC") or len(channel_id) < 10:
            await interaction.response.send_message(
                "That doesn't look like a YouTube channel ID - it should start with `UC` "
                "(find it in the channel's page source, or use a lookup tool - not the @handle).",
                ephemeral=True,
            )
            return
        self.bot.db.add_youtube_watch(interaction.guild.id, channel_id, announce_channel.id)
        await interaction.response.send_message(
            f"Watching YouTube channel `{channel_id}` - new uploads will post in {announce_channel.mention}.\n"
            f"(Only videos uploaded *after* this point will be announced - not their existing back catalog.)"
        )

    @app_commands.command(name="removeyoutube", description="Stop announcing a YouTube channel's uploads")
    @app_commands.describe(channel_id="The YouTube channel ID to stop watching")
    @manager_or_permission("manage_guild")
    async def removeyoutube(self, interaction: discord.Interaction, channel_id: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        removed = self.bot.db.remove_youtube_watch(interaction.guild.id, channel_id)
        await interaction.response.send_message(
            "Removed." if removed else "Wasn't watching that channel."
        )

    @app_commands.command(name="listyoutube", description="List YouTube channels being watched")
    async def listyoutube(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        rows = self.bot.db.list_youtube_watches(interaction.guild.id)
        if not rows:
            await interaction.response.send_message("Not watching any YouTube channels yet.")
            return
        lines = [f"`{yt_id}` -> <#{announce_channel_id}>" for yt_id, announce_channel_id, _ in rows]
        await interaction.response.send_message("\n".join(lines))

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_feeds(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()

        for guild_id, yt_channel_id, announce_channel_id, last_video_id in self.bot.db.all_youtube_watches():
            try:
                await self._check_one(guild_id, yt_channel_id, announce_channel_id, last_video_id)
            except Exception:
                logger.exception("failed checking youtube channel %s", yt_channel_id)

    async def _check_one(self, guild_id: int, yt_channel_id: str, announce_channel_id: int, last_video_id):
        url = FEED_URL.format(channel_id=yt_channel_id)
        async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                logger.warning("youtube feed for %s returned status %s", yt_channel_id, resp.status)
                return
            text = await resp.text()

        result = extract_latest_video(text)
        if result is None:
            return
        video_id, title, author, url = result

        if video_id == last_video_id:
            return  # nothing new since last check

        self.bot.db.set_youtube_last_video(guild_id, yt_channel_id, video_id)

        if last_video_id is None:
            # First time watching this channel - just start tracking from
            # here rather than announcing whatever their latest video
            # already was (which could be old and would look like spam).
            return

        channel = self.bot.get_channel(announce_channel_id)
        if channel is None:
            return
        await channel.send(f"📺 New video from **{author}**: {title}\n{url}")

    @check_feeds.before_loop
    async def before_check_feeds(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTube(bot))
