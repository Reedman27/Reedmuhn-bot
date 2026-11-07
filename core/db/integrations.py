"""YouTube notifications, AI integrations and per-guild music settings."""
import json
import logging
from typing import Optional

logger = logging.getLogger("db")


class IntegrationsMixin:

    # ---- youtube notifications ----

    def add_youtube_watch(self, guild_id: int, yt_channel_id: str, announce_channel_id: int) -> None:
        # Re-adding an already-watched channel (e.g. just to change the
        # announce channel) must preserve last_video_id, channel_name,
        # role_id, and the notify/live-channel settings - INSERT OR REPLACE
        # would silently wipe all of those back to defaults, so every
        # preserved field is carried over via a self-referencing subquery
        # instead.
        self.conn.execute(
            """INSERT OR REPLACE INTO youtube_watches
                   (guild_id, yt_channel_id, announce_channel_id, last_video_id, channel_name, role_id,
                    notify_videos, notify_lives, live_announce_channel_id)
               VALUES (
                   ?, ?, ?,
                   (SELECT last_video_id FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?),
                   (SELECT channel_name FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?),
                   (SELECT role_id FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?),
                   COALESCE((SELECT notify_videos FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?), 1),
                   COALESCE((SELECT notify_lives FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?), 1),
                   (SELECT live_announce_channel_id FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?)
               )""",
            (
                guild_id, yt_channel_id, announce_channel_id,
                guild_id, yt_channel_id, guild_id, yt_channel_id,
                guild_id, yt_channel_id, guild_id, yt_channel_id,
                guild_id, yt_channel_id, guild_id, yt_channel_id,
            ),
        )
        self.conn.commit()

    def remove_youtube_watch(self, guild_id: int, yt_channel_id: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?",
            (guild_id, yt_channel_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    _YOUTUBE_WATCH_COLUMNS = (
        "yt_channel_id, announce_channel_id, last_video_id, channel_name, role_id, "
        "notify_videos, notify_lives, live_announce_channel_id"
    )

    def list_youtube_watches(self, guild_id: int) -> list[tuple[str, int, Optional[str], Optional[str], Optional[int], bool, bool, Optional[int]]]:
        cur = self.conn.execute(
            f"SELECT {self._YOUTUBE_WATCH_COLUMNS} FROM youtube_watches WHERE guild_id = ?",
            (guild_id,),
        )
        return [row[:5] + (bool(row[5]), bool(row[6]), row[7]) for row in cur.fetchall()]

    def all_youtube_watches(self) -> list[tuple[int, str, int, Optional[str], Optional[str], Optional[int], bool, bool, Optional[int]]]:
        """Every watch across every guild - used by the background poller."""
        cur = self.conn.execute(f"SELECT guild_id, {self._YOUTUBE_WATCH_COLUMNS} FROM youtube_watches")
        return [row[:6] + (bool(row[6]), bool(row[7]), row[8]) for row in cur.fetchall()]

    def set_youtube_last_video(self, guild_id: int, yt_channel_id: str, video_id: str) -> None:
        self.conn.execute(
            "UPDATE youtube_watches SET last_video_id = ? WHERE guild_id = ? AND yt_channel_id = ?",
            (video_id, guild_id, yt_channel_id),
        )
        self.conn.commit()

    def set_youtube_channel_name(self, guild_id: int, yt_channel_id: str, name: str) -> None:
        self.conn.execute(
            "UPDATE youtube_watches SET channel_name = ? WHERE guild_id = ? AND yt_channel_id = ?",
            (name, guild_id, yt_channel_id),
        )
        self.conn.commit()

    def set_youtube_role(self, guild_id: int, yt_channel_id: str, role_id: Optional[int]) -> None:
        self.conn.execute(
            "UPDATE youtube_watches SET role_id = ? WHERE guild_id = ? AND yt_channel_id = ?",
            (role_id, guild_id, yt_channel_id),
        )
        self.conn.commit()

    def set_youtube_notify(self, guild_id: int, yt_channel_id: str, notify_videos: bool, notify_lives: bool) -> None:
        self.conn.execute(
            "UPDATE youtube_watches SET notify_videos = ?, notify_lives = ? WHERE guild_id = ? AND yt_channel_id = ?",
            (1 if notify_videos else 0, 1 if notify_lives else 0, guild_id, yt_channel_id),
        )
        self.conn.commit()

    def set_youtube_live_channel(self, guild_id: int, yt_channel_id: str, live_announce_channel_id: Optional[int]) -> None:
        self.conn.execute(
            "UPDATE youtube_watches SET live_announce_channel_id = ? WHERE guild_id = ? AND yt_channel_id = ?",
            (live_announce_channel_id, guild_id, yt_channel_id),
        )
        self.conn.commit()


    # ---- AI integrations ----
    def get_ai_config(self, guild_id: int) -> dict:
        row = self.conn.execute(
            "SELECT enabled, provider, base_url, api_key, model, system_prompt, max_tokens, temperature, "
            "use_channel_context, context_message_limit, index_channels, index_message_limit FROM ai_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if row is None:
            return {
                "enabled": False,
                "provider": "openai_compatible",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4o-mini",
                "system_prompt": "You are ReedMuhn, a helpful Discord assistant. Be concise and follow the server context when provided.",
                "max_tokens": 800,
                "temperature": 0.7,
                "use_channel_context": False,
                "context_message_limit": 10,
                "index_channels": False,
                "index_message_limit": 500,
            }
        return {
            "enabled": bool(row[0]), "provider": row[1], "base_url": row[2],
            "api_key": row[3], "model": row[4], "system_prompt": row[5],
            "max_tokens": int(row[6]), "temperature": float(row[7]),
            "use_channel_context": bool(row[8]), "context_message_limit": int(row[9]),
            "index_channels": bool(row[10]), "index_message_limit": int(row[11]),
        }

    def set_ai_config(self, guild_id: int, *, enabled: bool, provider: str, base_url: str,
                      api_key: str, model: str, system_prompt: str, max_tokens: int,
                      temperature: float, use_channel_context: bool = False,
                      context_message_limit: int = 10, index_channels: bool = False,
                      index_message_limit: int = 500) -> None:
        self.conn.execute(
            """INSERT INTO ai_config (guild_id, enabled, provider, base_url, api_key, model, system_prompt, max_tokens, temperature, use_channel_context, context_message_limit, index_channels, index_message_limit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled, provider=excluded.provider,
                 base_url=excluded.base_url, api_key=excluded.api_key, model=excluded.model,
                 system_prompt=excluded.system_prompt, max_tokens=excluded.max_tokens, temperature=excluded.temperature,
                 use_channel_context=excluded.use_channel_context, context_message_limit=excluded.context_message_limit,
                 index_channels=excluded.index_channels, index_message_limit=excluded.index_message_limit""",
            (guild_id, int(enabled), provider, base_url, api_key, model, system_prompt, max_tokens, temperature,
             int(use_channel_context), context_message_limit, int(index_channels), index_message_limit),
        )
        self.conn.commit()

    def ai_index_message(self, guild_id: int, channel_id: int, message_id: int, author_id: int, author_name: str, content: str, created_at: int, keep_per_channel: int = 500) -> None:
        content = (content or "").strip()[:2000]
        if not content:
            return
        self.conn.execute(
            """INSERT INTO ai_channel_messages(guild_id,channel_id,message_id,author_id,author_name,content,created_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(guild_id,message_id) DO UPDATE SET content=excluded.content, author_name=excluded.author_name, created_at=excluded.created_at""",
            (guild_id, channel_id, message_id, author_id, author_name[:100], content, int(created_at)),
        )
        self.conn.execute(
            """DELETE FROM ai_channel_messages WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (PARTITION BY guild_id, channel_id ORDER BY created_at DESC, id DESC) rn
                    FROM ai_channel_messages WHERE guild_id=? AND channel_id=?
                ) WHERE rn > ?
            )""", (guild_id, channel_id, max(50, min(int(keep_per_channel), 5000)))
        )
        self.conn.commit()

    def ai_search_messages(self, guild_id: int, query: str, limit: int = 12) -> list[dict]:
        terms = [t.lower() for t in __import__('re').findall(r"[\w'-]{3,}", query or "")][:8]
        if not terms:
            rows = self.conn.execute(
                "SELECT channel_id, author_name, content, created_at FROM ai_channel_messages WHERE guild_id=? ORDER BY created_at DESC LIMIT ?",
                (guild_id, max(1, min(int(limit), 30))),
            ).fetchall()
        else:
            clauses=[]; params=[guild_id]
            for term in terms:
                clauses.append("LOWER(content) LIKE ?")
                params.append(f"%{term}%")
            rows = self.conn.execute(
                "SELECT channel_id, author_name, content, created_at FROM ai_channel_messages WHERE guild_id=? AND (" + " OR ".join(clauses) + ") ORDER BY created_at DESC LIMIT ?",
                (*params, max(1, min(int(limit), 30))),
            ).fetchall()
        return [{"channel_id": r[0], "author_name": r[1], "content": r[2], "created_at": r[3]} for r in rows]

    def ai_indexed_count(self, guild_id: int) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM ai_channel_messages WHERE guild_id=?", (guild_id,)).fetchone()[0])

    def clear_ai_index(self, guild_id: int) -> int:
        cur = self.conn.execute("DELETE FROM ai_channel_messages WHERE guild_id=?", (guild_id,))
        self.conn.commit()
        return int(cur.rowcount)

    # ---- music (vocard_settings is the bot process's per-guild music
    # settings table, written directly by voicelink.SQLiteMusicDB - the
    # webui reads/writes the same JSON blob shape here since it has no
    # import path to voicelink, only the shared SQLite file) ----
    def get_music_settings(self, guild_id: int) -> dict:
        row = self.conn.execute("SELECT data FROM vocard_settings WHERE guild_id=?", (guild_id,)).fetchone()
        return json.loads(row[0]) if row else {"_id": guild_id}

    def update_music_settings(self, guild_id: int, **fields) -> None:
        settings = self.get_music_settings(guild_id)
        settings.update(fields)
        settings["_id"] = guild_id
        self.conn.execute(
            "INSERT INTO vocard_settings (guild_id, data) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET data = excluded.data",
            (guild_id, json.dumps(settings)),
        )
        self.conn.commit()
