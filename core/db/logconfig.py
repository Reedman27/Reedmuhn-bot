"""Logging config and the channel feed that mirrors messages into the dashboard."""
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("db")


class LoggingMixin:

    # ---- channel feed (mirrors messages into the dashboard) ----

    def is_feed_enabled(self, guild_id: int) -> bool:
        row = self.conn.execute(
            "SELECT message_feed_enabled FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return bool(row[0]) if row else False

    def record_feed_message(self, guild_id: int, channel_id: int, message_id: int, author_id: int,
                             author_name: str, author_avatar: Optional[str], content: str,
                             attachments: list[str], created_at: int) -> None:
        # Re-check opt-in within the INSERT itself. The cog's earlier check is
        # only a fast path; this closes the race where disable/purge happens
        # between that check and the write.
        self.conn.execute(
            """INSERT INTO message_feed
                   (guild_id, channel_id, message_id, author_id, author_name, author_avatar,
                    content, attachments, created_at)
               SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
               WHERE EXISTS (
                   SELECT 1 FROM guild_config
                   WHERE guild_id = ? AND message_feed_enabled = 1
               )""",
            (guild_id, channel_id, message_id, author_id, author_name, author_avatar,
             content, json.dumps(attachments) if attachments else None, created_at, guild_id),
        )
        self.conn.commit()

    def prune_feed_messages(self, keep_per_channel: int = 300, retention_days: int = 0) -> None:
        """Retain recent feed messages only: expire old rows, then cap each
        channel to its most recent `keep_per_channel` messages."""
        keep_per_channel = max(1, int(keep_per_channel))
        retention_days = max(0, int(retention_days))
        if retention_days:
            cutoff = int(time.time()) - retention_days * 86400
            self.conn.execute("DELETE FROM message_feed WHERE created_at < ?", (cutoff,))
        self.conn.execute(
            """DELETE FROM message_feed WHERE id IN (
                   SELECT id FROM (
                       SELECT id, ROW_NUMBER() OVER (
                           PARTITION BY guild_id, channel_id ORDER BY id DESC
                       ) AS rn
                       FROM message_feed
                   ) WHERE rn > ?
               )""",
            (keep_per_channel,),
        )
        self.conn.commit()

    # Dashboard-facing half of Channel Feed: toggling it on/off and reading
    # back what's been recorded. `is_feed_enabled` above is what the bot
    # process actually checks before mirroring a message; `get_feed_enabled`
    # is the same read, named to match the get_/set_ pattern the rest of the
    # dashboard's settings toggles use. Both are kept (rather than picking
    # one and updating every call site) since this file is copied verbatim
    # into core/db.py and both names are already relied on elsewhere.

    def get_feed_enabled(self, guild_id: int) -> bool:
        return self.is_feed_enabled(guild_id)

    def set_feed_enabled(self, guild_id: int, enabled: bool) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, message_feed_enabled)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET message_feed_enabled = excluded.message_feed_enabled""",
            (guild_id, int(enabled)),
        )
        if not enabled:
            # Disabling the opt-in feed also clears this guild's collected
            # message content. This write shares the transaction with the flag.
            self.conn.execute("DELETE FROM message_feed WHERE guild_id = ?", (guild_id,))
        self.conn.commit()

    def list_feed_channels(self, guild_id: int) -> list[dict]:
        """One row per channel that has ever had a message mirrored, newest
        activity first, with a preview of the latest message."""
        rows = self.conn.execute(
            """SELECT mf.channel_id, bc.name, mf.author_name, mf.content, mf.created_at
               FROM message_feed mf
               JOIN (
                   SELECT channel_id, MAX(id) AS max_id
                   FROM message_feed WHERE guild_id = ?
                   GROUP BY channel_id
               ) latest ON latest.channel_id = mf.channel_id AND latest.max_id = mf.id
               LEFT JOIN bot_channels bc ON bc.guild_id = ? AND bc.channel_id = mf.channel_id
               ORDER BY mf.created_at DESC""",
            (guild_id, guild_id),
        ).fetchall()
        return [
            {
                "channel_id": channel_id,
                "channel_name": name or f"deleted-channel-{channel_id}",
                "author_name": author_name,
                "preview": (content[:120] + "…") if content and len(content) > 120 else content,
                "created_at": created_at,
            }
            for channel_id, name, author_name, content, created_at in rows
        ]

    def get_feed_messages(self, guild_id: int, channel_id: int, limit: int = 100) -> list[dict]:
        """Most recent `limit` messages for one channel, oldest first (ready
        to render top-to-bottom like a chat log)."""
        rows = self.conn.execute(
            """SELECT id, message_id, author_id, author_name, author_avatar, content, attachments, created_at
               FROM message_feed WHERE guild_id = ? AND channel_id = ?
               ORDER BY id DESC LIMIT ?""",
            (guild_id, channel_id, limit),
        ).fetchall()
        return [self._feed_row_to_dict(row) for row in reversed(rows)]

    def get_feed_messages_after(self, guild_id: int, channel_id: int, after_id: int, limit: int = 200) -> list[dict]:
        """New messages since `after_id`, oldest first - used by the poll
        endpoint the channel viewer page calls every few seconds."""
        rows = self.conn.execute(
            """SELECT id, message_id, author_id, author_name, author_avatar, content, attachments, created_at
               FROM message_feed WHERE guild_id = ? AND channel_id = ? AND id > ?
               ORDER BY id ASC LIMIT ?""",
            (guild_id, channel_id, after_id, limit),
        ).fetchall()
        return [self._feed_row_to_dict(row) for row in rows]

    @staticmethod
    def _feed_row_to_dict(row) -> dict:
        feed_id, message_id, author_id, author_name, author_avatar, content, attachments, created_at = row
        return {
            "id": feed_id,
            "message_id": message_id,
            "author_id": author_id,
            "author_name": author_name,
            "author_avatar": author_avatar,
            "content": content,
            "attachments": json.loads(attachments) if attachments else [],
            "created_at": created_at,
        }

    def set_welcome_card_enabled(self, guild_id: int, enabled: bool) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, welcome_card_enabled)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET welcome_card_enabled = excluded.welcome_card_enabled""",
            (guild_id, int(enabled)),
        )
        self.conn.commit()

    def set_birthday_channel(self, guild_id: int, channel_id: int) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, birthday_channel_id)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET birthday_channel_id = excluded.birthday_channel_id""",
            (guild_id, channel_id),
        )
        self.conn.commit()

    def set_welcome(self, guild_id: int, channel_id: int, message: str) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, welcome_channel_id, welcome_message)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   welcome_channel_id = excluded.welcome_channel_id,
                   welcome_message = excluded.welcome_message""",
            (guild_id, channel_id, message),
        )
        self.conn.commit()

    def set_autorole(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, autorole_id)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET autorole_id = excluded.autorole_id""",
            (guild_id, role_id),
        )
        self.conn.commit()

    def clear_autorole(self, guild_id: int) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, autorole_id)
               VALUES (?, NULL)
               ON CONFLICT(guild_id) DO UPDATE SET autorole_id = NULL""",
            (guild_id,),
        )
        self.conn.commit()

    def set_sticky_roles_enabled(self, guild_id: int, enabled: bool) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, sticky_roles_enabled)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET sticky_roles_enabled = excluded.sticky_roles_enabled""",
            (guild_id, int(enabled)),
        )
        self.conn.commit()

    # ---- logging: auto-create channel (WebUI asks for a channel to be
    # created for a category, same queue/claim/complete pattern as the
    # muted-role sync above, since only the bot process holds Discord
    # channel-creation permissions) ----

    def queue_log_channel_create(self, guild_id: int, category: str, reason: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO dashboard_log_channel_create_requests (guild_id, category, reason, status, created_at)
               VALUES (?, ?, ?, 'queued', ?)""",
            (guild_id, category, reason, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_log_channel_create_requests(self, limit: int = 10) -> list[tuple[int, int, str, str]]:
        limit = max(1, min(int(limit), 100))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id, category, reason FROM dashboard_log_channel_create_requests WHERE status = 'queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dashboard_log_channel_create_requests SET status='processing', error=NULL WHERE id IN ({placeholders}) AND status='queued'",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise

    def complete_log_channel_create(self, request_id: int, error: str | None = None, channel_id: int | None = None) -> None:
        self.conn.execute(
            """UPDATE dashboard_log_channel_create_requests
               SET status=?, completed_at=?, error=?, channel_id=? WHERE id=?""",
            ('failed' if error else 'completed', int(time.time()), error, channel_id, request_id),
        )
        self.conn.commit()

    def latest_log_channel_create(self, guild_id: int) -> Optional[tuple[str, int, Optional[str], Optional[int], str]]:
        """Most recent create-channel attempt for this guild - lets the
        WebUI show whether a queued request has actually been carried out
        yet, not just stored."""
        row = self.conn.execute(
            """SELECT status, created_at, error, channel_id, category
               FROM dashboard_log_channel_create_requests WHERE guild_id=? ORDER BY id DESC LIMIT 1""",
            (guild_id,),
        ).fetchone()
        return row

    # ---- logging config ----
    # Carl-bot-style category logging: each category (messages, members,
    # moderation, server, voice) routes to its own channel, or is disabled
    # if unset. A channel can also be added to the ignore list so message
    # edit/delete logging skips noise from e.g. a bot-commands channel.

    LOG_CATEGORIES = ("messages", "members", "moderation", "server", "voice")

    def set_log_channel(self, guild_id: int, category: str, channel_id: int) -> None:
        self.conn.execute(
            """INSERT INTO log_channels (guild_id, category, channel_id) VALUES (?, ?, ?)
               ON CONFLICT(guild_id, category) DO UPDATE SET channel_id = excluded.channel_id""",
            (guild_id, category, channel_id),
        )
        self.conn.commit()

    def disable_log_category(self, guild_id: int, category: str) -> None:
        self.conn.execute(
            "DELETE FROM log_channels WHERE guild_id = ? AND category = ?", (guild_id, category)
        )
        self.conn.commit()

    def get_log_channel(self, guild_id: int, category: str) -> Optional[int]:
        row = self.conn.execute(
            "SELECT channel_id FROM log_channels WHERE guild_id = ? AND category = ?", (guild_id, category)
        ).fetchone()
        return row[0] if row else None

    def get_all_log_channels(self, guild_id: int) -> dict:
        cur = self.conn.execute(
            "SELECT category, channel_id FROM log_channels WHERE guild_id = ?", (guild_id,)
        )
        return dict(cur.fetchall())

    def add_ignored_log_channel(self, guild_id: int, channel_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO log_ignored_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id),
        )
        self.conn.commit()

    def remove_ignored_log_channel(self, guild_id: int, channel_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM log_ignored_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def is_log_channel_ignored(self, guild_id: int, channel_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM log_ignored_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)
        ).fetchone()
        return row is not None

    def list_ignored_log_channels(self, guild_id: int) -> list[int]:
        cur = self.conn.execute(
            "SELECT channel_id FROM log_ignored_channels WHERE guild_id = ?", (guild_id,)
        )
        return [row[0] for row in cur.fetchall()]
