"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


ReedMuhn music integration derived from the MIT-licensed Vocard project.

See THIRD_PARTY_NOTICES.md and voicelink/VOCARD_LICENSE for full attribution.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import voicelink
from voicelink import Config, LangHandler, SQLiteMusicDB
from voicelink.enums import LoopType, SearchType
from voicelink.filters import Equalizer, Filter, Filters, Timescale
from voicelink.lyrics import LYRICS_PLATFORMS
from voicelink.pool import NodePool
from voicelink.exceptions import NoNodesAvailable, VoicelinkException
from voicelink.utils import format_ms, format_to_ms

logger = logging.getLogger("music")


class Music(commands.Cog, name="Music"):
    music = app_commands.Group(name="music", description="Music playback, queues, effects, lyrics, and playlists")
    playlist = app_commands.Group(name="playlist", description="Manage your saved playlists", parent=music)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._node_task: asyncio.Task | None = None
        self._ready = False

    async def cog_load(self):
        # Seed the Vocard-derived configuration from environment variables and
        # ReedMuhn's existing SQLite database is shared by the music subsystem.
        db_path = getattr(self.bot.db, "path", os.environ.get("DB_PATH", "data/bot.db"))
        await SQLiteMusicDB.init(db_path)
        LangHandler.init()
        settings = {
            "nodes": {
                "DEFAULT": {
                    "host": os.environ.get("LAVALINK_HOST", "lavalink"),
                    "port": int(os.environ.get("LAVALINK_PORT", "2333")),
                    "password": os.environ.get("LAVALINK_PASSWORD", "youshallnotpass"),
                    "secure": os.environ.get("LAVALINK_SECURE", "0").lower() in {"1", "true", "yes"},
                    "identifier": "DEFAULT",
                }
            },
            "default_max_queue": int(os.environ.get("MUSIC_MAX_QUEUE", "1000")),
            "default_search_platform": os.environ.get("MUSIC_SEARCH_PLATFORM", "youtube"),
            "lyrics_platform": os.environ.get("MUSIC_LYRICS_PLATFORM", "lrclib"),
            "playlist_settings": {
                "max_playlists": int(os.environ.get("MUSIC_MAX_PLAYLISTS", "10")),
                "max_tracks_per_playlist": int(os.environ.get("MUSIC_MAX_TRACKS", "500")),
                "default_playlist_name": "Favourite",
            },
            "sources_settings": {
                "youtube": {"emoji": "🎵", "color": "FF0000"},
                "soundcloud": {"emoji": "☁️", "color": "FF7700"},
                "twitch": {"emoji": "🟣", "color": "9B4AFF"},
                "others": {"emoji": "🔗", "color": "B3B3B3"},
            },
            "timer_settings": {
                "inactive_player_cleanup": int(os.environ.get("MUSIC_INACTIVE_CLEANUP", "600")),
                "cache_cleanup": 43200,
            },
        }
        Config(settings)
        if not NodePool.nodes:
            self._node_task = asyncio.create_task(self._connect_node())
        # timer_settings.cache_cleanup was already exposed by Config but
        # nothing ever called SQLiteMusicDB.cleanup_cache() on a schedule -
        # without it, per-guild settings/user caches are never evicted, so
        # (a) webui edits to a guild's music settings can be served stale
        # indefinitely and (b) the cache itself grows unbounded across many
        # guilds. Wire it up here at the configured interval (default 12h).
        cleanup_interval = Config().timer_settings.get("cache_cleanup", 43200)
        self._cache_cleanup_loop.change_interval(seconds=cleanup_interval)
        self._cache_cleanup_loop.start()
        self._ready = True

    @tasks.loop(hours=12)
    async def _cache_cleanup_loop(self):
        await SQLiteMusicDB.cleanup_cache()

    @_cache_cleanup_loop.before_loop
    async def _before_cache_cleanup_loop(self):
        await self.bot.wait_until_ready()

    async def cog_unload(self):
        self._cache_cleanup_loop.cancel()
        if self._node_task:
            self._node_task.cancel()
        for node in list(NodePool.nodes.values()):
            for player in list(node.players.values()):
                try:
                    await player.destroy()
                except Exception:
                    logger.exception("Failed to destroy music player during unload")
        NodePool._nodes.clear()

    async def _connect_node(self):
        try:
            await self.bot.wait_until_ready()
            node_cfg = Config().nodes["DEFAULT"]
            await NodePool.create_node(bot=self.bot, **node_cfg, logger=logging.getLogger("music.lavalink"))
            logger.info("Connected to Lavalink node %s:%s", node_cfg["host"], node_cfg["port"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unable to connect to Lavalink; /music will report the node as unavailable")

    @staticmethod
    def _guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise app_commands.CheckFailure("This command only works in a server.")
        return interaction.guild

    def _player(self, guild: discord.Guild) -> Optional[voicelink.Player]:
        vc = guild.voice_client
        return vc if isinstance(vc, voicelink.Player) else None

    async def _require_player(self, interaction: discord.Interaction) -> voicelink.Player:
        guild = self._guild(interaction)
        player = self._player(guild)
        if player is None:
            raise VoicelinkException("I am not connected to a voice channel. Use `/music play` first or `/music connect`.")
        return player

    @staticmethod
    def _in_player_channel(interaction: discord.Interaction, player: voicelink.Player) -> bool:
        voice = getattr(interaction.user, "voice", None)
        return bool(voice and voice.channel and player.channel and voice.channel.id == player.channel.id)

    @staticmethod
    async def _send_error(interaction: discord.Interaction, message: str):
        if interaction.response.is_done():
            await interaction.followup.send(f"❌ {message}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)

    @music.command(name="connect", description="Join your current voice channel")
    async def connect(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None = None):
        await interaction.response.defer()
        try:
            player = await voicelink.connect_channel(interaction, channel)
            await interaction.followup.send(f"🔊 Connected to **{player.channel.name}**.")
        except (VoicelinkException, discord.ClientException) as exc:
            await self._send_error(interaction, str(exc))
        except Exception:
            logger.exception("Music connect failed")
            await self._send_error(interaction, "I couldn't connect to that voice channel.")

    @music.command(name="play", description="Play a song, URL, or playlist")
    @app_commands.describe(query="Song name, URL, or playlist URL", start="Start time such as 1:20", end="Optional end time")
    async def play(self, interaction: discord.Interaction, query: str, start: str = "0", end: str = "0"):
        await interaction.response.defer()
        try:
            guild = self._guild(interaction)
            player = self._player(guild)
            if player is None:
                player = await voicelink.connect_channel(interaction)
            if not self._in_player_channel(interaction, player):
                await self._send_error(interaction, f"Join **{player.channel.name}** before controlling music.")
                return
            tracks = await player.get_tracks(query, requester=interaction.user)
            if not tracks:
                await self._send_error(interaction, "I couldn't find anything for that query.")
                return
            if isinstance(tracks, voicelink.Playlist):
                position = await player.add_track(tracks.tracks, start_time=format_to_ms(start), end_time=format_to_ms(end))
                msg = f"📚 Added **{len(tracks.tracks)}** tracks from **{tracks.name or 'playlist'}**"
                if position and player.is_playing:
                    msg += f" starting at queue position **{position}**"
            else:
                track = tracks[0]
                position = await player.add_track(track, start_time=format_to_ms(start), end_time=format_to_ms(end))
                msg = f"🎵 **[{track.title}]({track.uri})** — `{track.formatted_length}`"
                if position and player.is_playing:
                    msg += f" • queue position **{position}**"
            await interaction.followup.send(msg)
            if not player.is_playing:
                await player.do_next()
        except Exception as exc:
            logger.exception("Music play failed")
            await self._send_error(interaction, str(exc)[:1000])

    @music.command(name="search", description="Search for music without immediately playing it")
    @app_commands.describe(query="What to search for", platform="Search platform")
    @app_commands.choices(platform=[app_commands.Choice(name=x.display_name, value=x.name.lower()) for x in SearchType])
    async def search(self, interaction: discord.Interaction, query: str, platform: app_commands.Choice[str] | None = None):
        await interaction.response.defer(ephemeral=True)
        try:
            node = NodePool.get_node()
            search_type = SearchType.from_platform(platform.value) if platform else Config().search_platform
            tracks = await node.get_tracks(query, requester=interaction.user, search_type=search_type)
            if isinstance(tracks, voicelink.Playlist):
                tracks = tracks.tracks
            if not tracks:
                await interaction.followup.send("No results found.", ephemeral=True)
                return
            lines = [f"**{i}.** [{t.title}]({t.uri}) — `{t.formatted_length}` — {t.author}" for i, t in enumerate(tracks[:10], 1)]
            await interaction.followup.send("🔎 **Search results**\n" + "\n".join(lines), ephemeral=True)
        except Exception as exc:
            await self._send_error(interaction, str(exc)[:1000])

    @music.command(name="nowplaying", description="Show the currently playing song")
    async def nowplaying(self, interaction: discord.Interaction):
        player = await self._require_player(interaction)
        track = player.current
        if not track:
            await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
            return
        embed = discord.Embed(title="🎵 Now playing", description=f"[{track.title}]({track.uri})", color=discord.Color.blurple())
        embed.add_field(name="Artist", value=track.author or "Unknown")
        embed.add_field(name="Progress", value=f"{format_ms(player.position)} / {track.formatted_length}")
        embed.add_field(name="Queue", value=str(player.queue.count))
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        await interaction.response.send_message(embed=embed)

    @music.command(name="queue", description="Show the current queue")
    async def queue(self, interaction: discord.Interaction):
        player = await self._require_player(interaction)
        upcoming = player.queue.tracks()[:15]
        lines = []
        if player.current:
            lines.append(f"▶️ **Now:** [{player.current.title}]({player.current.uri}) — `{player.current.formatted_length}`")
        for i, track in enumerate(upcoming, 1):
            lines.append(f"`{i:02}` [{track.title}]({track.uri}) — `{track.formatted_length}` — {track.requester.mention if track.requester else 'unknown'}")
        if not lines:
            lines.append("The queue is empty.")
        await interaction.response.send_message("🎶 **Queue**\n" + "\n".join(lines))

    @music.command(name="pause", description="Pause the current track")
    async def pause(self, interaction: discord.Interaction):
        player = await self._require_player(interaction)
        if not self._in_player_channel(interaction, player):
            return await self._send_error(interaction, "Join the music voice channel first.")
        await player.set_pause(True, interaction.user)
        await interaction.response.send_message("⏸️ Paused.")

    @music.command(name="resume", description="Resume the current track")
    async def resume(self, interaction: discord.Interaction):
        player = await self._require_player(interaction)
        await player.set_pause(False, interaction.user)
        await interaction.response.send_message("▶️ Resumed.")

    @music.command(name="skip", description="Skip to the next track or a queue position")
    @app_commands.describe(index="Optional 1-based queue position")
    async def skip(self, interaction: discord.Interaction, index: int = 0):
        player = await self._require_player(interaction)
        if index > 0:
            player.queue.skipto(index)
        await player.stop()
        await interaction.response.send_message("⏭️ Skipped." if index <= 0 else f"⏭️ Jumped to queue position **{index}**.")

    @music.command(name="back", description="Play the previous track")
    async def back(self, interaction: discord.Interaction, index: int = 1):
        player = await self._require_player(interaction)
        player.queue.back(index)
        await player.stop()
        await interaction.response.send_message(f"⏮️ Went back **{index}** track(s).")

    @music.command(name="seek", description="Seek within the current track")
    @app_commands.describe(position="Position such as 1:20 or 90")
    async def seek(self, interaction: discord.Interaction, position: str):
        player = await self._require_player(interaction)
        value = format_to_ms(position)
        await player.seek(value, interaction.user)
        await interaction.response.send_message(f"⏩ Seeked to **{position}**.")

    @music.command(name="forward", description="Move forward in the current track")
    async def forward(self, interaction: discord.Interaction, position: str = "10"):
        player = await self._require_player(interaction)
        await player.seek(min(player.position + format_to_ms(position), player.current.length), interaction.user)
        await interaction.response.send_message(f"⏩ Forwarded **{position}**.")

    @music.command(name="rewind", description="Move backward in the current track")
    async def rewind(self, interaction: discord.Interaction, position: str = "10"):
        player = await self._require_player(interaction)
        await player.seek(max(player.position - format_to_ms(position), 0), interaction.user)
        await interaction.response.send_message(f"⏪ Rewound **{position}**.")

    @music.command(name="replay", description="Restart the current track")
    async def replay(self, interaction: discord.Interaction):
        player = await self._require_player(interaction)
        await player.seek(0, interaction.user)
        await interaction.response.send_message("🔁 Restarted the current track.")

    @music.command(name="loop", description="Cycle or choose the repeat mode")
    @app_commands.describe(mode="Off, track, or queue")
    @app_commands.choices(mode=[
        app_commands.Choice(name="Off", value="off"),
        app_commands.Choice(name="Track", value="track"),
        app_commands.Choice(name="Queue", value="queue"),
    ])
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str] | None = None):
        player = await self._require_player(interaction)
        selected = {"off": LoopType.OFF, "track": LoopType.TRACK, "queue": LoopType.QUEUE}.get(mode.value) if mode else None
        result = await player.set_repeat(selected, interaction.user)
        await interaction.response.send_message(f"🔁 Repeat mode: **{result.name.lower()}**.")

    @music.command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, interaction: discord.Interaction):
        player = await self._require_player(interaction)
        await player.shuffle("queue", interaction.user)
        await interaction.response.send_message("🔀 Queue shuffled.")

    @music.command(name="remove", description="Remove a track or range from the queue")
    @app_commands.describe(position1="First queue position", position2="Optional last queue position")
    async def remove(self, interaction: discord.Interaction, position1: int, position2: int | None = None):
        player = await self._require_player(interaction)
        removed = await player.remove_track(position1, position2, requester=interaction.user)
        await interaction.response.send_message(f"🗑️ Removed **{len(removed)}** track(s).")

    @music.command(name="clear", description="Clear the queue or playback history")
    @app_commands.choices(queue=[app_commands.Choice(name="Queue", value="queue"), app_commands.Choice(name="History", value="history")])
    async def clear(self, interaction: discord.Interaction, queue: app_commands.Choice[str] = None):
        player = await self._require_player(interaction)
        target = queue.value if queue else "queue"
        await player.clear_queue(target, interaction.user)
        await interaction.response.send_message(f"🧹 Cleared the **{target}**.")

    @music.command(name="volume", description="Set music volume")
    @app_commands.describe(value="Volume percentage (1-150)")
    async def volume(self, interaction: discord.Interaction, value: app_commands.Range[int, 1, 150]):
        player = await self._require_player(interaction)
        await player.set_volume(value, interaction.user)
        await interaction.response.send_message(f"🔊 Volume: **{value}%**.")

    @music.command(name="leave", description="Disconnect from voice and clear the player")
    async def leave(self, interaction: discord.Interaction):
        player = await self._require_player(interaction)
        await player.destroy()
        await interaction.response.send_message("👋 Left the voice channel.")

    @music.command(name="autoplay", description="Toggle automatic recommendations when the queue empties")
    async def autoplay(self, interaction: discord.Interaction):
        player = await self._require_player(interaction)
        enabled = not player.autoplay
        player.settings["autoplay"] = enabled
        await SQLiteMusicDB.update_settings(interaction.guild.id, {"$set": {"autoplay": enabled}})
        await interaction.response.send_message(f"✨ Autoplay is now **{'on' if enabled else 'off'}**.")

    @music.command(name="lyrics", description="Fetch lyrics for the current or searched song")
    @app_commands.describe(title="Optional title", artist="Optional artist")
    async def lyrics(self, interaction: discord.Interaction, title: str = "", artist: str = ""):
        await interaction.response.defer()
        if not title:
            player = self._player(interaction.guild)
            if not player or not player.current:
                return await self._send_error(interaction, "Nothing is playing, and no title was supplied.")
            title = player.current.title
            artist = player.current.author
        try:
            platform_name = Config().lyrics_platform
            platform_cls = LYRICS_PLATFORMS.get(platform_name, LYRICS_PLATFORMS["lrclib"])
            result = await platform_cls().get_lyrics(title, artist)
            lyrics = (result or {}).get("default") if isinstance(result, dict) else None
            if not lyrics:
                return await self._send_error(interaction, "No lyrics were found.")
            if len(lyrics) > 3900:
                lyrics = lyrics[:3897] + "..."
            await interaction.followup.send(f"🎤 **{title}**{' — ' + artist if artist else ''}\n```text\n{lyrics}\n```")
        except Exception:
            logger.exception("Lyrics lookup failed")
            await self._send_error(interaction, "Lyrics lookup failed.")

    @music.command(name="filter", description="Apply a named Lavalink audio effect")
    @app_commands.describe(effect="Effect preset: nightcore, vaporwave, 8d, karaoke, etc.", clear="Remove all effects first")
    @app_commands.choices(effect=[app_commands.Choice(name=k, value=k) for k in Filters.get_available_filters().keys()])
    async def filter(self, interaction: discord.Interaction, effect: app_commands.Choice[str], clear: bool = False):
        player = await self._require_player(interaction)
        if clear:
            await player.reset_filter(requester=interaction.user)
        cls = Filters.get_available_filters()[effect.value]
        filt = cls()
        await player.add_filter(filt, interaction.user, fast_apply=True)
        await interaction.response.send_message(f"🎛️ Applied **{effect.value}**.")

    @music.command(name="effect-clear", description="Remove one audio effect or reset all effects")
    @app_commands.describe(effect="Optional effect tag; omit to clear all")
    async def effect_clear(self, interaction: discord.Interaction, effect: str = None):
        player = await self._require_player(interaction)
        if effect:
            await player.remove_filter(effect, interaction.user, fast_apply=True)
            msg = f"🧹 Removed **{effect}**."
        else:
            await player.reset_filter(requester=interaction.user, fast_apply=True)
            msg = "🧹 Cleared all effects."
        await interaction.response.send_message(msg)

    @playlist.command(name="create", description="Create a personal playlist")
    @app_commands.describe(name="Playlist name", link="Optional playlist URL to import")
    async def playlist_create(self, interaction: discord.Interaction, name: str, link: str = None):
        user = await SQLiteMusicDB.get_user(interaction.user.id, need_copy=True)
        playlists = user.setdefault("playlist", {})
        max_playlists, _, _ = Config.get_playlist_config()
        if len(playlists) >= max_playlists:
            return await self._send_error(interaction, f"You can have at most {max_playlists} playlists.")
        if any(v.get("name", "").casefold() == name.casefold() for v in playlists.values()):
            return await self._send_error(interaction, "A playlist with that name already exists.")
        new_id = str(max([int(k) for k in playlists.keys() if str(k).isdigit()] or [199]) + 1)
        playlists[new_id] = {"tracks": [], "perms": {"read": [], "write": [], "remove": []}, "name": name[:80], "type": "playlist"}
        if link:
            try:
                node = NodePool.get_node()
                result = await node.get_tracks(link, requester=interaction.user)
                tracks = result.tracks if isinstance(result, voicelink.Playlist) else result
                _, max_tracks, _ = Config.get_playlist_config()
                playlists[new_id]["tracks"] = [t.track_id for t in tracks[:max_tracks]]
            except Exception as exc:
                logger.warning("Playlist link import failed for user %s: %s", interaction.user.id, exc)
        await SQLiteMusicDB.update_user(interaction.user.id, {"$set": {f"playlist.{new_id}": playlists[new_id]}})
        await interaction.response.send_message(f"📂 Created playlist **{name[:80]}**.")

    @playlist.command(name="list", description="List your saved playlists")
    async def playlist_list(self, interaction: discord.Interaction):
        user = await SQLiteMusicDB.get_user(interaction.user.id, need_copy=True)
        playlists = user.get("playlist", {})
        lines = [f"• **{p.get('name', pid)}** — {len(p.get('tracks', []))} tracks" for pid, p in playlists.items()]
        await interaction.response.send_message("📂 **Your playlists**\n" + "\n".join(lines) if lines else "You have no playlists.", ephemeral=True)

    @playlist.command(name="delete", description="Delete one of your playlists")
    @app_commands.describe(name="Exact playlist name")
    async def playlist_delete(self, interaction: discord.Interaction, name: str):
        user = await SQLiteMusicDB.get_user(interaction.user.id, need_copy=True)
        playlists = user.get("playlist", {})
        target = next((pid for pid, p in playlists.items() if p.get("name", "").casefold() == name.casefold() and pid != "200"), None)
        if target is None:
            return await self._send_error(interaction, "I couldn't find that playlist (the Favourite playlist cannot be deleted).")
        await SQLiteMusicDB.update_user(interaction.user.id, {"$unset": {f"playlist.{target}": ""}})
        await interaction.response.send_message(f"🗑️ Deleted **{name}**.")

    @playlist.command(name="add", description="Add a song to one of your playlists")
    @app_commands.describe(name="Exact playlist name", query="Song name or URL")
    async def playlist_add(self, interaction: discord.Interaction, name: str, query: str):
        user = await SQLiteMusicDB.get_user(interaction.user.id, need_copy=True)
        playlists = user.get("playlist", {})
        target_id = next((pid for pid, p in playlists.items() if p.get("name", "").casefold() == name.casefold()), None)
        if target_id is None:
            return await self._send_error(interaction, "Playlist not found.")
        node = NodePool.get_node()
        result = await node.get_tracks(query, requester=interaction.user)
        tracks = result.tracks if isinstance(result, voicelink.Playlist) else result
        if not tracks:
            return await self._send_error(interaction, "No track found.")
        _, max_tracks, _ = Config.get_playlist_config()
        updated = playlists[target_id].get("tracks", []) + [t.track_id for t in tracks]
        updated = updated[:max_tracks]
        await SQLiteMusicDB.update_user(interaction.user.id, {"$set": {f"playlist.{target_id}.tracks": updated}})
        await interaction.response.send_message(f"➕ Added **{len(tracks[:max_tracks])}** track(s) to **{name}**.")

    @playlist.command(name="play", description="Play a saved playlist")
    @app_commands.describe(name="Exact playlist name")
    async def playlist_play(self, interaction: discord.Interaction, name: str):
        player = self._player(interaction.guild) or await voicelink.connect_channel(interaction)
        if not self._in_player_channel(interaction, player):
            return await self._send_error(interaction, "Join the music voice channel first.")
        user = await SQLiteMusicDB.get_user(interaction.user.id, need_copy=True)
        playlists = user.get("playlist", {})
        target = next((p for p in playlists.values() if p.get("name", "").casefold() == name.casefold()), None)
        if not target:
            return await self._send_error(interaction, "Playlist not found.")
        node = NodePool.get_node()
        resolved = []
        for encoded in target.get("tracks", [])[:500]:
            try:
                resolved.append(await node.build_track(encoded, requester=interaction.user))
            except Exception:
                continue
        if not resolved:
            return await self._send_error(interaction, "None of the saved tracks could be loaded anymore.")
        await player.add_track(resolved)
        if not player.is_playing:
            await player.do_next()
        await interaction.response.send_message(f"▶️ Loaded **{len(resolved)}** tracks from **{name}**.")

    async def cog_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CheckFailure):
            return await self._send_error(interaction, str(error))
        if isinstance(error, app_commands.CommandInvokeError):
            error = error.original
        logger.exception("Unhandled music command error", exc_info=error)
        await self._send_error(interaction, str(error)[:1000])


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
