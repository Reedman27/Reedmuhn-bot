"""SQLite storage. Kept as one small class rather than an ORM since the
schema is intentionally small and direct - an ORM would add dependency weight
without much benefit for this self-hosted bot.

NOTE: this file is an exact copy of the bot's db.py, duplicated here because
the web UI runs as a separate Docker container/process. If you change the
schema or add a method in the bot's db.py, copy the change here too, or the
two processes' views of the database will drift out of sync.
"""
import json
import logging
import os
import sqlite3
import time
from typing import Optional


logger = logging.getLogger("db")


class Db:
    def __init__(self, path: str = "bot.db"):
        # Make sure the parent directory exists - matters when path points
        # into a mounted volume like /app/data/bot.db that may not have been
        # created yet on first container start.
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        # check_same_thread=False is safe here because discord.py runs a
        # single asyncio event loop in one OS thread - we're never actually
        # touching this connection from two threads at once.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._create_tables()

    def _create_tables(self) -> None:
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS scheduled_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_name TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                run_at INTEGER NOT NULL,
                data TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS custom_commands (
                guild_id INTEGER NOT NULL,
                trigger TEXT NOT NULL,
                response TEXT NOT NULL,
                PRIMARY KEY (guild_id, trigger)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                welcome_channel_id INTEGER,
                welcome_message TEXT,
                autorole_id INTEGER,
                birthday_channel_id INTEGER
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS birthdays (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                month INTEGER NOT NULL,
                day INTEGER NOT NULL,
                last_announced_year INTEGER,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        # guild_config predates the birthday_channel_id column - add it for
        # anyone upgrading from an older db.db without wiping their data.
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(guild_config)")}
        if "birthday_channel_id" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN birthday_channel_id INTEGER")
        if "tempnick_mode" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN tempnick_mode TEXT NOT NULL DEFAULT 'everyone'")
        if "welcome_card_enabled" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN welcome_card_enabled INTEGER NOT NULL DEFAULT 0")
        if "muted_role_id" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_role_id INTEGER")
        if "muted_deny_send_messages" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_deny_send_messages INTEGER NOT NULL DEFAULT 1")
        if "muted_deny_reactions" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_deny_reactions INTEGER NOT NULL DEFAULT 1")
        if "muted_deny_threads" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_deny_threads INTEGER NOT NULL DEFAULT 1")
        if "muted_deny_connect" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_deny_connect INTEGER NOT NULL DEFAULT 1")
        if "muted_deny_speak" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_deny_speak INTEGER NOT NULL DEFAULT 1")
        if "muted_deny_stream" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_deny_stream INTEGER NOT NULL DEFAULT 1")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS tempnick_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS youtube_watches (
                guild_id INTEGER NOT NULL,
                yt_channel_id TEXT NOT NULL,
                announce_channel_id INTEGER NOT NULL,
                last_video_id TEXT,
                PRIMARY KEY (guild_id, yt_channel_id)
            )"""
        )
        # youtube_watches predates channel_name/role_id/mode - add them for
        # anyone upgrading from an older db without wiping their data.
        yt_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(youtube_watches)")}
        if "channel_name" not in yt_cols:
            self.conn.execute("ALTER TABLE youtube_watches ADD COLUMN channel_name TEXT")
        if "role_id" not in yt_cols:
            self.conn.execute("ALTER TABLE youtube_watches ADD COLUMN role_id INTEGER")
        if "mode" not in yt_cols:
            self.conn.execute("ALTER TABLE youtube_watches ADD COLUMN mode TEXT NOT NULL DEFAULT 'embed'")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS reaction_role_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                action TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        # voice_hubs originally allowed only one hub per guild (guild_id was
        # the primary key). Migrate anyone upgrading from that schema to the
        # new one, which allows multiple hubs per guild, before the
        # CREATE TABLE IF NOT EXISTS below (which is a no-op on an existing
        # table, schema and all).
        pk_cols = [row[1] for row in self.conn.execute("PRAGMA table_info(voice_hubs)") if row[5] > 0]
        if pk_cols == ["guild_id"]:
            self.conn.execute("ALTER TABLE voice_hubs RENAME TO voice_hubs_old")
            self.conn.execute(
                """CREATE TABLE voice_hubs (
                    guild_id INTEGER NOT NULL,
                    hub_channel_id INTEGER NOT NULL,
                    PRIMARY KEY (guild_id, hub_channel_id)
                )"""
            )
            self.conn.execute(
                "INSERT INTO voice_hubs (guild_id, hub_channel_id) SELECT guild_id, hub_channel_id FROM voice_hubs_old"
            )
            self.conn.execute("DROP TABLE voice_hubs_old")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS voice_hubs (
                guild_id INTEGER NOT NULL,
                hub_channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, hub_channel_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS temp_voice_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automod_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                block_invites INTEGER NOT NULL DEFAULT 1,
                banned_words TEXT NOT NULL DEFAULT '',
                caps_percent INTEGER NOT NULL DEFAULT 70,
                caps_min_len INTEGER NOT NULL DEFAULT 10,
                mention_threshold INTEGER NOT NULL DEFAULT 5,
                spam_count INTEGER NOT NULL DEFAULT 5,
                spam_window_seconds INTEGER NOT NULL DEFAULT 5,
                duplicate_count INTEGER NOT NULL DEFAULT 4,
                duplicate_window_seconds INTEGER NOT NULL DEFAULT 30,
                violation_mute_threshold INTEGER NOT NULL DEFAULT 3,
                violation_window_seconds INTEGER NOT NULL DEFAULT 3600,
                violation_mute_duration_seconds INTEGER NOT NULL DEFAULT 600
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automod_exempt_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_manager_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automod_violations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, channel_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, role_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_members (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_guilds (
                guild_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS log_channels (
                guild_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, category)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS log_ignored_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS outbound_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )"""
        )
        # Durable institutional memory. These tables are intentionally separate
        # from transient/cache tables so code updates never need to recreate or
        # overwrite the bot's history.
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS member_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor_id INTEGER,
                reason TEXT,
                details TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_member_history_user
               ON member_history (guild_id, user_id, created_at DESC)"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER,
                event_type TEXT NOT NULL,
                actor_id INTEGER,
                target_id INTEGER,
                details TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_bot_events_created
               ON bot_events (created_at DESC)"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS counting (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                current_number INTEGER NOT NULL DEFAULT 0,
                last_user_id INTEGER,
                high_score INTEGER NOT NULL DEFAULT 0,
                save_milestone INTEGER NOT NULL DEFAULT 50,
                max_saves INTEGER NOT NULL DEFAULT 3
            )"""
        )
        # counting predates save_milestone/max_saves - add them for anyone
        # upgrading from an older db without wiping their data.
        counting_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(counting)")}
        if "save_milestone" not in counting_cols:
            self.conn.execute("ALTER TABLE counting ADD COLUMN save_milestone INTEGER NOT NULL DEFAULT 50")
        if "max_saves" not in counting_cols:
            self.conn.execute("ALTER TABLE counting ADD COLUMN max_saves INTEGER NOT NULL DEFAULT 3")
        if "high_score_alerts" not in counting_cols:
            self.conn.execute("ALTER TABLE counting ADD COLUMN high_score_alerts INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS counting_users (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                correct_count INTEGER NOT NULL DEFAULT 0,
                saves INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS reaction_roles (
                guild_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, message_id, emoji)
            )"""
        )
        self.conn.commit()

    # ---- scheduled events (generalized: tempban unbans + reminders both
    # live here, same shape as yagpdb's real ScheduledEvents table) ----

    def insert_scheduled_event(self, event_name: str, guild_id: int, run_at: int, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO scheduled_events (event_name, guild_id, run_at, data) VALUES (?, ?, ?, ?)",
            (event_name, guild_id, run_at, json.dumps(data)),
        )
        self.conn.commit()

    def due_events(self, now: int) -> list[tuple[int, str, int, str]]:
        cur = self.conn.execute(
            "SELECT id, event_name, guild_id, data FROM scheduled_events WHERE run_at <= ?",
            (now,),
        )
        return cur.fetchall()

    def list_scheduled_events(self, guild_id: int, event_name: Optional[str] = None) -> list[tuple[int, str, int, str]]:
        """Returns (id, event_name, run_at, data) for every scheduled event
        for this guild, regardless of whether it's due yet - used by the web
        dashboard to show pending tempbans/reminders. Pass event_name to
        filter to just one kind ('unban' or 'reminder')."""
        if event_name:
            cur = self.conn.execute(
                "SELECT id, event_name, run_at, data FROM scheduled_events WHERE guild_id = ? AND event_name = ? ORDER BY run_at",
                (guild_id, event_name),
            )
        else:
            cur = self.conn.execute(
                "SELECT id, event_name, run_at, data FROM scheduled_events WHERE guild_id = ? ORDER BY run_at",
                (guild_id,),
            )
        return cur.fetchall()

    def get_scheduled_event(self, event_id: int, guild_id: int, event_name: Optional[str] = None):
        query = "SELECT id, event_name, run_at, data FROM scheduled_events WHERE id = ? AND guild_id = ?"
        params = [event_id, guild_id]
        if event_name:
            query += " AND event_name = ?"
            params.append(event_name)
        return self.conn.execute(query, params).fetchone()

    def delete_scheduled_event(self, event_id: int, guild_id: Optional[int] = None) -> bool:
        if guild_id is None:
            cur = self.conn.execute("DELETE FROM scheduled_events WHERE id = ?", (event_id,))
        else:
            cur = self.conn.execute("DELETE FROM scheduled_events WHERE id = ? AND guild_id = ?", (event_id, guild_id))
        self.conn.commit()
        return cur.rowcount > 0

    # ---- custom commands ----

    def add_custom_command(self, guild_id: int, trigger: str, response: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO custom_commands (guild_id, trigger, response) VALUES (?, ?, ?)",
            (guild_id, trigger, response),
        )
        self.conn.commit()

    def remove_custom_command(self, guild_id: int, trigger: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM custom_commands WHERE guild_id = ? AND trigger = ?",
            (guild_id, trigger),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_custom_commands(self, guild_id: int) -> list[tuple[str, str]]:
        cur = self.conn.execute(
            "SELECT trigger, response FROM custom_commands WHERE guild_id = ?", (guild_id,)
        )
        return cur.fetchall()

    def lookup_custom_command(self, guild_id: int, trigger: str) -> Optional[str]:
        cur = self.conn.execute(
            "SELECT response FROM custom_commands WHERE guild_id = ? AND trigger = ?",
            (guild_id, trigger),
        )
        row = cur.fetchone()
        return row[0] if row else None

    # ---- reaction roles ----
    # A reaction role binds one emoji on one specific message to one role.
    # `emoji` is stored exactly as discord.py renders it via str(payload.emoji)
    # for both unicode emoji ('🎮') and custom emoji ('<:name:id>' / animated
    # '<a:name:id>'), so lookups on incoming reaction events are a direct
    # string match with no separate unicode/custom-emoji branching needed.

    def add_reaction_role(self, guild_id: int, message_id: int, channel_id: int, emoji: str, role_id: int) -> None:
        self.conn.execute(
            "INSERT INTO reaction_roles (guild_id, message_id, channel_id, emoji, role_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, message_id, emoji) DO UPDATE SET role_id=excluded.role_id, channel_id=excluded.channel_id",
            (guild_id, message_id, channel_id, emoji, role_id),
        )
        self.conn.commit()

    def remove_reaction_role(self, guild_id: int, message_id: int, emoji: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
            (guild_id, message_id, emoji),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def remove_reaction_role_by_id(self, guild_id: int, message_id: int, role_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND role_id = ?",
            (guild_id, message_id, role_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_reaction_role(self, guild_id: int, message_id: int, emoji: str) -> Optional[int]:
        """Returns the role_id bound to this emoji on this message, or None."""
        row = self.conn.execute(
            "SELECT role_id FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
            (guild_id, message_id, emoji),
        ).fetchone()
        return row[0] if row else None

    def list_reaction_roles(self, guild_id: int) -> list[tuple[int, int, str, int]]:
        """Returns (message_id, channel_id, emoji, role_id) for every
        configured reaction role in the guild."""
        cur = self.conn.execute(
            "SELECT message_id, channel_id, emoji, role_id FROM reaction_roles WHERE guild_id = ? "
            "ORDER BY message_id, emoji",
            (guild_id,),
        )
        return cur.fetchall()

    def list_reaction_roles_for_message(self, guild_id: int, message_id: int) -> list[tuple[str, int]]:
        cur = self.conn.execute(
            "SELECT emoji, role_id FROM reaction_roles WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )
        return cur.fetchall()

    def remove_reaction_roles_for_message(self, guild_id: int, message_id: int) -> None:
        self.conn.execute(
            "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ?", (guild_id, message_id)
        )
        self.conn.commit()


    # ---- guild config (welcome / autorole) ----

    def get_guild_config(self, guild_id: int) -> dict:
        cur = self.conn.execute(
            """SELECT welcome_channel_id, welcome_message, autorole_id, birthday_channel_id, welcome_card_enabled, muted_role_id,
                      muted_deny_send_messages, muted_deny_reactions, muted_deny_threads,
                      muted_deny_connect, muted_deny_speak, muted_deny_stream
               FROM guild_config WHERE guild_id = ?""",
            (guild_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {
                "welcome_channel_id": None,
                "welcome_message": None,
                "autorole_id": None,
                "birthday_channel_id": None,
                "welcome_card_enabled": False,
                "muted_role_id": None,
                "muted_deny_send_messages": True,
                "muted_deny_reactions": True,
                "muted_deny_threads": True,
                "muted_deny_connect": True,
                "muted_deny_speak": True,
                "muted_deny_stream": True,
            }
        return {
            "welcome_channel_id": row[0],
            "welcome_message": row[1],
            "autorole_id": row[2],
            "birthday_channel_id": row[3],
            "welcome_card_enabled": bool(row[4]),
            "muted_role_id": row[5],
            "muted_deny_send_messages": bool(row[6]),
            "muted_deny_reactions": bool(row[7]),
            "muted_deny_threads": bool(row[8]),
            "muted_deny_connect": bool(row[9]),
            "muted_deny_speak": bool(row[10]),
            "muted_deny_stream": bool(row[11]),
        }

    def set_muted_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, muted_role_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET muted_role_id = excluded.muted_role_id""",
            (guild_id, role_id),
        )
        self.conn.commit()

    def set_muted_settings(self, guild_id: int, *, deny_send_messages: bool, deny_reactions: bool,
                           deny_threads: bool, deny_connect: bool, deny_speak: bool, deny_stream: bool) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (
                   guild_id, muted_deny_send_messages, muted_deny_reactions, muted_deny_threads,
                   muted_deny_connect, muted_deny_speak, muted_deny_stream
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   muted_deny_send_messages = excluded.muted_deny_send_messages,
                   muted_deny_reactions = excluded.muted_deny_reactions,
                   muted_deny_threads = excluded.muted_deny_threads,
                   muted_deny_connect = excluded.muted_deny_connect,
                   muted_deny_speak = excluded.muted_deny_speak,
                   muted_deny_stream = excluded.muted_deny_stream""",
            (guild_id, int(deny_send_messages), int(deny_reactions), int(deny_threads),
             int(deny_connect), int(deny_speak), int(deny_stream)),
        )
        self.conn.commit()

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

    # ---- tempnick permission rules ----
    # mode is one of: "everyone" (default), "allowlist" (only listed roles
    # can self-tempnick), "denylist" (everyone except listed roles can).
    # This only governs using /tempnick on yourself - changing someone
    # else's nickname always requires the real Manage Nicknames permission
    # regardless of this setting.

    def get_tempnick_mode(self, guild_id: int) -> str:
        row = self.conn.execute(
            "SELECT tempnick_mode FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return row[0] if row else "everyone"

    def set_tempnick_mode(self, guild_id: int, mode: str) -> None:
        if mode not in ("everyone", "allowlist", "denylist"):
            raise ValueError(f"invalid tempnick mode: {mode!r}")
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, tempnick_mode)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET tempnick_mode = excluded.tempnick_mode""",
            (guild_id, mode),
        )
        self.conn.commit()

    def list_tempnick_roles(self, guild_id: int) -> list[int]:
        cur = self.conn.execute("SELECT role_id FROM tempnick_roles WHERE guild_id = ?", (guild_id,))
        return [row[0] for row in cur.fetchall()]

    def add_tempnick_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO tempnick_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id)
        )
        self.conn.commit()

    def remove_tempnick_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "DELETE FROM tempnick_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)
        )
        self.conn.commit()

    # ---- warns ----

    def add_warn(self, guild_id: int, user_id: int, moderator_id: int, reason: str, created_at: int) -> int:
        cur = self.conn.execute(
            """INSERT INTO warns (guild_id, user_id, moderator_id, reason, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (guild_id, user_id, moderator_id, reason, created_at),
        )
        self.conn.commit()
        self.record_member_history(guild_id, user_id, "warn", moderator_id, reason, f"warning_id={cur.lastrowid}", created_at)
        return cur.lastrowid

    def list_warns(self, guild_id: int, user_id: int) -> list[tuple[int, int, str, int]]:
        cur = self.conn.execute(
            """SELECT id, moderator_id, reason, created_at FROM warns
               WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC""",
            (guild_id, user_id),
        )
        return cur.fetchall()

    def count_warns(self, guild_id: int, user_id: int) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM warns WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return cur.fetchone()[0]

    def list_warned_users(self, guild_id: int) -> list[tuple[int, int, int]]:
        cur = self.conn.execute(
            """SELECT user_id, COUNT(*), MAX(created_at) FROM warns
               WHERE guild_id = ? GROUP BY user_id ORDER BY MAX(created_at) DESC""",
            (guild_id,),
        )
        return cur.fetchall()

    def remove_warn(self, guild_id: int, warn_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM warns WHERE guild_id = ? AND id = ?",
            (guild_id, warn_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def clear_warns(self, guild_id: int, user_id: int) -> int:
        cur = self.conn.execute(
            "DELETE FROM warns WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self.conn.commit()
        return cur.rowcount

    # ---- durable memory / audit journal ----

    def record_member_history(
        self, guild_id: int, user_id: int, event_type: str, actor_id: Optional[int] = None,
        reason: Optional[str] = None, details: Optional[str] = None, created_at: Optional[int] = None,
    ) -> int:
        created_at = int(time.time()) if created_at is None else int(created_at)
        cur = self.conn.execute(
            """INSERT INTO member_history
               (guild_id, user_id, event_type, actor_id, reason, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, user_id, event_type, actor_id, reason, details, created_at),
        )
        self.conn.commit()
        logger.info("member history: guild=%s user=%s event=%s actor=%s reason=%s", guild_id, user_id, event_type, actor_id, reason)
        return cur.lastrowid

    def list_member_history(self, guild_id: int, user_id: int, limit: int = 100):
        limit = max(1, min(int(limit), 500))
        cur = self.conn.execute(
            """SELECT id, event_type, actor_id, reason, details, created_at
               FROM member_history WHERE guild_id = ? AND user_id = ?
               ORDER BY created_at DESC, id DESC LIMIT ?""",
            (guild_id, user_id, limit),
        )
        return cur.fetchall()

    def record_bot_event(
        self, event_type: str, guild_id: Optional[int] = None, actor_id: Optional[int] = None,
        target_id: Optional[int] = None, details: Optional[str] = None, created_at: Optional[int] = None,
    ) -> int:
        created_at = int(time.time()) if created_at is None else int(created_at)
        cur = self.conn.execute(
            """INSERT INTO bot_events
               (guild_id, event_type, actor_id, target_id, details, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, event_type, actor_id, target_id, details, created_at),
        )
        self.conn.commit()
        logger.info("bot event: type=%s guild=%s actor=%s target=%s details=%s", event_type, guild_id, actor_id, target_id, details)
        return cur.lastrowid

    def recent_bot_events(self, limit: int = 500):
        limit = max(1, min(int(limit), 5000))
        cur = self.conn.execute(
            """SELECT id, guild_id, event_type, actor_id, target_id, details, created_at
               FROM bot_events ORDER BY created_at DESC, id DESC LIMIT ?""",
            (limit,),
        )
        return cur.fetchall()

    # ---- birthdays ----

    def set_birthday(self, guild_id: int, user_id: int, month: int, day: int) -> None:
        self.conn.execute(
            """INSERT INTO birthdays (guild_id, user_id, month, day, last_announced_year)
               VALUES (?, ?, ?, ?, NULL)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET
                   month = excluded.month, day = excluded.day, last_announced_year = NULL""",
            (guild_id, user_id, month, day),
        )
        self.conn.commit()

    def remove_birthday(self, guild_id: int, user_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM birthdays WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_birthday(self, guild_id: int, user_id: int) -> Optional[tuple[int, int]]:
        cur = self.conn.execute(
            "SELECT month, day FROM birthdays WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def list_birthdays(self, guild_id: int) -> list[tuple[int, int, int]]:
        cur = self.conn.execute(
            "SELECT user_id, month, day FROM birthdays WHERE guild_id = ? ORDER BY month, day",
            (guild_id,),
        )
        return cur.fetchall()

    def birthdays_today(self, guild_id: int, month: int, day: int, year: int) -> list[int]:
        """Returns user_ids whose birthday is today and haven't been
        announced yet this year (restart-safe, checked by the caller each
        loop tick rather than relying on the loop only firing once)."""
        cur = self.conn.execute(
            """SELECT user_id FROM birthdays
               WHERE guild_id = ? AND month = ? AND day = ?
               AND (last_announced_year IS NULL OR last_announced_year != ?)""",
            (guild_id, month, day, year),
        )
        return [row[0] for row in cur.fetchall()]

    def mark_birthday_announced(self, guild_id: int, user_id: int, year: int) -> None:
        self.conn.execute(
            "UPDATE birthdays SET last_announced_year = ? WHERE guild_id = ? AND user_id = ?",
            (year, guild_id, user_id),
        )
        self.conn.commit()

    def all_guild_ids_with_birthdays(self) -> list[int]:
        cur = self.conn.execute("SELECT DISTINCT guild_id FROM birthdays")
        return [row[0] for row in cur.fetchall()]

    # ---- counting ----

    def get_counting(self, guild_id: int) -> Optional[dict]:
        cur = self.conn.execute(
            """SELECT channel_id, current_number, last_user_id, high_score, save_milestone, max_saves,
                      high_score_alerts
               FROM counting WHERE guild_id = ?""",
            (guild_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "channel_id": row[0],
            "current_number": row[1],
            "last_user_id": row[2],
            "high_score": row[3],
            "save_milestone": row[4],
            "max_saves": row[5],
            "high_score_alerts": bool(row[6]),
        }

    def set_high_score_alerts(self, guild_id: int, enabled: bool) -> None:
        self.conn.execute(
            """INSERT INTO counting (guild_id, channel_id, high_score_alerts) VALUES (?, 0, ?)
               ON CONFLICT(guild_id) DO UPDATE SET high_score_alerts = excluded.high_score_alerts""",
            (guild_id, int(enabled)),
        )
        self.conn.commit()

    def set_counting_channel(self, guild_id: int, channel_id: int) -> None:
        # Changing the channel only touches channel_id - existing progress
        # and high score for this guild are preserved, matching how the
        # reference count-bot's /channel behaves.
        self.conn.execute(
            """INSERT INTO counting (guild_id, channel_id, current_number, last_user_id, high_score)
               VALUES (?, ?, 0, NULL, 0)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id""",
            (guild_id, channel_id),
        )
        self.conn.commit()

    def set_save_settings(self, guild_id: int, milestone: int, max_saves: int) -> None:
        self.conn.execute(
            """INSERT INTO counting (guild_id, channel_id, save_milestone, max_saves)
               VALUES (?, 0, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   save_milestone = excluded.save_milestone,
                   max_saves = excluded.max_saves""",
            (guild_id, milestone, max_saves),
        )
        self.conn.commit()

    def advance_count(self, guild_id: int, new_number: int, user_id: int) -> None:
        self.conn.execute(
            """UPDATE counting SET
                   current_number = ?,
                   last_user_id = ?,
                   high_score = MAX(high_score, ?)
               WHERE guild_id = ?""",
            (new_number, user_id, new_number, guild_id),
        )
        self.conn.commit()

    def reset_count(self, guild_id: int) -> None:
        self.conn.execute(
            "UPDATE counting SET current_number = 0, last_user_id = NULL WHERE guild_id = ?",
            (guild_id,),
        )
        self.conn.commit()
        self.record_bot_event("counting.reset", guild_id, None, None, "current_number=0")
        self.record_bot_event("counting.advance", guild_id, user_id, None, f"number={new_number}")

    # ---- counting saves (earned by personal correct-count milestones) ----

    def get_user_counting_stats(self, guild_id: int, user_id: int) -> dict:
        cur = self.conn.execute(
            "SELECT correct_count, saves FROM counting_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        row = cur.fetchone()
        return {"correct_count": row[0], "saves": row[1]} if row else {"correct_count": 0, "saves": 0}

    def record_correct_count(self, guild_id: int, user_id: int, milestone: int, max_saves: int) -> tuple[int, int, bool]:
        """Increments this user's lifetime correct-count for the guild, and
        grants a save if they just crossed a milestone (capped at
        max_saves). Returns (new_correct_count, new_saves, earned_a_save)."""
        self.conn.execute(
            """INSERT INTO counting_users (guild_id, user_id, correct_count, saves)
               VALUES (?, ?, 1, 0)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET correct_count = correct_count + 1""",
            (guild_id, user_id),
        )
        stats = self.get_user_counting_stats(guild_id, user_id)
        earned = False
        if milestone > 0 and stats["correct_count"] % milestone == 0 and stats["saves"] < max_saves:
            self.conn.execute(
                "UPDATE counting_users SET saves = saves + 1 WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
            stats["saves"] += 1
            earned = True
        self.conn.commit()
        return stats["correct_count"], stats["saves"], earned

    def use_save(self, guild_id: int, user_id: int) -> int:
        """Consumes one save (caller must have already checked saves > 0).
        Returns the number of saves remaining."""
        self.conn.execute(
            "UPDATE counting_users SET saves = saves - 1 WHERE guild_id = ? AND user_id = ? AND saves > 0",
            (guild_id, user_id),
        )
        self.conn.commit()
        return self.get_user_counting_stats(guild_id, user_id)["saves"]

    # ---- youtube notifications ----

    def add_youtube_watch(self, guild_id: int, yt_channel_id: str, announce_channel_id: int) -> None:
        # Re-adding an already-watched channel (e.g. just to change the
        # announce channel) must preserve last_video_id, channel_name,
        # role_id, and mode - INSERT OR REPLACE would silently wipe all of
        # those back to defaults, so every preserved field is carried over
        # via a self-referencing subquery instead.
        self.conn.execute(
            """INSERT OR REPLACE INTO youtube_watches
                   (guild_id, yt_channel_id, announce_channel_id, last_video_id, channel_name, role_id, mode)
               VALUES (
                   ?, ?, ?,
                   (SELECT last_video_id FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?),
                   (SELECT channel_name FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?),
                   (SELECT role_id FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?),
                   COALESCE((SELECT mode FROM youtube_watches WHERE guild_id = ? AND yt_channel_id = ?), 'embed')
               )""",
            (
                guild_id, yt_channel_id, announce_channel_id,
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

    def list_youtube_watches(self, guild_id: int) -> list[tuple[str, int, Optional[str], Optional[str], Optional[int], str]]:
        cur = self.conn.execute(
            """SELECT yt_channel_id, announce_channel_id, last_video_id, channel_name, role_id, mode
               FROM youtube_watches WHERE guild_id = ?""",
            (guild_id,),
        )
        return cur.fetchall()

    def all_youtube_watches(self) -> list[tuple[int, str, int, Optional[str], Optional[str], Optional[int], str]]:
        """Every watch across every guild - used by the background poller."""
        cur = self.conn.execute(
            """SELECT guild_id, yt_channel_id, announce_channel_id, last_video_id, channel_name, role_id, mode
               FROM youtube_watches"""
        )
        return cur.fetchall()

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

    def set_youtube_mode(self, guild_id: int, yt_channel_id: str, mode: str) -> None:
        self.conn.execute(
            "UPDATE youtube_watches SET mode = ? WHERE guild_id = ? AND yt_channel_id = ?",
            (mode, guild_id, yt_channel_id),
        )
        self.conn.commit()

    # ---- reaction role live-action queue ----
    # The web UI runs in a separate process with no Discord connection, so
    # it can't add/remove a Discord reaction itself. Instead it drops a row
    # here, and the bot's own background loop (reactionroles.py) polls this
    # table and performs the actual Discord API call. This is the same
    # "the database is the only shared state" pattern the rest of the
    # project already uses between the two processes.

    def enqueue_reaction_role_action(
        self, guild_id: int, channel_id: int, message_id: int, emoji: str, action: str
    ) -> None:
        self.conn.execute(
            """INSERT INTO reaction_role_actions (guild_id, channel_id, message_id, emoji, action, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, message_id, emoji, action, int(time.time())),
        )
        self.conn.commit()

    def pop_pending_reaction_role_actions(self, limit: int = 20) -> list[tuple[int, int, int, int, str, str]]:
        """Returns and deletes up to `limit` pending actions in one go, so
        a slow bot-side handler can't process the same row twice."""
        cur = self.conn.execute(
            "SELECT id, guild_id, channel_id, message_id, emoji, action FROM reaction_role_actions ORDER BY id LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ",".join("?" * len(ids))
            self.conn.execute(f"DELETE FROM reaction_role_actions WHERE id IN ({placeholders})", ids)
            self.conn.commit()
        return rows

    # ---- temp voice channels ----

    def add_voice_hub(self, guild_id: int, hub_channel_id: int) -> bool:
        """Returns False if this channel was already a hub (no-op)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO voice_hubs (guild_id, hub_channel_id) VALUES (?, ?)",
            (guild_id, hub_channel_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def is_voice_hub(self, guild_id: int, channel_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM voice_hubs WHERE guild_id = ? AND hub_channel_id = ?", (guild_id, channel_id)
        ).fetchone()
        return row is not None

    def list_voice_hubs(self, guild_id: int) -> list[int]:
        cur = self.conn.execute(
            "SELECT hub_channel_id FROM voice_hubs WHERE guild_id = ?", (guild_id,)
        )
        return [row[0] for row in cur.fetchall()]

    def remove_voice_hub(self, guild_id: int, hub_channel_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM voice_hubs WHERE guild_id = ? AND hub_channel_id = ?", (guild_id, hub_channel_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def add_temp_voice_channel(self, guild_id: int, channel_id: int, owner_id: int) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO temp_voice_channels (channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
            (channel_id, guild_id, owner_id),
        )
        self.conn.commit()

    def is_temp_voice_channel(self, channel_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM temp_voice_channels WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row is not None

    def remove_temp_voice_channel(self, channel_id: int) -> None:
        self.conn.execute("DELETE FROM temp_voice_channels WHERE channel_id = ?", (channel_id,))
        self.conn.commit()

    def list_temp_voice_channels(self, guild_id: int) -> list[tuple[int, int]]:
        cur = self.conn.execute(
            "SELECT channel_id, owner_id FROM temp_voice_channels WHERE guild_id = ?", (guild_id,)
        )
        return cur.fetchall()

    # ---- automod ----

    _AUTOMOD_COLUMNS = (
        "enabled", "block_invites", "banned_words", "caps_percent", "caps_min_len",
        "mention_threshold", "spam_count", "spam_window_seconds",
        "duplicate_count", "duplicate_window_seconds",
        "violation_mute_threshold", "violation_window_seconds", "violation_mute_duration_seconds",
    )

    def get_automod_config(self, guild_id: int) -> dict:
        cols = ", ".join(self._AUTOMOD_COLUMNS)
        row = self.conn.execute(
            f"SELECT {cols} FROM automod_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()

        if row is None:
            # insert a default row so future updates have something to
            # UPDATE against, then re-read it
            self.conn.execute("INSERT INTO automod_config (guild_id) VALUES (?)", (guild_id,))
            self.conn.commit()
            row = self.conn.execute(
                f"SELECT {cols} FROM automod_config WHERE guild_id = ?", (guild_id,)
            ).fetchone()

        result = dict(zip(self._AUTOMOD_COLUMNS, row))
        result["enabled"] = bool(result["enabled"])
        result["block_invites"] = bool(result["block_invites"])
        result["banned_words"] = [w for w in result["banned_words"].split(",") if w.strip()]
        return result

    def set_automod_enabled(self, guild_id: int, enabled: bool) -> None:
        self.get_automod_config(guild_id)  # ensures a row exists
        self.conn.execute(
            "UPDATE automod_config SET enabled = ? WHERE guild_id = ?", (int(enabled), guild_id)
        )
        self.conn.commit()

    def set_automod_invites(self, guild_id: int, block: bool) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET block_invites = ? WHERE guild_id = ?", (int(block), guild_id)
        )
        self.conn.commit()

    def set_automod_words(self, guild_id: int, words: list[str]) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET banned_words = ? WHERE guild_id = ?",
            (",".join(w.strip() for w in words if w.strip()), guild_id),
        )
        self.conn.commit()

    def set_automod_caps(self, guild_id: int, percent: int, min_len: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET caps_percent = ?, caps_min_len = ? WHERE guild_id = ?",
            (percent, min_len, guild_id),
        )
        self.conn.commit()

    def set_automod_mentions(self, guild_id: int, threshold: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET mention_threshold = ? WHERE guild_id = ?", (threshold, guild_id)
        )
        self.conn.commit()

    def set_automod_spam(self, guild_id: int, count: int, window_seconds: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET spam_count = ?, spam_window_seconds = ? WHERE guild_id = ?",
            (count, window_seconds, guild_id),
        )
        self.conn.commit()

    def set_automod_duplicates(self, guild_id: int, count: int, window_seconds: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET duplicate_count = ?, duplicate_window_seconds = ? WHERE guild_id = ?",
            (count, window_seconds, guild_id),
        )
        self.conn.commit()

    def set_automod_escalation(self, guild_id: int, violation_threshold: int, window_seconds: int, mute_duration_seconds: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            """UPDATE automod_config
               SET violation_mute_threshold = ?, violation_window_seconds = ?, violation_mute_duration_seconds = ?
               WHERE guild_id = ?""",
            (violation_threshold, window_seconds, mute_duration_seconds, guild_id),
        )
        self.conn.commit()

    def add_automod_violation(self, guild_id: int, user_id: int, reason: str, created_at: int) -> None:
        self.conn.execute(
            "INSERT INTO automod_violations (guild_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, reason, created_at),
        )
        self.conn.commit()

    def count_recent_automod_violations(self, guild_id: int, user_id: int, since: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM automod_violations WHERE guild_id = ? AND user_id = ? AND created_at >= ?",
            (guild_id, user_id, since),
        ).fetchone()
        return row[0]

    def clear_automod_violations(self, guild_id: int, user_id: int) -> int:
        cur = self.conn.execute(
            "DELETE FROM automod_violations WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        self.conn.commit()
        return cur.rowcount

    # ---- automod exemption roles / bot manager roles ----

    def list_automod_exempt_roles(self, guild_id: int) -> list[int]:
        cur = self.conn.execute(
            "SELECT role_id FROM automod_exempt_roles WHERE guild_id = ? ORDER BY role_id",
            (guild_id,),
        )
        return [row[0] for row in cur.fetchall()]

    def add_automod_exempt_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO automod_exempt_roles (guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id),
        )
        self.conn.commit()

    def remove_automod_exempt_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "DELETE FROM automod_exempt_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        self.conn.commit()

    def list_bot_manager_roles(self, guild_id: int) -> list[int]:
        cur = self.conn.execute(
            "SELECT role_id FROM bot_manager_roles WHERE guild_id = ? ORDER BY role_id",
            (guild_id,),
        )
        return [row[0] for row in cur.fetchall()]

    def add_bot_manager_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO bot_manager_roles (guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id),
        )
        self.conn.commit()

    def remove_bot_manager_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "DELETE FROM bot_manager_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        self.conn.commit()

    # ---- cached Discord objects (used by the web dashboard) ----

    def sync_guild_channels(self, guild_id: int, channels: list) -> None:
        self.conn.execute("DELETE FROM bot_channels WHERE guild_id = ?", (guild_id,))
        for channel in channels:
            channel_type = getattr(getattr(channel, "type", None), "name", str(getattr(channel, "type", "unknown")))
            self.conn.execute(
                "INSERT INTO bot_channels (guild_id, channel_id, name, type, position) VALUES (?, ?, ?, ?, ?)",
                (guild_id, int(channel.id), channel.name, channel_type, int(getattr(channel, "position", 0))),
            )
        self.conn.commit()

    def upsert_bot_channel(self, guild_id: int, channel_id: int, name: str, channel_type: str, position: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO bot_channels (guild_id, channel_id, name, type, position) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, channel_id) DO UPDATE SET name=excluded.name, type=excluded.type, position=excluded.position",
            (guild_id, channel_id, name, channel_type, position),
        )
        self.conn.commit()

    def remove_bot_channel(self, guild_id: int, channel_id: int) -> None:
        self.conn.execute("DELETE FROM bot_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id))
        self.conn.commit()

    def list_bot_channels(self, guild_id: int, channel_type: Optional[str] = None) -> list[tuple[int, str, str, int]]:
        query = "SELECT channel_id, name, type, position FROM bot_channels WHERE guild_id = ?"
        params = [guild_id]
        if channel_type:
            query += " AND type = ?"
            params.append(channel_type)
        query += " ORDER BY LOWER(name), position"
        return self.conn.execute(query, params).fetchall()

    def get_channel_name(self, guild_id: int, channel_id: Optional[int]) -> Optional[str]:
        if channel_id is None:
            return None
        row = self.conn.execute("SELECT name FROM bot_channels WHERE guild_id = ? AND channel_id = ?", (guild_id, channel_id)).fetchone()
        return row[0] if row else None

    def queue_outbound_message(self, guild_id: int, channel_id: int, content: str) -> None:
        self.conn.execute(
            "INSERT INTO outbound_messages (guild_id, channel_id, content, sent, created_at) VALUES (?, ?, ?, 0, ?)",
            (guild_id, channel_id, content, int(time.time())),
        )
        self.conn.commit()

    def sync_guild_roles(self, guild_id: int, roles: list) -> None:
        self.conn.execute("DELETE FROM bot_roles WHERE guild_id = ?", (guild_id,))
        for role in roles:
            if getattr(role, "is_default", lambda: False)():
                continue
            self.conn.execute(
                "INSERT INTO bot_roles (guild_id, role_id, name, position) VALUES (?, ?, ?, ?)",
                (guild_id, int(role.id), role.name, int(getattr(role, "position", 0))),
            )
        self.conn.commit()

    def upsert_bot_role(self, guild_id: int, role_id: int, name: str, position: int = 0) -> None:
        if role_id == guild_id:
            return
        self.conn.execute(
            "INSERT INTO bot_roles (guild_id, role_id, name, position) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, role_id) DO UPDATE SET name=excluded.name, position=excluded.position",
            (guild_id, role_id, name, position),
        )
        self.conn.commit()

    def remove_bot_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute("DELETE FROM bot_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
        self.conn.commit()

    def list_bot_roles(self, guild_id: int) -> list[tuple[int, str, int]]:
        return self.conn.execute("SELECT role_id, name, position FROM bot_roles WHERE guild_id = ? ORDER BY position DESC", (guild_id,)).fetchall()

    def get_role_name(self, guild_id: int, role_id: Optional[int]) -> Optional[str]:
        if role_id is None:
            return None
        row = self.conn.execute("SELECT name FROM bot_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)).fetchone()
        return row[0] if row else None

    def sync_guild_members(self, guild_id: int, members: list) -> None:
        self.conn.execute("DELETE FROM bot_members WHERE guild_id = ?", (guild_id,))
        for member in members:
            self.conn.execute(
                "INSERT INTO bot_members (guild_id, user_id, name, display_name) VALUES (?, ?, ?, ?)",
                (guild_id, int(member.id), member.name, member.display_name),
            )
        self.conn.commit()

    def upsert_bot_member(self, guild_id: int, user_id: int, name: str, display_name: str) -> None:
        self.conn.execute(
            "INSERT INTO bot_members (guild_id, user_id, name, display_name) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET name=excluded.name, display_name=excluded.display_name",
            (guild_id, user_id, name, display_name),
        )
        self.conn.commit()

    def remove_bot_member(self, guild_id: int, user_id: int) -> None:
        self.conn.execute("DELETE FROM bot_members WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        self.conn.commit()

    def list_bot_members(self, guild_id: int) -> list[tuple[int, str, str]]:
        return self.conn.execute("SELECT user_id, display_name, name FROM bot_members WHERE guild_id = ? ORDER BY LOWER(display_name), LOWER(name)", (guild_id,)).fetchall()

    def get_member_name(self, guild_id: int, user_id: Optional[int]) -> Optional[str]:
        if user_id is None:
            return None
        row = self.conn.execute("SELECT display_name FROM bot_members WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)).fetchone()
        return row[0] if row else None

    def remove_guild_cache(self, guild_id: int) -> None:
        self.conn.execute("DELETE FROM bot_channels WHERE guild_id = ?", (guild_id,))
        self.conn.execute("DELETE FROM bot_roles WHERE guild_id = ?", (guild_id,))
        self.conn.execute("DELETE FROM bot_members WHERE guild_id = ?", (guild_id,))
        self.conn.commit()

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

    # ---- bot guild tracking ----
    # The bot writes its actual current guild list here (on_ready, and on
    # every join/leave) so the web UI - a separate process with no Discord
    # connection of its own - can verify a guild_id actually corresponds to
    # a server the bot is in, instead of trusting whatever guild_id shows
    # up in a URL or session.

    def sync_bot_guilds(self, guilds: list) -> None:
        """Full replace - guilds is a list of (guild_id, name). Called on
        on_ready so the list is fully accurate even if guilds changed while
        the bot was offline."""
        self.conn.execute("DELETE FROM bot_guilds")
        self.conn.executemany("INSERT INTO bot_guilds (guild_id, name) VALUES (?, ?)", guilds)
        self.conn.commit()

    def upsert_bot_guild(self, guild_id: int, name: str) -> None:
        self.conn.execute(
            """INSERT INTO bot_guilds (guild_id, name) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET name = excluded.name""",
            (guild_id, name),
        )
        self.conn.commit()

    def remove_bot_guild(self, guild_id: int) -> None:
        self.conn.execute("DELETE FROM bot_guilds WHERE guild_id = ?", (guild_id,))
        self.conn.commit()

    def is_bot_in_guild(self, guild_id: int) -> bool:
        row = self.conn.execute("SELECT 1 FROM bot_guilds WHERE guild_id = ?", (guild_id,)).fetchone()
        return row is not None

    def list_bot_guilds(self) -> list:
        cur = self.conn.execute("SELECT guild_id, name FROM bot_guilds ORDER BY name COLLATE NOCASE")
        return cur.fetchall()

    def get_guild_name(self, guild_id: int) -> Optional[str]:
        row = self.conn.execute("SELECT name FROM bot_guilds WHERE guild_id = ?", (guild_id,)).fetchone()
        return row[0] if row else None
