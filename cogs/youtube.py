"""Announces new YouTube uploads. Uses YouTube's public per-channel RSS feed
(no API key, no quota) rather than the official Data API - the feed only
exposes the ~15 most recent videos and doesn't include everything the real
API would (view counts, etc.), but for "tell me when a new video drops"
that's all we need.
"""
import json
import logging
import re
from urllib.parse import urlparse

import aiohttp
import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger("youtube")

FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
CHECK_INTERVAL_MINUTES = 10

CHANNEL_ID_RE = re.compile(r"^UC[\w-]{20,}$")
CHANNEL_ID_IN_HTML_RE = re.compile(r'"channelId":"(UC[\w-]{20,})"')
# Channel pages expose the name via channelMetadataRenderer; video pages
# (someone might paste a video link instead of a channel link) don't have
# that renderer, but do carry the uploader's name as ownerChannelName - try
# both so either kind of pasted link resolves to a friendly display name.
CHANNEL_NAME_PATTERNS = [
    re.compile(r'"channelMetadataRenderer":\{"title":"((?:[^"\\]|\\.)*)"'),
    re.compile(r'"ownerChannelName":"((?:[^"\\]|\\.)*)"'),
]
# A currently-active broadcast carries "isLive":true inside the watch
# page's embedded videoDetails block, which also carries that same video's
# own videoId - anchoring the search to "this video's ID, then isLive
# shortly after" (rather than a bare page-wide search for "isLive":true)
# avoids picking up an unrelated live video mentioned elsewhere on the page
# (autoplay-next/sidebar data can embed other videos' details, and some of
# those could be live even when the one we're checking isn't). The gap is
# generous since JSON key order within videoDetails isn't guaranteed, but
# bounded so it can't match something from a completely different part of
# the page. This is the same "scrape the page, don't need an API key"
# approach resolve_youtube_channel already uses.
_IS_LIVE_SEARCH_WINDOW = 2000


def _is_live_pattern(video_id: str) -> re.Pattern:
    return re.compile(r'"videoId":"%s".{0,%d}?"isLive":true' % (re.escape(video_id), _IS_LIVE_SEARCH_WINDOW), re.DOTALL)


_ALLOWED_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}


def _channel_page_url(raw: str) -> str | None:
    """Turns whatever a normal person would paste - a full channel/video URL,
    an @handle (with or without the @), or an already-known channel ID -
    into a URL worth fetching for its page metadata.

    Returns None for a pasted URL that isn't actually a youtube.com/youtu.be
    link - fetching an arbitrary attacker-supplied URL from the bot's server
    would be a server-side request forgery vector (hitting internal/LAN
    services, cloud metadata endpoints, etc.), so anything off-host is
    rejected outright rather than fetched.
    """
    raw = raw.strip()
    if CHANNEL_ID_RE.match(raw):
        return f"https://www.youtube.com/channel/{raw}"
    if raw.startswith("http://") or raw.startswith("https://"):
        host = (urlparse(raw).hostname or "").lower()
        if host not in _ALLOWED_YOUTUBE_HOSTS:
            return None
        return raw
    return f"https://www.youtube.com/@{raw.lstrip('@')}"


async def resolve_youtube_channel(session: aiohttp.ClientSession, raw: str) -> tuple[str, str | None] | None:
    """Resolves a pasted channel URL, video URL, @handle, or bare channel ID
    into (channel_id, channel_name) by fetching the page and reading its
    metadata. The RSS feed this cog polls only accepts a real UC... channel
    ID, but normal users generally only have a link or handle handy - not
    that ID - so this does the lookup for them. Returns None if nothing
    resolvable was found (bad input, page changed shape, network hiccup).
    """
    url = _channel_page_url(raw)
    if url is None:
        return None
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return None
            html = await resp.text()
    except aiohttp.ClientError:
        return None

    id_match = CHANNEL_ID_IN_HTML_RE.search(html)
    if not id_match:
        return None
    channel_id = id_match.group(1)

    name_match = None
    for pattern in CHANNEL_NAME_PATTERNS:
        name_match = pattern.search(html)
        if name_match:
            break
    name = None
    if name_match:
        try:
            # The title is embedded as a JSON string literal - wrapping it
            # in quotes and running it through json.loads unescapes it
            # properly (unicode escapes, escaped quotes, etc.) rather than
            # guessing at ad-hoc unescaping rules.
            name = json.loads(f'"{name_match.group(1)}"')
        except json.JSONDecodeError:
            name = None

    return channel_id, name


async def is_live_now(session: aiohttp.ClientSession, video_id: str) -> bool:
    """Best-effort check of whether a video is a broadcast that's live right
    now, by scraping its watch page - the RSS feed itself carries no live/
    video distinction. Treated as "not live" (i.e. a normal video) on any
    fetch failure, so a network hiccup never blocks the announcement."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return False
            html = await resp.text()
    except aiohttp.ClientError:
        return False
    return bool(_is_live_pattern(video_id).search(html))


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
        if self.session is not None and not self.session.closed:
            # cog_unload isn't a coroutine, but ClientSession.close() is -
            # schedule it rather than leaving the connector open (and
            # aiohttp complaining about it) until the process exits.
            self.bot.loop.create_task(self.session.close())

    youtube = app_commands.Group(name="youtube", description="YouTube upload/live announcements")

    @youtube.command(name="set", description="Announce new uploads from a YouTube channel")
    @app_commands.describe(
        channel="The YouTube channel's URL, @handle, or channel ID",
        announce_channel="Where to post new-video announcements",
    )
    @manager_or_permission("manage_guild")
    async def setyoutube(
        self, interaction: discord.Interaction, channel: str, announce_channel: discord.TextChannel
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        await interaction.response.defer()  # resolving the channel needs a network round trip

        if self.session is None:
            self.session = aiohttp.ClientSession()
        resolved = await resolve_youtube_channel(self.session, channel)
        if resolved is None:
            await interaction.followup.send(
                "Couldn't find a YouTube channel there - double check the link or handle, or paste "
                "the channel ID directly (starts with `UC`) if you have it."
            )
            return
        channel_id, channel_name = resolved

        self.bot.db.add_youtube_watch(interaction.guild.id, channel_id, announce_channel.id)
        if channel_name:
            self.bot.db.set_youtube_channel_name(interaction.guild.id, channel_id, channel_name)

        display = f"**{channel_name}**" if channel_name else f"`{channel_id}`"
        await interaction.followup.send(
            f"Watching {display} - new uploads will post in {announce_channel.mention}.\n"
            f"(Only videos uploaded *after* this point will be announced - not their existing back catalog.)"
        )

    @youtube.command(name="remove", description="Stop announcing a YouTube channel's uploads")
    @app_commands.describe(channel="The YouTube channel's URL, @handle, or channel ID to stop watching")
    @manager_or_permission("manage_guild")
    async def removeyoutube(self, interaction: discord.Interaction, channel: str):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        await interaction.response.defer()

        raw = channel.strip()
        watched_ids = {yt_id for yt_id, *_rest in self.bot.db.list_youtube_watches(interaction.guild.id)}

        # Already-watched IDs skip the network round trip; anything else
        # (a URL/handle, or an ID that's since gone stale on YouTube's end)
        # gets resolved the same way setyoutube does.
        target_id = raw if raw in watched_ids else None
        if target_id is None:
            if self.session is None:
                self.session = aiohttp.ClientSession()
            resolved = await resolve_youtube_channel(self.session, raw)
            target_id = resolved[0] if resolved else None

        removed = target_id is not None and self.bot.db.remove_youtube_watch(interaction.guild.id, target_id)
        await interaction.followup.send("Removed." if removed else "Wasn't watching that channel.")

    @youtube.command(name="list", description="List YouTube channels being watched")
    async def listyoutube(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        rows = self.bot.db.list_youtube_watches(interaction.guild.id)
        if not rows:
            await interaction.response.send_message("Not watching any YouTube channels yet.")
            return
        lines = []
        for yt_id, announce_channel_id, _last_video_id, channel_name, _role_id, notify_videos, notify_lives, live_announce_channel_id in rows:
            display = channel_name if channel_name else f"`{yt_id}`"
            if notify_videos and notify_lives:
                kind = ""
            elif notify_lives:
                kind = " (live streams only)"
            else:
                kind = " (uploads only)"
            line = f"{display} -> <#{announce_channel_id}>{kind}"
            if notify_lives and live_announce_channel_id and live_announce_channel_id != announce_channel_id:
                line += f" (lives in <#{live_announce_channel_id}>)"
            lines.append(line)
        await interaction.response.send_message("\n".join(lines))

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def check_feeds(self):
        if self.session is None:
            self.session = aiohttp.ClientSession()

        for (guild_id, yt_channel_id, announce_channel_id, last_video_id, _channel_name, role_id,
             notify_videos, notify_lives, live_announce_channel_id) in self.bot.db.all_youtube_watches():
            try:
                await self._check_one(
                    guild_id, yt_channel_id, announce_channel_id, last_video_id,
                    role_id, notify_videos, notify_lives, live_announce_channel_id,
                )
            except Exception:
                logger.exception("failed checking youtube channel %s", yt_channel_id)

    async def _check_one(
        self, guild_id: int, yt_channel_id: str, announce_channel_id: int, last_video_id,
        role_id: int | None, notify_videos: bool, notify_lives: bool, live_announce_channel_id: int | None,
    ):
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

        if last_video_id is None:
            # First time watching this channel - establish a baseline only.
            self.bot.db.set_youtube_last_video(guild_id, yt_channel_id, video_id)
            self.bot.db.record_bot_event("youtube.baseline", guild_id, None, None, f"channel={yt_channel_id} video={video_id}")
            return

        is_live = await is_live_now(self.session, video_id)
        wanted = notify_lives if is_live else notify_videos
        if not wanted:
            # This kind of upload is toggled off for this watch - still
            # advance the cursor so it isn't re-checked (and re-scraped for
            # live status) every poll from here on.
            self.bot.db.set_youtube_last_video(guild_id, yt_channel_id, video_id)
            return

        target_channel_id = announce_channel_id
        if is_live and live_announce_channel_id:
            target_channel_id = live_announce_channel_id
        channel = self.bot.get_channel(target_channel_id)
        if channel is None:
            logger.warning("youtube announcement channel %s is unavailable for guild %s", target_channel_id, guild_id)
            return

        role_mention = f"<@&{role_id}> " if role_id else ""
        if is_live:
            content = f"{role_mention}🔴 **{author}** is live now: {title}\n{url}"
        else:
            content = f"{role_mention}📺 New video from **{author}**: {title}\n{url}"

        # Only advance the cursor after Discord accepts the announcement. A
        # transient Discord outage must not permanently lose a notification.
        await channel.send(content, allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=True))
        self.bot.db.set_youtube_last_video(guild_id, yt_channel_id, video_id)
        event_type = "youtube.live_announced" if is_live else "youtube.announced"
        self.bot.db.record_bot_event(event_type, guild_id, None, target_channel_id, f"channel={yt_channel_id} video={video_id}")

    @check_feeds.before_loop
    async def before_check_feeds(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(YouTube(bot))
