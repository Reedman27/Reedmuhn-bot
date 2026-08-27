"""SQLite storage. Kept as one small class rather than an ORM since the
schema is intentionally small and direct - an ORM would add dependency weight
without much benefit for this self-hosted bot.
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
        # Bot and WebUI are separate processes sharing this SQLite file.
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
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
        if "muted_deny_view_channel" not in cols:
            # Defaults to 0 (not denied) so upgrading servers keep today's
            # behavior - the Muted role has never hidden channels until now.
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_deny_view_channel INTEGER NOT NULL DEFAULT 0")
        if "sticky_roles_enabled" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN sticky_roles_enabled INTEGER NOT NULL DEFAULT 0")
        if "muted_strip_roles" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_strip_roles INTEGER NOT NULL DEFAULT 0")
        if "message_feed_enabled" not in cols:
            # Opt-in per server: mirrors channel messages into the dashboard's
            # Channel Feed page so it can be read without opening Discord.
            # Off by default since it persists message content (everything
            # else in this table is metadata-only).
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN message_feed_enabled INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS message_feed (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                author_name TEXT NOT NULL,
                author_avatar TEXT,
                content TEXT NOT NULL,
                attachments TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_message_feed_channel ON message_feed(guild_id, channel_id, id)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS mute_stripped_roles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_ids TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS tempnick_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, role_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS sticky_roles (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_ids TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS sticky_role_exclusions (
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
        # youtube_watches predates channel_name/role_id - add them for
        # anyone upgrading from an older db without wiping their data.
        yt_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(youtube_watches)")}
        if "channel_name" not in yt_cols:
            self.conn.execute("ALTER TABLE youtube_watches ADD COLUMN channel_name TEXT")
        if "role_id" not in yt_cols:
            self.conn.execute("ALTER TABLE youtube_watches ADD COLUMN role_id INTEGER")
        # notify_videos/notify_lives let a watch announce just uploads, just
        # live streams, or both - both default on so existing watches keep
        # today's "announce everything" behavior. live_announce_channel_id
        # is optional - NULL means lives post in announce_channel_id same
        # as videos, same as before this existed.
        if "notify_videos" not in yt_cols:
            self.conn.execute("ALTER TABLE youtube_watches ADD COLUMN notify_videos INTEGER NOT NULL DEFAULT 1")
        if "notify_lives" not in yt_cols:
            self.conn.execute("ALTER TABLE youtube_watches ADD COLUMN notify_lives INTEGER NOT NULL DEFAULT 1")
        if "live_announce_channel_id" not in yt_cols:
            self.conn.execute("ALTER TABLE youtube_watches ADD COLUMN live_announce_channel_id INTEGER")
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
        # user_limit columns predate this feature - add them for anyone
        # upgrading without wiping their data. 0 means "unlimited", matching
        # Discord's own convention for a voice channel's user limit.
        voice_hub_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(voice_hubs)")}
        if "user_limit" not in voice_hub_cols:
            self.conn.execute("ALTER TABLE voice_hubs ADD COLUMN user_limit INTEGER NOT NULL DEFAULT 0")
        temp_voice_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(temp_voice_channels)")}
        if "user_limit" not in temp_voice_cols:
            self.conn.execute("ALTER TABLE temp_voice_channels ADD COLUMN user_limit INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS temp_voice_delete_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS temp_voice_limit_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_limit INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_purge_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER,
                amount INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                error TEXT
            )"""
        )
        purge_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(dashboard_purge_requests)")}
        purge_migrations = {
            # Carl-style "who actually got purged" breakdown, filled in once
            # the bot process finishes the request. `deleted_count` can differ
            # from `amount` (fewer messages existed, some couldn't be deleted,
            # etc.) so it's tracked separately rather than assumed to match.
            "deleted_count": "ALTER TABLE dashboard_purge_requests ADD COLUMN deleted_count INTEGER",
            "breakdown": "ALTER TABLE dashboard_purge_requests ADD COLUMN breakdown TEXT",
        }
        for col, statement in purge_migrations.items():
            if col not in purge_cols:
                self.conn.execute(statement)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_mod_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                duration_seconds INTEGER,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                error TEXT
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_emergency_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                params TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                error TEXT,
                result TEXT
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS emergency_lockdown_state (
                guild_id INTEGER PRIMARY KEY,
                channel_overwrites TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                started_by INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS config_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                created_by INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_config_snapshots_guild ON config_snapshots(guild_id, id)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_mute_role_sync_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                error TEXT,
                changed INTEGER,
                failed INTEGER
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS verification_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                channel_id INTEGER,
                role_id INTEGER,
                message TEXT NOT NULL DEFAULT 'Click the button below to verify and unlock the rest of the server.',
                message_id INTEGER
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_verify_post_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                error TEXT
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS ticket_config (
                guild_id INTEGER PRIMARY KEY,
                category_id INTEGER,
                support_role_id INTEGER
            )"""
        )
        ticket_cfg_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(ticket_config)")}
        if "panel_channel_id" not in ticket_cfg_cols:
            self.conn.execute("ALTER TABLE ticket_config ADD COLUMN panel_channel_id INTEGER")
        if "panel_message_id" not in ticket_cfg_cols:
            self.conn.execute("ALTER TABLE ticket_config ADD COLUMN panel_message_id INTEGER")
        if "panel_title" not in ticket_cfg_cols:
            self.conn.execute("ALTER TABLE ticket_config ADD COLUMN panel_title TEXT NOT NULL DEFAULT 'Support'")
        if "panel_description" not in ticket_cfg_cols:
            self.conn.execute(
                "ALTER TABLE ticket_config ADD COLUMN panel_description TEXT NOT NULL DEFAULT "
                "'Click the button below to open a private ticket with the support team.'"
            )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                opener_id INTEGER NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL,
                closed_at INTEGER,
                closed_by INTEGER,
                close_reason TEXT
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tickets_guild ON tickets(guild_id, status, id)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_ticket_close_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                ticket_id INTEGER NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                error TEXT
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_ticket_panel_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                error TEXT
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                question TEXT NOT NULL,
                options TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                ends_at INTEGER,
                closed INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_polls_guild ON polls(guild_id, id)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS poll_votes (
                poll_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                PRIMARY KEY (poll_id, user_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_poll_close_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                poll_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                error TEXT
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS webui_login_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                ip TEXT NOT NULL,
                success INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS webui_action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at INTEGER NOT NULL,
                guild_id INTEGER,
                ip TEXT NOT NULL,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                status_code INTEGER NOT NULL
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
                violation_mute_duration_seconds INTEGER NOT NULL DEFAULT 600,
                fuzzy_words INTEGER NOT NULL DEFAULT 0
            )"""
        )
        automod_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(automod_config)")}
        if "fuzzy_words" not in automod_cols:
            self.conn.execute("ALTER TABLE automod_config ADD COLUMN fuzzy_words INTEGER NOT NULL DEFAULT 0")
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
            """CREATE TABLE IF NOT EXISTS automod_escalation_tiers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                threshold INTEGER NOT NULL,
                action TEXT NOT NULL,
                duration_seconds INTEGER,
                UNIQUE(guild_id, threshold)
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
                status TEXT NOT NULL DEFAULT 'offline',
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        member_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(bot_members)")}
        if "status" not in member_cols:
            self.conn.execute("ALTER TABLE bot_members ADD COLUMN status TEXT NOT NULL DEFAULT 'offline'")
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
                created_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at INTEGER NOT NULL DEFAULT 0,
                locked_at INTEGER,
                sent_at INTEGER,
                failed_at INTEGER,
                last_error TEXT,
                discord_message_id INTEGER
            )"""
        )
        outbound_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(outbound_messages)")}
        migrations = {
            "status": "ALTER TABLE outbound_messages ADD COLUMN status TEXT NOT NULL DEFAULT 'queued'",
            "attempts": "ALTER TABLE outbound_messages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0",
            "available_at": "ALTER TABLE outbound_messages ADD COLUMN available_at INTEGER NOT NULL DEFAULT 0",
            "locked_at": "ALTER TABLE outbound_messages ADD COLUMN locked_at INTEGER",
            "sent_at": "ALTER TABLE outbound_messages ADD COLUMN sent_at INTEGER",
            "failed_at": "ALTER TABLE outbound_messages ADD COLUMN failed_at INTEGER",
            "last_error": "ALTER TABLE outbound_messages ADD COLUMN last_error TEXT",
            "discord_message_id": "ALTER TABLE outbound_messages ADD COLUMN discord_message_id INTEGER",
            "deleted_at": "ALTER TABLE outbound_messages ADD COLUMN deleted_at INTEGER",
        }
        for col, statement in migrations.items():
            if col not in outbound_cols:
                self.conn.execute(statement)
        self.conn.execute("UPDATE outbound_messages SET status = 'sent', sent_at = COALESCE(sent_at, created_at) WHERE sent = 1 AND status = 'queued'")
        self.conn.execute("UPDATE outbound_messages SET available_at = created_at WHERE available_at = 0 AND sent = 0")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_outbound_queue ON outbound_messages(status, available_at, id)")
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
            """CREATE TABLE IF NOT EXISTS analytics_settings (
                guild_id INTEGER PRIMARY KEY,
                messages INTEGER NOT NULL DEFAULT 1,
                commands INTEGER NOT NULL DEFAULT 1,
                member_joins INTEGER NOT NULL DEFAULT 1,
                member_leaves INTEGER NOT NULL DEFAULT 1,
                message_edits INTEGER NOT NULL DEFAULT 1,
                message_deletes INTEGER NOT NULL DEFAULT 1,
                reactions INTEGER NOT NULL DEFAULT 1,
                voice_joins INTEGER NOT NULL DEFAULT 1,
                voice_leaves INTEGER NOT NULL DEFAULT 1
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT,
                guild_id INTEGER,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'system',
                status TEXT NOT NULL DEFAULT 'success',
                actor_id INTEGER,
                target_id INTEGER,
                details TEXT,
                duration_ms INTEGER,
                correlation_id TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        # Structured audit fields are additive so existing bot.db files keep
        # all historical events while newer events become searchable and
        # correlatable without parsing log text.
        bot_event_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(bot_events)")}
        for column, definition in (
            ("event_id", "TEXT"),
            ("source", "TEXT NOT NULL DEFAULT 'system'"),
            ("status", "TEXT NOT NULL DEFAULT 'success'"),
            ("duration_ms", "INTEGER"),
            ("correlation_id", "TEXT"),
        ):
            if column not in bot_event_cols:
                self.conn.execute(f"ALTER TABLE bot_events ADD COLUMN {column} {definition}")
        self.conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_events_event_id
               ON bot_events (event_id)"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_bot_events_created
               ON bot_events (created_at DESC)"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_bot_events_guild_created
               ON bot_events (guild_id, created_at DESC)"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_bot_events_type_created
               ON bot_events (guild_id, event_type, created_at DESC)"""
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

        # warns predates notes (batch 2's "evidence/notes on existing cases")
        # and rule_id (batch 1's "reference a numbered rule") - add both for
        # anyone upgrading without wiping their warning history.
        warns_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(warns)")}
        for column, definition in (("notes", "TEXT"), ("rule_id", "INTEGER")):
            if column not in warns_cols:
                self.conn.execute(f"ALTER TABLE warns ADD COLUMN {column} {definition}")

        # ---- server rules ----
        # Numbered rules an admin defines; warnings can optionally cite one
        # (warns.rule_id above). Deliberately no separate "position" column -
        # a rule's displayed number is just its rank in id order, resolved
        # at read time (see get_rule_by_number) - simpler than keeping a
        # position column in sync, and fine at the scale a rules list runs at.
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS server_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                rule_text TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )

        # ---- member reports ----
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                reporter_id INTEGER NOT NULL,
                target_user_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL,
                resolved_by INTEGER,
                resolved_at INTEGER,
                resolution_note TEXT,
                linked_warn_id INTEGER
            )"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_reports_guild_status
               ON reports (guild_id, status, created_at DESC)"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS report_config (
                guild_id INTEGER PRIMARY KEY,
                channel_id INTEGER
            )"""
        )

        # ---- automod review queue ----
        # A fuzzy "alike words" match is inherently less certain than an
        # exact match (see automod_checks.contains_banned_word's docstring),
        # so when a guild opts in (automod_config.queue_fuzzy_matches), a
        # fuzzy-only match lands here for a moderator to confirm or dismiss
        # instead of walking the escalation ladder immediately. The message
        # is still deleted right away either way - only the punishment step
        # waits. content_snapshot preserves what the message said, since the
        # message itself is already gone by the time anyone reviews this.
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automod_review_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rule_label TEXT NOT NULL,
                content_snapshot TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                resolved_by INTEGER,
                resolved_at INTEGER
            )"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_automod_queue_guild_status
               ON automod_review_queue (guild_id, status, created_at DESC)"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automod_queue_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                review_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                resolved_by INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        automod_cfg_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(automod_config)")}
        if "queue_fuzzy_matches" not in automod_cfg_cols:
            self.conn.execute(
                "ALTER TABLE automod_config ADD COLUMN queue_fuzzy_matches INTEGER NOT NULL DEFAULT 0"
            )
        if "block_gifs" not in automod_cfg_cols:
            # Defaults to 0 (off) so upgrading servers keep today's behavior -
            # GIFs have never been touched by automod until now.
            self.conn.execute(
                "ALTER TABLE automod_config ADD COLUMN block_gifs INTEGER NOT NULL DEFAULT 0"
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
                      muted_deny_connect, muted_deny_speak, muted_deny_stream, muted_deny_view_channel,
                      sticky_roles_enabled, muted_strip_roles
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
                "muted_deny_view_channel": False,
                "sticky_roles_enabled": False,
                "muted_strip_roles": False,
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
            "muted_deny_view_channel": bool(row[12]),
            "sticky_roles_enabled": bool(row[13]),
            "muted_strip_roles": bool(row[14]),
        }

    def set_muted_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, muted_role_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET muted_role_id = excluded.muted_role_id""",
            (guild_id, role_id),
        )
        self.conn.commit()

    def set_muted_strip_roles(self, guild_id: int, enabled: bool) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, muted_strip_roles) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET muted_strip_roles = excluded.muted_strip_roles""",
            (guild_id, int(enabled)),
        )
        self.conn.commit()

    def save_stripped_roles(self, guild_id: int, user_id: int, role_ids: list[int]) -> None:
        self.conn.execute(
            """INSERT INTO mute_stripped_roles (guild_id, user_id, role_ids, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET
                   role_ids = excluded.role_ids, created_at = excluded.created_at""",
            (guild_id, user_id, ",".join(str(rid) for rid in role_ids), int(time.time())),
        )
        self.conn.commit()

    def pop_stripped_roles(self, guild_id: int, user_id: int) -> list[int]:
        """Returns and clears the roles stashed for this member by a
        strip-roles mute, or [] if none are stored. "Pop" (not "get") is
        deliberate - once restored (or once someone decides not to
        restore them, e.g. re-muting before the first mute expired), the
        stash shouldn't be reapplied a second time later."""
        row = self.conn.execute(
            "SELECT role_ids FROM mute_stripped_roles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if row is None:
            return []
        self.conn.execute(
            "DELETE FROM mute_stripped_roles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        self.conn.commit()
        return [int(rid) for rid in row[0].split(",") if rid.strip()]

    def set_muted_settings(self, guild_id: int, *, deny_send_messages: bool, deny_reactions: bool,
                           deny_threads: bool, deny_connect: bool, deny_speak: bool, deny_stream: bool,
                           deny_view_channel: bool = False) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (
                   guild_id, muted_deny_send_messages, muted_deny_reactions, muted_deny_threads,
                   muted_deny_connect, muted_deny_speak, muted_deny_stream, muted_deny_view_channel
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET
                   muted_deny_send_messages = excluded.muted_deny_send_messages,
                   muted_deny_reactions = excluded.muted_deny_reactions,
                   muted_deny_threads = excluded.muted_deny_threads,
                   muted_deny_connect = excluded.muted_deny_connect,
                   muted_deny_speak = excluded.muted_deny_speak,
                   muted_deny_stream = excluded.muted_deny_stream,
                   muted_deny_view_channel = excluded.muted_deny_view_channel""",
            (guild_id, int(deny_send_messages), int(deny_reactions), int(deny_threads),
             int(deny_connect), int(deny_speak), int(deny_stream), int(deny_view_channel)),
        )
        self.conn.commit()

    # ---- channel feed (mirrors messages into the dashboard) ----

    def is_feed_enabled(self, guild_id: int) -> bool:
        row = self.conn.execute(
            "SELECT message_feed_enabled FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return bool(row[0]) if row else False

    def record_feed_message(self, guild_id: int, channel_id: int, message_id: int, author_id: int,
                             author_name: str, author_avatar: Optional[str], content: str,
                             attachments: list[str], created_at: int) -> None:
        self.conn.execute(
            """INSERT INTO message_feed
                   (guild_id, channel_id, message_id, author_id, author_name, author_avatar,
                    content, attachments, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, channel_id, message_id, author_id, author_name, author_avatar,
             content, json.dumps(attachments) if attachments else None, created_at),
        )
        self.conn.commit()

    def prune_feed_messages(self, keep_per_channel: int = 300) -> None:
        """Keeps only the most recent `keep_per_channel` rows per channel so
        the table doesn't grow without bound on a busy server."""
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
    # into webui/db.py and both names are already relied on elsewhere.

    def get_feed_enabled(self, guild_id: int) -> bool:
        return self.is_feed_enabled(guild_id)

    def set_feed_enabled(self, guild_id: int, enabled: bool) -> None:
        self.conn.execute(
            """INSERT INTO guild_config (guild_id, message_feed_enabled)
               VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET message_feed_enabled = excluded.message_feed_enabled""",
            (guild_id, int(enabled)),
        )
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

    # ---- sticky roles ----

    def set_sticky_roles(self, guild_id: int, user_id: int, role_ids: list[int]) -> None:
        self.conn.execute(
            """INSERT INTO sticky_roles (guild_id, user_id, role_ids)
               VALUES (?, ?, ?)
               ON CONFLICT(guild_id, user_id) DO UPDATE SET role_ids = excluded.role_ids""",
            (guild_id, user_id, json.dumps(role_ids)),
        )
        self.conn.commit()

    def get_sticky_roles(self, guild_id: int, user_id: int) -> list[int]:
        row = self.conn.execute(
            "SELECT role_ids FROM sticky_roles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if not row:
            return []
        try:
            values = json.loads(row[0])
            return [int(v) for v in values]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []

    def clear_sticky_roles(self, guild_id: int, user_id: int) -> None:
        self.conn.execute(
            "DELETE FROM sticky_roles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self.conn.commit()

    def add_sticky_role_exclusion(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO sticky_role_exclusions (guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id),
        )
        self.conn.commit()

    def remove_sticky_role_exclusion(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "DELETE FROM sticky_role_exclusions WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        self.conn.commit()

    def list_sticky_role_exclusions(self, guild_id: int) -> list[int]:
        cur = self.conn.execute(
            "SELECT role_id FROM sticky_role_exclusions WHERE guild_id = ? ORDER BY role_id",
            (guild_id,),
        )
        return [row[0] for row in cur.fetchall()]

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

    def add_warn(
        self, guild_id: int, user_id: int, moderator_id: int, reason: str, created_at: int,
        rule_id: Optional[int] = None, notes: Optional[str] = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO warns (guild_id, user_id, moderator_id, reason, created_at, rule_id, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, user_id, moderator_id, reason, created_at, rule_id, notes),
        )
        self.conn.commit()
        self.record_member_history(guild_id, user_id, "warn", moderator_id, reason, f"warning_id={cur.lastrowid}", created_at)
        return cur.lastrowid

    def list_warns(self, guild_id: int, user_id: int) -> list[tuple[int, int, str, int, Optional[int], Optional[str]]]:
        cur = self.conn.execute(
            """SELECT id, moderator_id, reason, created_at, rule_id, notes FROM warns
               WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC""",
            (guild_id, user_id),
        )
        return cur.fetchall()

    def get_warn(self, guild_id: int, warn_id: int) -> Optional[tuple]:
        cur = self.conn.execute(
            """SELECT id, user_id, moderator_id, reason, created_at, rule_id, notes FROM warns
               WHERE guild_id = ? AND id = ?""",
            (guild_id, warn_id),
        )
        return cur.fetchone()

    def set_warn_notes(self, guild_id: int, warn_id: int, notes: Optional[str]) -> bool:
        """Appends/attaches evidence-style notes to an existing warning
        (message links, screenshots, context) after the fact - see the
        Reports/Cases feature. Overwrites any previous notes on this warn
        rather than appending, since the WebUI form always shows the
        current notes and submits the full edited text."""
        cur = self.conn.execute(
            "UPDATE warns SET notes = ? WHERE guild_id = ? AND id = ?", (notes, guild_id, warn_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def count_warns(self, guild_id: int, user_id: int) -> int:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM warns WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return cur.fetchone()[0]

    def count_recent_warns(self, guild_id: int, user_id: int, since: int) -> int:
        """Warns within the automod escalation window - used to check a
        freshly-added manual warning (/warn or the WebUI) against the same
        escalation tiers AutoMod violations climb. See
        AutoMod.apply_warn_escalation for why this is a separate counter
        from count_recent_automod_violations."""
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM warns WHERE guild_id = ? AND user_id = ? AND created_at >= ?",
            (guild_id, user_id, since),
        )
        return cur.fetchone()[0]

    def list_warned_users(self, guild_id: int) -> list[tuple[int, int, int]]:
        """Every user in this guild with at least one warning, with their
        warning count and the timestamp of their most recent one - used for
        the dashboard's "who has warnings" overview so an admin doesn't have
        to already know who to look up."""
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

    # ---- server rules ----
    # A rule's displayed number is its 1-based rank in id order, resolved at
    # read time rather than stored - see the CREATE TABLE comment.

    def add_rule(self, guild_id: int, rule_text: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO server_rules (guild_id, rule_text, created_at) VALUES (?, ?, ?)",
            (guild_id, rule_text, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_rules(self, guild_id: int) -> list[tuple[int, str]]:
        """Returns (id, rule_text) tuples in display order (rule #1 first)."""
        cur = self.conn.execute(
            "SELECT id, rule_text FROM server_rules WHERE guild_id = ? ORDER BY id", (guild_id,)
        )
        return cur.fetchall()

    def get_rule_by_number(self, guild_id: int, number: int) -> Optional[tuple[int, str]]:
        """number is the 1-based position shown in /rules or the dashboard,
        not the row id - resolves it the same way list_rules orders things."""
        if number < 1:
            return None
        cur = self.conn.execute(
            "SELECT id, rule_text FROM server_rules WHERE guild_id = ? ORDER BY id LIMIT 1 OFFSET ?",
            (guild_id, number - 1),
        )
        return cur.fetchone()

    def get_rule_by_id(self, guild_id: int, rule_id: int) -> Optional[str]:
        """Resolves a warns.rule_id reference back to its text - used to
        display "Rule #N: <text>" against a warning, where N is looked up
        fresh each time (see list_rules) so it stays correct even if
        earlier rules have since been deleted."""
        row = self.conn.execute(
            "SELECT rule_text FROM server_rules WHERE guild_id = ? AND id = ?", (guild_id, rule_id)
        ).fetchone()
        return row[0] if row else None

    def rule_number_for_id(self, guild_id: int, rule_id: int) -> Optional[int]:
        rules = self.list_rules(guild_id)
        for idx, (rid, _text) in enumerate(rules, start=1):
            if rid == rule_id:
                return idx
        return None

    def delete_rule(self, guild_id: int, rule_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM server_rules WHERE guild_id = ? AND id = ?", (guild_id, rule_id))
        self.conn.commit()
        return cur.rowcount > 0

    # ---- member reports ----

    def create_report(self, guild_id: int, reporter_id: int, target_user_id: int, reason: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO reports (guild_id, reporter_id, target_user_id, reason, status, created_at)
               VALUES (?, ?, ?, ?, 'open', ?)""",
            (guild_id, reporter_id, target_user_id, reason, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_report(self, guild_id: int, report_id: int) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT id, reporter_id, target_user_id, reason, status, created_at,
                      resolved_by, resolved_at, resolution_note, linked_warn_id
               FROM reports WHERE guild_id = ? AND id = ?""",
            (guild_id, report_id),
        ).fetchone()
        if row is None:
            return None
        keys = ("id", "reporter_id", "target_user_id", "reason", "status", "created_at",
                "resolved_by", "resolved_at", "resolution_note", "linked_warn_id")
        return dict(zip(keys, row))

    def list_reports(self, guild_id: int, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        if status:
            cur = self.conn.execute(
                """SELECT id, reporter_id, target_user_id, reason, status, created_at,
                          resolved_by, resolved_at, resolution_note, linked_warn_id
                   FROM reports WHERE guild_id = ? AND status = ? ORDER BY created_at DESC LIMIT ?""",
                (guild_id, status, limit),
            )
        else:
            cur = self.conn.execute(
                """SELECT id, reporter_id, target_user_id, reason, status, created_at,
                          resolved_by, resolved_at, resolution_note, linked_warn_id
                   FROM reports WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?""",
                (guild_id, limit),
            )
        keys = ("id", "reporter_id", "target_user_id", "reason", "status", "created_at",
                "resolved_by", "resolved_at", "resolution_note", "linked_warn_id")
        return [dict(zip(keys, row)) for row in cur.fetchall()]

    def set_report_status(self, guild_id: int, report_id: int, status: str) -> bool:
        """For 'reviewing' - a lightweight status change with no resolution
        details. Use resolve_report/dismiss_report for a final status, which
        also record who closed it and why."""
        cur = self.conn.execute(
            "UPDATE reports SET status = ? WHERE guild_id = ? AND id = ?", (status, guild_id, report_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def close_report(
        self, guild_id: int, report_id: int, status: str, resolved_by: int,
        resolution_note: Optional[str] = None, linked_warn_id: Optional[int] = None,
    ) -> bool:
        """status is 'resolved' or 'dismissed'. linked_warn_id is set when a
        report was acted on by issuing a warning through the same form
        (see webui/main.py's resolve-as-warning route)."""
        cur = self.conn.execute(
            """UPDATE reports SET status = ?, resolved_by = ?, resolved_at = ?,
                      resolution_note = ?, linked_warn_id = ?
               WHERE guild_id = ? AND id = ?""",
            (status, resolved_by, int(time.time()), resolution_note, linked_warn_id, guild_id, report_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_report_config(self, guild_id: int) -> dict:
        row = self.conn.execute(
            "SELECT channel_id FROM report_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return {"channel_id": row[0] if row else None}

    def set_report_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        self.conn.execute(
            """INSERT INTO report_config (guild_id, channel_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id""",
            (guild_id, channel_id),
        )
        self.conn.commit()

    # ---- staff activity stats ----

    def staff_action_counts(self, guild_id: int, since: Optional[int] = None) -> dict[int, dict[str, int]]:
        """Per-moderator counts of warns issued, tickets closed, and reports
        resolved, merged from the tables that already record each (see the
        module note atop dashboardmoderation.py about member_history being
        this codebase's shared case log - this reads straight off the real
        tables rather than introducing a parallel 'staff activity' log that
        could drift out of sync with them).

        Manual kicks/mutes/timeouts done from Discord's own UI aren't
        counted here - unlike bot-issued actions, those aren't guaranteed to
        have a real Discord user id to group by (see logging_cog's
        audit-log attribution, which is best-effort and can come back
        empty), so they're left out rather than risk silently undercounting
        against an incomplete moderator id.
        """
        counts: dict[int, dict[str, int]] = {}

        def bump(moderator_id: int, field: str, n: int = 1) -> None:
            entry = counts.setdefault(moderator_id, {"warns": 0, "tickets_closed": 0, "reports_resolved": 0})
            entry[field] += n

        warn_query = "SELECT moderator_id, COUNT(*) FROM warns WHERE guild_id = ?"
        warn_args: tuple = (guild_id,)
        if since is not None:
            warn_query += " AND created_at >= ?"
            warn_args += (since,)
        warn_query += " GROUP BY moderator_id"
        for moderator_id, n in self.conn.execute(warn_query, warn_args):
            bump(moderator_id, "warns", n)

        ticket_query = "SELECT closed_by, COUNT(*) FROM tickets WHERE guild_id = ? AND status = 'closed' AND closed_by IS NOT NULL"
        ticket_args: tuple = (guild_id,)
        if since is not None:
            ticket_query += " AND closed_at >= ?"
            ticket_args += (since,)
        ticket_query += " GROUP BY closed_by"
        for closed_by, n in self.conn.execute(ticket_query, ticket_args):
            bump(closed_by, "tickets_closed", n)

        report_query = "SELECT resolved_by, COUNT(*) FROM reports WHERE guild_id = ? AND status = 'resolved' AND resolved_by IS NOT NULL"
        report_args: tuple = (guild_id,)
        if since is not None:
            report_query += " AND resolved_at >= ?"
            report_args += (since,)
        report_query += " GROUP BY resolved_by"
        for resolved_by, n in self.conn.execute(report_query, report_args):
            bump(resolved_by, "reports_resolved", n)

        return counts

    # ---- automod review queue ----

    def queue_automod_review(
        self, guild_id: int, channel_id: int, user_id: int, rule_label: str, content_snapshot: str,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO automod_review_queue
                   (guild_id, channel_id, user_id, rule_label, content_snapshot, created_at, status)
               VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
            (guild_id, channel_id, user_id, rule_label, content_snapshot, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_automod_queue(self, guild_id: int, status: str = "pending", limit: int = 100) -> list[dict]:
        cur = self.conn.execute(
            """SELECT id, channel_id, user_id, rule_label, content_snapshot, created_at,
                      status, resolved_by, resolved_at
               FROM automod_review_queue WHERE guild_id = ? AND status = ?
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, status, limit),
        )
        keys = ("id", "channel_id", "user_id", "rule_label", "content_snapshot", "created_at",
                "status", "resolved_by", "resolved_at")
        return [dict(zip(keys, row)) for row in cur.fetchall()]

    def get_automod_review(self, guild_id: int, review_id: int) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT id, channel_id, user_id, rule_label, content_snapshot, created_at,
                      status, resolved_by, resolved_at
               FROM automod_review_queue WHERE guild_id = ? AND id = ?""",
            (guild_id, review_id),
        ).fetchone()
        if row is None:
            return None
        keys = ("id", "channel_id", "user_id", "rule_label", "content_snapshot", "created_at",
                "status", "resolved_by", "resolved_at")
        return dict(zip(keys, row))

    def resolve_automod_review(self, guild_id: int, review_id: int, resolved_by: int, status: str) -> bool:
        """status is 'confirmed' or 'dismissed'. Only updates a still-pending
        row, so the WebUI and a Discord-side reviewer can't both resolve the
        same entry out from under each other."""
        cur = self.conn.execute(
            """UPDATE automod_review_queue SET status = ?, resolved_by = ?, resolved_at = ?
               WHERE guild_id = ? AND id = ? AND status = 'pending'""",
            (status, resolved_by, int(time.time()), guild_id, review_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def queue_automod_decision(self, guild_id: int, review_id: int, decision: str, resolved_by: int) -> int:
        """Dashboard has no live Discord connection, so a Confirm/Dismiss
        click there is queued here and applied by the bot's poller - same
        queue-and-poll bridge every other WebUI->Discord action in this
        codebase uses."""
        cur = self.conn.execute(
            """INSERT INTO automod_queue_decisions (guild_id, review_id, decision, resolved_by, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (guild_id, review_id, decision, resolved_by, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def claim_automod_decisions(self, limit: int = 10) -> list[tuple[int, int, int, str, int]]:
        rows = self.conn.execute(
            "SELECT id, guild_id, review_id, decision, resolved_by FROM automod_queue_decisions ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        if rows:
            self.conn.executemany("DELETE FROM automod_queue_decisions WHERE id = ?", [(row[0],) for row in rows])
            self.conn.commit()
        return rows

    def set_automod_queue_fuzzy(self, guild_id: int, enabled: bool) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET queue_fuzzy_matches = ? WHERE guild_id = ?", (int(enabled), guild_id)
        )
        self.conn.commit()

    # ---- server-wide search ----

    def search_all(self, guild_id: int, query: str, limit_per_category: int = 8) -> dict[str, list]:
        """Plain-text search across everything this bot keeps records of,
        for the dashboard's search box. Each category returns its own raw
        rows (not pre-formatted) so the caller can resolve user/channel
        labels the same way the rest of the WebUI already does.
        """
        like = f"%{query}%"
        results: dict[str, list] = {}

        results["warns"] = self.conn.execute(
            """SELECT id, user_id, moderator_id, reason, created_at FROM warns
               WHERE guild_id = ? AND reason LIKE ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, like, limit_per_category),
        ).fetchall()

        results["reports"] = self.conn.execute(
            """SELECT id, reporter_id, target_user_id, reason, status, created_at FROM reports
               WHERE guild_id = ? AND reason LIKE ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, like, limit_per_category),
        ).fetchall()

        results["rules"] = self.conn.execute(
            """SELECT id, rule_text FROM server_rules
               WHERE guild_id = ? AND rule_text LIKE ? ORDER BY id LIMIT ?""",
            (guild_id, like, limit_per_category),
        ).fetchall()

        results["tickets"] = self.conn.execute(
            """SELECT id, channel_id, opener_id, subject, status, created_at FROM tickets
               WHERE guild_id = ? AND (subject LIKE ? OR CAST(opener_id AS TEXT) LIKE ?)
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, like, like, limit_per_category),
        ).fetchall()

        results["polls"] = self.conn.execute(
            """SELECT id, channel_id, question, closed, created_at FROM polls
               WHERE guild_id = ? AND question LIKE ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, like, limit_per_category),
        ).fetchall()

        results["automod_queue"] = self.conn.execute(
            """SELECT id, channel_id, user_id, rule_label, content_snapshot, created_at, status
               FROM automod_review_queue
               WHERE guild_id = ? AND content_snapshot LIKE ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, like, limit_per_category),
        ).fetchall()

        return results

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

    def list_member_history_by_type(self, guild_id: int, event_type: str, limit: int = 20):
        """Recent member_history rows of one event type across the whole
        guild (unlike list_member_history, which is scoped to one member) -
        used by the verification dashboard page to show recent verifications
        without a dedicated table."""
        cur = self.conn.execute(
            """SELECT user_id, actor_id, reason, created_at FROM member_history
               WHERE guild_id = ? AND event_type = ? ORDER BY created_at DESC, id DESC LIMIT ?""",
            (guild_id, event_type, limit),
        )
        return cur.fetchall()

    _ANALYTICS_EVENT_COLUMNS = {
        "message.received": "messages",
        "command.completed": "commands",
        "member.join": "member_joins",
        "member.leave": "member_leaves",
        "message.edited": "message_edits",
        "message.deleted": "message_deletes",
        "reaction.added": "reactions",
        "voice.join": "voice_joins",
        "voice.leave": "voice_leaves",
    }

    def get_analytics_settings(self, guild_id: int) -> dict:
        row = self.conn.execute(
            """SELECT messages, commands, member_joins, member_leaves,
                      message_edits, message_deletes, reactions, voice_joins, voice_leaves
               FROM analytics_settings WHERE guild_id = ?""",
            (guild_id,),
        ).fetchone()
        if row is None:
            return {name: True for name in self._ANALYTICS_EVENT_COLUMNS.values()}
        names = ("messages", "commands", "member_joins", "member_leaves",
                 "message_edits", "message_deletes", "reactions", "voice_joins", "voice_leaves")
        return dict(zip(names, map(bool, row)))

    def set_analytics_setting(self, guild_id: int, setting: str, enabled: bool) -> None:
        if setting not in set(self._ANALYTICS_EVENT_COLUMNS.values()):
            raise ValueError("Unknown analytics setting")
        self.conn.execute(
            "INSERT INTO analytics_settings (guild_id, {0}) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET {0}=excluded.{0}".format(setting),
            (guild_id, 1 if enabled else 0),
        )
        self.conn.commit()

    def analytics_enabled_for_event(self, guild_id: int, event_type: str) -> bool:
        setting = self._ANALYTICS_EVENT_COLUMNS.get(event_type)
        if not setting or guild_id is None:
            return True
        return self.get_analytics_settings(guild_id).get(setting, True)

    def record_bot_event(
        self, event_type: str, guild_id: Optional[int] = None, actor_id: Optional[int] = None,
        target_id: Optional[int] = None, details=None, created_at: Optional[int] = None,
        *, source: str = "system", status: str = "success", duration_ms: Optional[int] = None,
        correlation_id: Optional[str] = None,
    ) -> int:
        """Write one durable structured audit event.

        ``details`` may be a string or a JSON-serializable mapping. The event
        receives a stable event_id automatically, while source/status/duration
        and correlation_id make related actions searchable without scraping
        human-readable log files.
        """
        import uuid

        if guild_id is not None and not self.analytics_enabled_for_event(guild_id, event_type):
            return 0
        created_at = int(time.time()) if created_at is None else int(created_at)
        if isinstance(details, (dict, list, tuple)):
            details = json.dumps(details, ensure_ascii=False, separators=(",", ":"))
        if details is not None:
            details = str(details)
        event_id = "evt_" + uuid.uuid4().hex
        cur = self.conn.execute(
            """INSERT INTO bot_events
               (event_id, guild_id, event_type, source, status, actor_id, target_id,
                details, duration_ms, correlation_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, guild_id, event_type, source, status, actor_id, target_id,
             details, duration_ms, correlation_id, created_at),
        )
        self.conn.commit()

        # Keep analytics bounded on long-running self-hosted instances.
        # Retain 90 days and cap the table at 100,000 newest events.
        cutoff = int(time.time()) - (90 * 86400)
        self.conn.execute("DELETE FROM bot_events WHERE created_at < ?", (cutoff,))
        self.conn.execute(
            """DELETE FROM bot_events
               WHERE id NOT IN (
                   SELECT id FROM bot_events ORDER BY created_at DESC, id DESC LIMIT 100000
               )"""
        )
        self.conn.commit()

        logger.info(
            "audit event: id=%s type=%s source=%s status=%s guild=%s actor=%s target=%s duration_ms=%s details=%s",
            event_id, event_type, source, status, guild_id, actor_id, target_id, duration_ms, details,
        )
        return cur.lastrowid

    def recent_bot_events(self, limit: int = 500):
        limit = max(1, min(int(limit), 5000))
        cur = self.conn.execute(
            """SELECT id, event_id, guild_id, event_type, source, status, actor_id,
                      target_id, details, duration_ms, correlation_id, created_at
               FROM bot_events ORDER BY created_at DESC, id DESC LIMIT ?""",
            (limit,),
        )
        return cur.fetchall()

    def activity_counts(self, guild_id: int, since: int, event_types: tuple[str, ...]) -> dict[str, int]:
        """Count durable activity events for a guild without storing message content."""
        if not event_types:
            return {}
        placeholders = ",".join("?" for _ in event_types)
        cur = self.conn.execute(
            f"""SELECT event_type, COUNT(*) FROM bot_events
                WHERE guild_id = ? AND created_at >= ? AND event_type IN ({placeholders})
                GROUP BY event_type""",
            (guild_id, int(since), *event_types),
        )
        counts = {event_type: 0 for event_type in event_types}
        counts.update({row[0]: row[1] for row in cur.fetchall()})
        return counts

    def get_daily_activity_counts(self, guild_id: int, since: int, event_type: str) -> list[tuple[str, int]]:
        """Per-day event counts for one event_type since `since`, as
        (YYYY-MM-DD, count) pairs. Bucketed by the local day of
        created_at, matching how the Analytics drill-down page renders
        its per-day bars."""
        cur = self.conn.execute(
            """SELECT date(created_at, 'unixepoch', 'localtime') AS day, COUNT(*)
               FROM bot_events
               WHERE guild_id = ? AND created_at >= ? AND event_type = ?
               GROUP BY day""",
            (guild_id, int(since), event_type),
        )
        return cur.fetchall()

    def list_recent_events(
        self, guild_id: int, since: int, event_type: str, limit: int = 25
    ) -> list[tuple[int, Optional[int], Optional[int], Optional[str]]]:
        """Most recent raw events of one type for the "recent activity" list
        under an Analytics drill-down chart, as
        (created_at, actor_id, target_id, details) tuples."""
        cur = self.conn.execute(
            """SELECT created_at, actor_id, target_id, details
               FROM bot_events
               WHERE guild_id = ? AND created_at >= ? AND event_type = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (guild_id, int(since), event_type, limit),
        )
        return cur.fetchall()

    def get_activity_stats(self, guild_id: int, since: int) -> dict[str, int]:
        counts = self.activity_counts(
            guild_id,
            since,
            (
                "message.received", "command.completed", "member.join", "member.leave",
                "message.edited", "message.deleted", "reaction.added",
                "voice.join", "voice.leave",
            ),
        )
        return {
            "messages": counts["message.received"],
            "commands": counts["command.completed"],
            "joins": counts["member.join"],
            "leaves": counts["member.leave"],
            "message_edits": counts["message.edited"],
            "message_deletes": counts["message.deleted"],
            "reactions": counts["reaction.added"],
            "voice_joins": counts["voice.join"],
            "voice_leaves": counts["voice.leave"],
        }

    def get_server_counts(self, guild_id: int) -> dict[str, int]:
        members = self.conn.execute(
            "SELECT COUNT(*) FROM bot_members WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0]
        online = self.conn.execute(
            """SELECT COUNT(*) FROM bot_members
               WHERE guild_id = ? AND status IN ('online', 'idle', 'dnd')""",
            (guild_id,),
        ).fetchone()[0]
        channels = self.conn.execute(
            "SELECT COUNT(*) FROM bot_channels WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0]
        roles = self.conn.execute(
            "SELECT COUNT(*) FROM bot_roles WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0] + 1  # include @everyone, which is intentionally not cached
        return {"members": members, "online": online, "channels": channels, "roles": roles}

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

    # ---- dashboard purge queue ----

    def queue_purge_request(self, guild_id: int, channel_id: int, user_id: int | None, amount: int, reason: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO dashboard_purge_requests
               (guild_id, channel_id, user_id, amount, reason, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'queued', ?)""",
            (guild_id, channel_id, user_id, amount, reason, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_purge_requests(self, limit: int = 10) -> list[tuple[int, int, int, int | None, int, str]]:
        rows = self.conn.execute(
            "SELECT id, guild_id, channel_id, user_id, amount, reason FROM dashboard_purge_requests WHERE status = 'queued' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ','.join('?' * len(ids))
            self.conn.execute(f"UPDATE dashboard_purge_requests SET status='processing', error=NULL WHERE id IN ({placeholders})", ids)
            self.conn.commit()
        return rows

    def complete_purge_request(self, request_id: int, error: str | None = None,
                                deleted_count: int | None = None, breakdown: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_purge_requests SET status=?, completed_at=?, error=?, deleted_count=?, breakdown=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, deleted_count,
             json.dumps(breakdown) if breakdown else None, request_id),
        )
        self.conn.commit()

    def recent_purge_requests(self, guild_id: int, limit: int = 20):
        return self.conn.execute(
            """SELECT id, channel_id, user_id, amount, reason, status, created_at, completed_at, error,
                      deleted_count, breakdown
               FROM dashboard_purge_requests WHERE guild_id=? ORDER BY id DESC LIMIT ?""",
            (guild_id, limit),
        ).fetchall()

    # ---- dashboard mod actions (kick/ban/mute/timeout etc. queued from the
    # WebUI, same claim/complete pattern as the purge queue above - the
    # dashboard is a separate process with no Discord connection, so it
    # queues the request here and the bot process claims and executes it) ----

    def queue_mod_action(self, guild_id: int, user_id: int, action: str, duration_seconds: int | None, reason: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO dashboard_mod_actions
               (guild_id, user_id, action, duration_seconds, reason, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'queued', ?)""",
            (guild_id, user_id, action, duration_seconds, reason, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_mod_actions(self, limit: int = 10) -> list[tuple[int, int, int, str, int | None, str]]:
        rows = self.conn.execute(
            """SELECT id, guild_id, user_id, action, duration_seconds, reason
               FROM dashboard_mod_actions WHERE status = 'queued' ORDER BY id LIMIT ?""",
            (limit,),
        ).fetchall()
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ','.join('?' * len(ids))
            self.conn.execute(f"UPDATE dashboard_mod_actions SET status='processing', error=NULL WHERE id IN ({placeholders})", ids)
            self.conn.commit()
        return rows

    def complete_mod_action(self, request_id: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_mod_actions SET status=?, completed_at=?, error=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, request_id),
        )
        self.conn.commit()

    def recent_mod_actions(self, guild_id: int, limit: int = 20):
        return self.conn.execute(
            """SELECT id, user_id, action, duration_seconds, reason, status, created_at, completed_at, error
               FROM dashboard_mod_actions WHERE guild_id=? ORDER BY id DESC LIMIT ?""",
            (guild_id, limit),
        ).fetchall()

    # ---- emergency control center (server-wide lockdown, invite revoke,
    # mass timeout) - dashboard-only, same queue/claim/complete pattern as
    # purge/mod-actions above. Deliberately separate from dashboard_mod_actions:
    # these act on the whole guild rather than one member, and lockdown in
    # particular needs its own persistent state table (emergency_lockdown_state)
    # to remember each channel's original @everyone send-permission so Unlock
    # can restore it exactly rather than guessing "no overwrite". ----

    def queue_emergency_request(self, guild_id: int, action: str, params: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO dashboard_emergency_requests (guild_id, action, params, status, created_at)
               VALUES (?, ?, ?, 'queued', ?)""",
            (guild_id, action, json.dumps(params), int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_emergency_requests(self, limit: int = 5) -> list[tuple[int, int, str, str]]:
        rows = self.conn.execute(
            """SELECT id, guild_id, action, params FROM dashboard_emergency_requests
               WHERE status = 'queued' ORDER BY id LIMIT ?""",
            (limit,),
        ).fetchall()
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ','.join('?' * len(ids))
            self.conn.execute(f"UPDATE dashboard_emergency_requests SET status='processing', error=NULL WHERE id IN ({placeholders})", ids)
            self.conn.commit()
        return rows

    def complete_emergency_request(self, request_id: int, error: str | None = None, result: dict | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_emergency_requests SET status=?, completed_at=?, error=?, result=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, json.dumps(result) if result else None, request_id),
        )
        self.conn.commit()

    def recent_emergency_requests(self, guild_id: int, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, action, params, status, created_at, completed_at, error, result
               FROM dashboard_emergency_requests WHERE guild_id=? ORDER BY id DESC LIMIT ?""",
            (guild_id, limit),
        ).fetchall()
        keys = ("id", "action", "params", "status", "created_at", "completed_at", "error", "result")
        out = []
        for row in rows:
            item = dict(zip(keys, row))
            item["params"] = json.loads(item["params"]) if item["params"] else {}
            item["result"] = json.loads(item["result"]) if item["result"] else None
            out.append(item)
        return out

    def get_lockdown_state(self, guild_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT channel_overwrites, started_at, started_by FROM emergency_lockdown_state WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        if row is None:
            return None
        return {"channel_overwrites": json.loads(row[0]), "started_at": row[1], "started_by": row[2]}

    def set_lockdown_state(self, guild_id: int, channel_overwrites: dict, started_by: int) -> None:
        """channel_overwrites maps channel_id -> the @everyone role's prior
        send_messages value (true/false/null) for that channel, before
        lockdown touched it - Unlock restores exactly this rather than
        assuming every channel should end up with no overwrite at all."""
        self.conn.execute(
            """INSERT INTO emergency_lockdown_state (guild_id, channel_overwrites, started_at, started_by)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_overwrites=excluded.channel_overwrites,
                   started_at=excluded.started_at, started_by=excluded.started_by""",
            (guild_id, json.dumps({str(k): v for k, v in channel_overwrites.items()}), int(time.time()), started_by),
        )
        self.conn.commit()

    def clear_lockdown_state(self, guild_id: int) -> None:
        self.conn.execute("DELETE FROM emergency_lockdown_state WHERE guild_id=?", (guild_id,))
        self.conn.commit()

    # ---- config snapshots (scoped to ReedMuhn's own settings tables, not
    # live Discord role/channel permissions - see the note in emergency.py
    # about why actual Discord permissions are out of scope here) ----

    def create_config_snapshot(self, guild_id: int, name: str, data: dict, created_by: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO config_snapshots (guild_id, name, data, created_at, created_by) VALUES (?, ?, ?, ?, ?)",
            (guild_id, name, json.dumps(data), int(time.time()), created_by),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_config_snapshots(self, guild_id: int, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, name, created_at, created_by FROM config_snapshots WHERE guild_id=? ORDER BY id DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()
        return [{"id": r[0], "name": r[1], "created_at": r[2], "created_by": r[3]} for r in rows]

    def get_config_snapshot(self, guild_id: int, snapshot_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, name, data, created_at, created_by FROM config_snapshots WHERE guild_id=? AND id=?",
            (guild_id, snapshot_id),
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "name": row[1], "data": json.loads(row[2]), "created_at": row[3], "created_by": row[4]}

    def delete_config_snapshot(self, guild_id: int, snapshot_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM config_snapshots WHERE guild_id=? AND id=?", (guild_id, snapshot_id))
        self.conn.commit()
        return cur.rowcount > 0

    def capture_config_snapshot_data(self, guild_id: int) -> dict:
        """Gathers every ReedMuhn-owned setting for a guild into one plain
        dict, ready to json.dumps into config_snapshots.data. Deliberately
        does NOT touch real Discord role/channel permissions - only this
        bot's own settings tables (see the module note in cogs/emergency.py
        for why that's out of scope)."""
        automod = self.get_automod_config(guild_id)
        automod["escalation_tiers"] = self.list_automod_escalation_tiers(guild_id)
        return {
            "guild_config": self.get_guild_config(guild_id),
            "automod": automod,
            "verification": self.get_verification_config(guild_id),
            "tickets": self.get_ticket_config(guild_id),
            "reports": self.get_report_config(guild_id),
            "tempnick_mode": self.get_tempnick_mode(guild_id),
            "tempnick_roles": self.list_tempnick_roles(guild_id),
            "bot_manager_roles": self.list_bot_manager_roles(guild_id),
            "voice_hubs": self.list_voice_hubs(guild_id),
            "log_channels": self.get_all_log_channels(guild_id),
            "log_ignored_channels": self.list_ignored_log_channels(guild_id),
        }

    def restore_config_snapshot_data(self, guild_id: int, data: dict) -> None:
        """Inverse of capture_config_snapshot_data - writes every section
        back. Runs as one transaction (commits only at the end) so a
        mid-restore failure can't leave settings half-swapped between the
        old and snapshotted state."""
        gc = data.get("guild_config", {})
        if gc.get("welcome_channel_id") and gc.get("welcome_message"):
            self.set_welcome(guild_id, gc["welcome_channel_id"], gc["welcome_message"])
        self.set_welcome_card_enabled(guild_id, bool(gc.get("welcome_card_enabled")))
        if gc.get("autorole_id"):
            self.set_autorole(guild_id, gc["autorole_id"])
        else:
            self.clear_autorole(guild_id)
        if gc.get("birthday_channel_id"):
            self.set_birthday_channel(guild_id, gc["birthday_channel_id"])
        if gc.get("muted_role_id"):
            self.set_muted_role(guild_id, gc["muted_role_id"])
        self.set_sticky_roles_enabled(guild_id, bool(gc.get("sticky_roles_enabled")))

        automod = data.get("automod")
        if automod:
            self._restore_automod_config(guild_id, automod)

        v = data.get("verification")
        if v:
            self.set_verification_config(guild_id, bool(v.get("enabled")), v.get("channel_id"), v.get("role_id"), v.get("message") or "")

        t = data.get("tickets")
        if t and t.get("category_id") and t.get("support_role_id"):
            self.set_ticket_config(guild_id, t["category_id"], t["support_role_id"])
        if t and t.get("panel_channel_id"):
            self.set_ticket_panel_config(
                guild_id, t["panel_channel_id"], t.get("panel_title") or "Support",
                t.get("panel_description") or "Click the button below to open a private ticket with the support team.",
            )
            self.queue_ticket_panel_post(guild_id)

        r = data.get("reports")
        if r:
            self.set_report_channel(guild_id, r.get("channel_id"))

        if data.get("tempnick_mode"):
            self.set_tempnick_mode(guild_id, data["tempnick_mode"])
        self._restore_id_list(guild_id, "tempnick_roles", "role_id", data.get("tempnick_roles", []))
        self._restore_id_list(guild_id, "bot_manager_roles", "role_id", data.get("bot_manager_roles", []))
        self._restore_id_list(guild_id, "log_ignored_channels", "channel_id", data.get("log_ignored_channels", []))

        self.conn.execute("DELETE FROM voice_hubs WHERE guild_id=?", (guild_id,))
        for hub_channel_id, user_limit in data.get("voice_hubs", []):
            self.conn.execute(
                "INSERT INTO voice_hubs (guild_id, hub_channel_id, user_limit) VALUES (?, ?, ?)",
                (guild_id, hub_channel_id, user_limit),
            )

        self.conn.execute("DELETE FROM log_channels WHERE guild_id=?", (guild_id,))
        for category, channel_id in data.get("log_channels", {}).items():
            self.conn.execute(
                "INSERT INTO log_channels (guild_id, category, channel_id) VALUES (?, ?, ?)",
                (guild_id, category, channel_id),
            )
        self.conn.commit()

    def _restore_id_list(self, guild_id: int, table: str, id_column: str, ids: list) -> None:
        """Shared by every snapshot section that's just a set of
        (guild_id, some_id) rows - tempnick_roles, bot_manager_roles,
        log_ignored_channels. `table` and `id_column` are only ever called
        with fixed string literals below, never user input."""
        self.conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
        for item_id in ids:
            self.conn.execute(f"INSERT OR IGNORE INTO {table} (guild_id, {id_column}) VALUES (?, ?)", (guild_id, item_id))

    def _restore_automod_config(self, guild_id: int, data: dict) -> None:
        self.get_automod_config(guild_id)  # ensures a row exists to UPDATE
        banned_words = ",".join(data.get("banned_words", []))
        self.conn.execute(
            """UPDATE automod_config SET enabled=?, block_invites=?, banned_words=?, caps_percent=?, caps_min_len=?,
                   mention_threshold=?, spam_count=?, spam_window_seconds=?, duplicate_count=?, duplicate_window_seconds=?,
                   violation_mute_threshold=?, violation_window_seconds=?, violation_mute_duration_seconds=?,
                   fuzzy_words=?, queue_fuzzy_matches=?, block_gifs=? WHERE guild_id=?""",
            (
                int(bool(data.get("enabled"))), int(bool(data.get("block_invites"))), banned_words,
                data.get("caps_percent", 70), data.get("caps_min_len", 10), data.get("mention_threshold", 5),
                data.get("spam_count", 5), data.get("spam_window_seconds", 5), data.get("duplicate_count", 3),
                data.get("duplicate_window_seconds", 30), data.get("violation_mute_threshold", 3),
                data.get("violation_window_seconds", 3600), data.get("violation_mute_duration_seconds", 600),
                int(bool(data.get("fuzzy_words"))), int(bool(data.get("queue_fuzzy_matches"))),
                int(bool(data.get("block_gifs"))), guild_id,
            ),
        )
        self.conn.execute("DELETE FROM automod_escalation_tiers WHERE guild_id=?", (guild_id,))
        for tier in data.get("escalation_tiers", []):
            self.conn.execute(
                "INSERT INTO automod_escalation_tiers (guild_id, threshold, action, duration_seconds) VALUES (?, ?, ?, ?)",
                (guild_id, tier["threshold"], tier["action"], tier.get("duration_seconds")),
            )

    # ---- muted-role settings sync (WebUI edits the policy in the db, but
    # actually applying it means walking every channel's permission
    # overwrites via the Discord API - same queue/claim/complete pattern as
    # purge/mod-actions above, so the bot process does the real work) ----

    def queue_mute_role_sync(self, guild_id: int, reason: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO dashboard_mute_role_sync_requests (guild_id, reason, status, created_at)
               VALUES (?, ?, 'queued', ?)""",
            (guild_id, reason, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_mute_role_sync_requests(self, limit: int = 10) -> list[tuple[int, int, str]]:
        rows = self.conn.execute(
            "SELECT id, guild_id, reason FROM dashboard_mute_role_sync_requests WHERE status = 'queued' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ','.join('?' * len(ids))
            self.conn.execute(
                f"UPDATE dashboard_mute_role_sync_requests SET status='processing', error=NULL WHERE id IN ({placeholders})",
                ids,
            )
            self.conn.commit()
        return rows

    def complete_mute_role_sync(self, request_id: int, error: str | None = None,
                                 changed: int | None = None, failed: int | None = None) -> None:
        self.conn.execute(
            """UPDATE dashboard_mute_role_sync_requests
               SET status=?, completed_at=?, error=?, changed=?, failed=? WHERE id=?""",
            ('failed' if error else 'completed', int(time.time()), error, changed, failed, request_id),
        )
        self.conn.commit()

    def latest_mute_role_sync(self, guild_id: int) -> Optional[tuple[str, int, Optional[str], Optional[int], Optional[int]]]:
        """Most recent sync attempt for this guild - lets the WebUI show
        whether the currently-saved settings have actually been pushed to
        Discord yet, not just stored."""
        row = self.conn.execute(
            """SELECT status, created_at, error, changed, failed
               FROM dashboard_mute_role_sync_requests WHERE guild_id=? ORDER BY id DESC LIMIT 1""",
            (guild_id,),
        ).fetchone()
        return row

    # ---- verification ----

    def get_verification_config(self, guild_id: int) -> dict:
        row = self.conn.execute(
            "SELECT enabled, channel_id, role_id, message, message_id FROM verification_config WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        if row is None:
            return {
                "enabled": False, "channel_id": None, "role_id": None,
                "message": "Click the button below to verify and unlock the rest of the server.",
                "message_id": None,
            }
        return {
            "enabled": bool(row[0]), "channel_id": row[1], "role_id": row[2],
            "message": row[3], "message_id": row[4],
        }

    def set_verification_config(self, guild_id: int, enabled: bool, channel_id: int | None,
                                 role_id: int | None, message: str) -> None:
        self.conn.execute(
            """INSERT INTO verification_config (guild_id, enabled, channel_id, role_id, message)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled, channel_id=excluded.channel_id,
                   role_id=excluded.role_id, message=excluded.message""",
            (guild_id, int(enabled), channel_id, role_id, message),
        )
        self.conn.commit()

    def set_verification_message_id(self, guild_id: int, message_id: int | None) -> None:
        self.conn.execute("UPDATE verification_config SET message_id=? WHERE guild_id=?", (message_id, guild_id))
        self.conn.commit()

    def queue_verify_post(self, guild_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO dashboard_verify_post_requests (guild_id, status, created_at) VALUES (?, 'queued', ?)",
            (guild_id, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_verify_post_requests(self, limit: int = 10) -> list[tuple[int, int]]:
        rows = self.conn.execute(
            "SELECT id, guild_id FROM dashboard_verify_post_requests WHERE status='queued' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ','.join('?' * len(ids))
            self.conn.execute(f"UPDATE dashboard_verify_post_requests SET status='processing', error=NULL WHERE id IN ({placeholders})", ids)
            self.conn.commit()
        return rows

    def complete_verify_post(self, request_id: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_verify_post_requests SET status=?, completed_at=?, error=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, request_id),
        )
        self.conn.commit()

    # ---- tickets ----

    def get_ticket_config(self, guild_id: int) -> dict:
        row = self.conn.execute(
            "SELECT category_id, support_role_id, panel_channel_id, panel_message_id, panel_title, panel_description "
            "FROM ticket_config WHERE guild_id=?", (guild_id,)
        ).fetchone()
        if row is None:
            return {
                "category_id": None, "support_role_id": None, "panel_channel_id": None, "panel_message_id": None,
                "panel_title": "Support", "panel_description": "Click the button below to open a private ticket with the support team.",
            }
        return {
            "category_id": row[0], "support_role_id": row[1], "panel_channel_id": row[2], "panel_message_id": row[3],
            "panel_title": row[4], "panel_description": row[5],
        }

    def set_ticket_config(self, guild_id: int, category_id: int | None, support_role_id: int | None) -> None:
        self.conn.execute(
            """INSERT INTO ticket_config (guild_id, category_id, support_role_id) VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET category_id=excluded.category_id, support_role_id=excluded.support_role_id""",
            (guild_id, category_id, support_role_id),
        )
        self.conn.commit()

    def set_ticket_panel_config(self, guild_id: int, channel_id: int, title: str, description: str) -> None:
        """Separate from set_ticket_config (category/support role) since the
        panel can be configured and (re)posted independently - you don't
        need a support role picked yet just to try out panel wording."""
        self.conn.execute(
            """INSERT INTO ticket_config (guild_id, panel_channel_id, panel_title, panel_description) VALUES (?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET panel_channel_id=excluded.panel_channel_id,
                   panel_title=excluded.panel_title, panel_description=excluded.panel_description""",
            (guild_id, channel_id, title, description),
        )
        self.conn.commit()

    def set_ticket_panel_message_id(self, guild_id: int, message_id: int | None) -> None:
        self.conn.execute("UPDATE ticket_config SET panel_message_id=? WHERE guild_id=?", (message_id, guild_id))
        self.conn.commit()

    def queue_ticket_panel_post(self, guild_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO dashboard_ticket_panel_requests (guild_id, status, created_at) VALUES (?, 'queued', ?)",
            (guild_id, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_ticket_panel_requests(self, limit: int = 10) -> list[tuple[int, int]]:
        rows = self.conn.execute(
            "SELECT id, guild_id FROM dashboard_ticket_panel_requests WHERE status='queued' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ','.join('?' * len(ids))
            self.conn.execute(f"UPDATE dashboard_ticket_panel_requests SET status='processing', error=NULL WHERE id IN ({placeholders})", ids)
            self.conn.commit()
        return rows

    def complete_ticket_panel_post(self, request_id: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_ticket_panel_requests SET status=?, completed_at=?, error=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, request_id),
        )
        self.conn.commit()

    def create_ticket(self, guild_id: int, channel_id: int, opener_id: int, subject: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO tickets (guild_id, channel_id, opener_id, subject, status, created_at)
               VALUES (?, ?, ?, ?, 'open', ?)""",
            (guild_id, channel_id, opener_id, subject, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_ticket_by_channel(self, channel_id: int) -> Optional[tuple]:
        return self.conn.execute(
            "SELECT id, guild_id, channel_id, opener_id, subject, status FROM tickets WHERE channel_id=? AND status='open'",
            (channel_id,),
        ).fetchone()

    def get_ticket(self, ticket_id: int) -> Optional[tuple]:
        return self.conn.execute(
            "SELECT id, guild_id, channel_id, opener_id, subject, status FROM tickets WHERE id=?",
            (ticket_id,),
        ).fetchone()

    def close_ticket(self, ticket_id: int, closed_by: int, reason: str) -> bool:
        """Atomic - only actually closes (and returns True) if the ticket
        is currently open. Guards the same double-close race the button
        and /closeticket could otherwise hit if clicked/run twice in quick
        succession (matches the WHERE status='pending' guard already used
        for the automod queue's confirm/dismiss)."""
        cur = self.conn.execute(
            "UPDATE tickets SET status='closed', closed_at=?, closed_by=?, close_reason=? WHERE id=? AND status='open'",
            (int(time.time()), closed_by, reason, ticket_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_open_ticket_by_opener(self, guild_id: int, opener_id: int) -> Optional[tuple]:
        return self.conn.execute(
            "SELECT id, channel_id FROM tickets WHERE guild_id=? AND opener_id=? AND status='open' LIMIT 1",
            (guild_id, opener_id),
        ).fetchone()
        self.conn.commit()

    def list_tickets(self, guild_id: int, limit: int = 50) -> list[tuple]:
        return self.conn.execute(
            """SELECT id, channel_id, opener_id, subject, status, created_at, closed_at, closed_by, close_reason
               FROM tickets WHERE guild_id=? ORDER BY status='open' DESC, id DESC LIMIT ?""",
            (guild_id, limit),
        ).fetchall()

    def queue_ticket_close(self, guild_id: int, ticket_id: int, reason: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO dashboard_ticket_close_requests (guild_id, ticket_id, reason, status, created_at)
               VALUES (?, ?, ?, 'queued', ?)""",
            (guild_id, ticket_id, reason, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_ticket_close_requests(self, limit: int = 10) -> list[tuple[int, int, int, str]]:
        rows = self.conn.execute(
            """SELECT id, guild_id, ticket_id, reason FROM dashboard_ticket_close_requests
               WHERE status='queued' ORDER BY id LIMIT ?""",
            (limit,),
        ).fetchall()
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ','.join('?' * len(ids))
            self.conn.execute(f"UPDATE dashboard_ticket_close_requests SET status='processing', error=NULL WHERE id IN ({placeholders})", ids)
            self.conn.commit()
        return rows

    def complete_ticket_close(self, request_id: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_ticket_close_requests SET status=?, completed_at=?, error=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, request_id),
        )
        self.conn.commit()

    # ---- polls ----

    def create_poll(self, guild_id: int, channel_id: int, question: str, options: list[str],
                     created_by: int, ends_at: int | None) -> int:
        cur = self.conn.execute(
            """INSERT INTO polls (guild_id, channel_id, question, options, created_by, created_at, ends_at, closed)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (guild_id, channel_id, question, json.dumps(options), created_by, int(time.time()), ends_at),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_poll_message_id(self, poll_id: int, message_id: int) -> None:
        self.conn.execute("UPDATE polls SET message_id=? WHERE id=?", (message_id, poll_id))
        self.conn.commit()

    def get_poll(self, poll_id: int) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT id, guild_id, channel_id, message_id, question, options, created_by, created_at, ends_at, closed
               FROM polls WHERE id=?""",
            (poll_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0], "guild_id": row[1], "channel_id": row[2], "message_id": row[3],
            "question": row[4], "options": json.loads(row[5]), "created_by": row[6],
            "created_at": row[7], "ends_at": row[8], "closed": bool(row[9]),
        }

    def list_open_polls(self) -> list[dict]:
        """Every non-closed poll across all guilds - used on bot startup to
        re-register each poll's persistent vote-button view against its
        message, since a dynamic (per-poll) view isn't restored automatically
        just by being defined in code the way a single static view would be."""
        rows = self.conn.execute(
            "SELECT id, guild_id, channel_id, message_id, question, options FROM polls WHERE closed=0 AND message_id IS NOT NULL"
        ).fetchall()
        return [
            {"id": r[0], "guild_id": r[1], "channel_id": r[2], "message_id": r[3],
             "question": r[4], "options": json.loads(r[5])}
            for r in rows
        ]

    def list_polls(self, guild_id: int, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """SELECT id, channel_id, message_id, question, options, created_by, created_at, ends_at, closed
               FROM polls WHERE guild_id=? ORDER BY id DESC LIMIT ?""",
            (guild_id, limit),
        ).fetchall()
        return [
            {"id": r[0], "channel_id": r[1], "message_id": r[2], "question": r[3], "options": json.loads(r[4]),
             "created_by": r[5], "created_at": r[6], "ends_at": r[7], "closed": bool(r[8])}
            for r in rows
        ]

    def cast_poll_vote(self, poll_id: int, user_id: int, option_index: int) -> None:
        self.conn.execute(
            "INSERT INTO poll_votes (poll_id, user_id, option_index) VALUES (?, ?, ?) "
            "ON CONFLICT(poll_id, user_id) DO UPDATE SET option_index=excluded.option_index",
            (poll_id, user_id, option_index),
        )
        self.conn.commit()

    def poll_results(self, poll_id: int, num_options: int) -> list[int]:
        counts = [0] * num_options
        for (option_index,) in self.conn.execute(
            "SELECT option_index FROM poll_votes WHERE poll_id=?", (poll_id,)
        ).fetchall():
            if 0 <= option_index < num_options:
                counts[option_index] += 1
        return counts

    def close_poll(self, poll_id: int) -> None:
        self.conn.execute("UPDATE polls SET closed=1 WHERE id=?", (poll_id,))
        self.conn.commit()

    def queue_poll_close(self, guild_id: int, poll_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO dashboard_poll_close_requests (guild_id, poll_id, status, created_at) VALUES (?, ?, 'queued', ?)",
            (guild_id, poll_id, int(time.time())),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_poll_close_requests(self, limit: int = 10) -> list[tuple[int, int, int]]:
        rows = self.conn.execute(
            "SELECT id, guild_id, poll_id FROM dashboard_poll_close_requests WHERE status='queued' ORDER BY id LIMIT ?",
            (limit,),
        ).fetchall()
        if rows:
            ids = [row[0] for row in rows]
            placeholders = ','.join('?' * len(ids))
            self.conn.execute(f"UPDATE dashboard_poll_close_requests SET status='processing', error=NULL WHERE id IN ({placeholders})", ids)
            self.conn.commit()
        return rows

    def complete_poll_close(self, request_id: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_poll_close_requests SET status=?, completed_at=?, error=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, request_id),
        )
        self.conn.commit()

    # ---- webui login / dashboard action log ----

    def record_webui_login(self, ip: str, success: bool) -> None:
        self.conn.execute(
            "INSERT INTO webui_login_events (created_at, ip, success) VALUES (?, ?, ?)",
            (int(time.time()), ip, int(success)),
        )
        self.conn.commit()
        # Keep this table from growing unbounded on a long-lived box - a
        # rolling window of the most recent attempts is all the dashboard
        # log needs; older rows have no ongoing value.
        self.conn.execute(
            "DELETE FROM webui_login_events WHERE id NOT IN (SELECT id FROM webui_login_events ORDER BY id DESC LIMIT 500)"
        )
        self.conn.commit()

    def list_webui_login_events(self, limit: int = 50) -> list[tuple[int, str, bool]]:
        return self.conn.execute(
            "SELECT created_at, ip, success FROM webui_login_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def record_webui_action(self, guild_id: int | None, ip: str, method: str, path: str, status_code: int) -> None:
        self.conn.execute(
            "INSERT INTO webui_action_log (created_at, guild_id, ip, method, path, status_code) VALUES (?, ?, ?, ?, ?, ?)",
            (int(time.time()), guild_id, ip, method, path, status_code),
        )
        self.conn.commit()
        self.conn.execute(
            "DELETE FROM webui_action_log WHERE id NOT IN (SELECT id FROM webui_action_log ORDER BY id DESC LIMIT 1000)"
        )
        self.conn.commit()

    def list_webui_action_log(self, limit: int = 100) -> list[tuple[int, int | None, str, str, str, int]]:
        return self.conn.execute(
            "SELECT created_at, guild_id, ip, method, path, status_code FROM webui_action_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    # ---- temp voice channels ----

    def add_voice_hub(self, guild_id: int, hub_channel_id: int, user_limit: int = 0) -> bool:
        """Returns False if this channel was already a hub (no-op) - in that
        case user_limit is NOT applied; use set_voice_hub_limit to change an
        existing hub's default."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO voice_hubs (guild_id, hub_channel_id, user_limit) VALUES (?, ?, ?)",
            (guild_id, hub_channel_id, user_limit),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def is_voice_hub(self, guild_id: int, channel_id: int) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM voice_hubs WHERE guild_id = ? AND hub_channel_id = ?", (guild_id, channel_id)
        ).fetchone()
        return row is not None

    def get_voice_hub_limit(self, guild_id: int, hub_channel_id: int) -> int:
        """Default user_limit (0 = unlimited) applied to channels created
        from this hub. Returns 0 if the hub isn't found."""
        row = self.conn.execute(
            "SELECT user_limit FROM voice_hubs WHERE guild_id = ? AND hub_channel_id = ?",
            (guild_id, hub_channel_id),
        ).fetchone()
        return row[0] if row else 0

    def set_voice_hub_limit(self, guild_id: int, hub_channel_id: int, user_limit: int) -> bool:
        """Update an existing hub's default user_limit. Returns False if the
        hub doesn't exist."""
        cur = self.conn.execute(
            "UPDATE voice_hubs SET user_limit = ? WHERE guild_id = ? AND hub_channel_id = ?",
            (user_limit, guild_id, hub_channel_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def pop_temp_voice_delete_requests(self, guild_id: int):
        rows = self.conn.execute(
            "SELECT id, channel_id FROM temp_voice_delete_requests WHERE guild_id = ? ORDER BY id LIMIT 50",
            (guild_id,),
        ).fetchall()
        if rows:
            self.conn.executemany("DELETE FROM temp_voice_delete_requests WHERE id = ?", [(row[0],) for row in rows])
            self.conn.commit()
        return [(row[0], row[1]) for row in rows]

    def pop_temp_voice_limit_requests(self, guild_id: int):
        """Returns (channel_id, user_limit) tuples for queued dashboard
        channel-limit changes and clears them from the queue."""
        rows = self.conn.execute(
            """SELECT id, channel_id, user_limit FROM temp_voice_limit_requests
               WHERE guild_id = ? ORDER BY id LIMIT 50""",
            (guild_id,),
        ).fetchall()
        if rows:
            self.conn.executemany("DELETE FROM temp_voice_limit_requests WHERE id = ?", [(row[0],) for row in rows])
            self.conn.commit()
        return [(row[1], row[2]) for row in rows]

    def list_voice_hubs(self, guild_id: int) -> list[tuple[int, int]]:
        """Returns (hub_channel_id, user_limit) tuples."""
        cur = self.conn.execute(
            "SELECT hub_channel_id, user_limit FROM voice_hubs WHERE guild_id = ?", (guild_id,)
        )
        return cur.fetchall()

    def remove_voice_hub(self, guild_id: int, hub_channel_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM voice_hubs WHERE guild_id = ? AND hub_channel_id = ?", (guild_id, hub_channel_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def add_temp_voice_channel(self, guild_id: int, channel_id: int, owner_id: int, user_limit: int = 0) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO temp_voice_channels (channel_id, guild_id, owner_id, user_limit) VALUES (?, ?, ?, ?)",
            (channel_id, guild_id, owner_id, user_limit),
        )
        self.conn.commit()

    def is_temp_voice_channel(self, channel_id: int, guild_id: int | None = None) -> bool:
        if guild_id is None:
            row = self.conn.execute(
                "SELECT 1 FROM temp_voice_channels WHERE channel_id = ?", (channel_id,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT 1 FROM temp_voice_channels WHERE channel_id = ? AND guild_id = ?",
                (channel_id, guild_id),
            ).fetchone()
        return row is not None

    def request_temp_voice_delete(self, guild_id: int, channel_id: int) -> bool:
        """Queue a dashboard deletion only for a temp channel in this guild."""
        if not self.is_temp_voice_channel(channel_id, guild_id):
            return False
        self.conn.execute(
            "INSERT INTO temp_voice_delete_requests (guild_id, channel_id, created_at) VALUES (?, ?, ?)",
            (guild_id, channel_id, int(time.time())),
        )
        self.conn.commit()
        return True

    def request_temp_voice_limit(self, guild_id: int, channel_id: int, user_limit: int) -> bool:
        """Queue a dashboard user-limit change only for a temp channel in
        this guild. user_limit must already be validated to 0-99 by the
        caller (0 = unlimited, matching Discord)."""
        if not self.is_temp_voice_channel(channel_id, guild_id):
            return False
        self.conn.execute(
            "INSERT INTO temp_voice_limit_requests (guild_id, channel_id, user_limit, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, channel_id, user_limit, int(time.time())),
        )
        self.conn.commit()
        return True

    def remove_temp_voice_channel(self, channel_id: int) -> None:
        self.conn.execute("DELETE FROM temp_voice_channels WHERE channel_id = ?", (channel_id,))
        self.conn.commit()

    def update_temp_voice_channel_limit(self, channel_id: int, user_limit: int) -> None:
        """Mirrors a live channel's user_limit into our tracked state, once
        Discord has actually confirmed the change - so the dashboard (which
        has no live Discord connection) can display the current value."""
        self.conn.execute(
            "UPDATE temp_voice_channels SET user_limit = ? WHERE channel_id = ?", (user_limit, channel_id)
        )
        self.conn.commit()

    def list_temp_voice_channels(self, guild_id: int) -> list[tuple[int, int, int]]:
        """Returns (channel_id, owner_id, user_limit) tuples."""
        cur = self.conn.execute(
            "SELECT channel_id, owner_id, user_limit FROM temp_voice_channels WHERE guild_id = ?", (guild_id,)
        )
        return cur.fetchall()

    # ---- automod ----

    _AUTOMOD_COLUMNS = (
        "enabled", "block_invites", "banned_words", "caps_percent", "caps_min_len",
        "mention_threshold", "spam_count", "spam_window_seconds",
        "duplicate_count", "duplicate_window_seconds",
        "violation_mute_threshold", "violation_window_seconds", "violation_mute_duration_seconds",
        "fuzzy_words", "queue_fuzzy_matches", "block_gifs",
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
        result["fuzzy_words"] = bool(result["fuzzy_words"])
        result["queue_fuzzy_matches"] = bool(result["queue_fuzzy_matches"])
        result["block_gifs"] = bool(result["block_gifs"])
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

    def set_automod_block_gifs(self, guild_id: int, block: bool) -> None:
        """GIFs are caught by file extension (.gif attachments) and by
        known GIF-hosting domains in links (Tenor/Giphy/Discord's own GIF
        picker CDN) - see automod_checks.is_gif_content. Doesn't touch
        static images, so it doesn't need the caps/spam filters' nuance."""
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET block_gifs = ? WHERE guild_id = ?", (int(block), guild_id)
        )
        self.conn.commit()

    def set_automod_fuzzy_words(self, guild_id: int, enabled: bool) -> None:
        """Toggles the 'alike words' fuzzy filter - see
        automod_checks.contains_banned_word's fuzzy= argument for what this
        actually changes about matching."""
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET fuzzy_words = ? WHERE guild_id = ?", (int(enabled), guild_id)
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

    def set_automod_violation_window(self, guild_id: int, window_seconds: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET violation_window_seconds = ? WHERE guild_id = ?",
            (window_seconds, guild_id),
        )
        self.conn.commit()

    # ---- automod escalation tiers ----
    # A tier fires once a member racks up `threshold` warnings within the
    # configured violation window. Several tiers can be configured per
    # guild (e.g. 3 -> mute, 5 -> kick, 8 -> ban) - see cogs/automod.py for
    # how they're matched and applied.

    def list_automod_escalation_tiers(self, guild_id: int) -> list[dict]:
        cur = self.conn.execute(
            """SELECT id, threshold, action, duration_seconds
               FROM automod_escalation_tiers WHERE guild_id = ? ORDER BY threshold ASC""",
            (guild_id,),
        )
        return [
            {"id": row[0], "threshold": row[1], "action": row[2], "duration_seconds": row[3]}
            for row in cur.fetchall()
        ]

    def set_automod_escalation_tier(self, guild_id: int, threshold: int, action: str, duration_seconds: Optional[int]) -> None:
        """Adds a tier, or replaces the existing tier at the same threshold
        (there can only be one action per threshold per guild)."""
        self.conn.execute(
            """INSERT OR REPLACE INTO automod_escalation_tiers (guild_id, threshold, action, duration_seconds)
               VALUES (?, ?, ?, ?)""",
            (guild_id, threshold, action, duration_seconds),
        )
        self.conn.commit()

    def remove_automod_escalation_tier(self, guild_id: int, tier_id: int) -> None:
        self.conn.execute(
            "DELETE FROM automod_escalation_tiers WHERE guild_id = ? AND id = ?", (guild_id, tier_id)
        )
        self.conn.commit()

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

    # ---- dashboard "talk as the bot" outbound message queue ----
    # The web dashboard and the Discord bot run as separate processes, so
    # the dashboard can't call channel.send() directly - it queues a row
    # here instead, and the bot's polling loop (see cogs/dashboardtalk.py)
    # picks it up, sends it, and marks it sent.

    def queue_outbound_message(self, guild_id: int, channel_id: int, content: str) -> int:
        content = content.strip()
        if not content:
            raise ValueError("message content cannot be empty")
        if len(content) > 2000:
            raise ValueError("Discord messages cannot exceed 2000 characters")
        now = int(time.time())
        cur = self.conn.execute(
            """INSERT INTO outbound_messages
               (guild_id, channel_id, content, sent, created_at, status, attempts, available_at)
               VALUES (?, ?, ?, 0, ?, 'queued', 0, ?)""",
            (guild_id, channel_id, content, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def claim_outbound_messages(
        self, limit: int = 10, lease_seconds: int = 120
    ) -> list[tuple[int, int, int, str, int]]:
        """Atomically claim ready messages so a worker crash/restart cannot
        cause two live polling iterations to send the same row concurrently.
        A stale sending lease is returned to the queue after lease_seconds.
        """
        limit = max(1, min(int(limit), 50))
        now = int(time.time())
        stale_before = now - max(30, int(lease_seconds))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """UPDATE outbound_messages
                   SET status = 'queued', locked_at = NULL, available_at = ?
                   WHERE status = 'sending' AND locked_at IS NOT NULL AND locked_at < ?""",
                (now, stale_before),
            )
            rows = self.conn.execute(
                """SELECT id, guild_id, channel_id, content, attempts
                   FROM outbound_messages
                   WHERE status = 'queued' AND available_at <= ?
                   ORDER BY id LIMIT ?""",
                (now, limit),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE outbound_messages SET status='sending', locked_at=?, attempts=attempts+1 WHERE id IN ({placeholders})",
                    [now, *ids],
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise

    def mark_outbound_message_sent(self, message_id: int, discord_message_id: int | None = None) -> None:
        now = int(time.time())
        self.conn.execute(
            """UPDATE outbound_messages
               SET status='sent', sent=1, locked_at=NULL, sent_at=?, failed_at=NULL,
                   last_error=NULL, discord_message_id=?
               WHERE id=?""",
            (now, discord_message_id, message_id),
        )
        self.conn.commit()

    def mark_outbound_message_failed(
        self, message_id: int, error: str, *, retry: bool = True, max_attempts: int = 5
    ) -> str:
        """Record a failure. Transient failures are retried with exponential
        backoff; permanent failures become terminal and remain visible in the
        dashboard instead of silently disappearing. Returns the new status."""
        now = int(time.time())
        row = self.conn.execute(
            "SELECT attempts FROM outbound_messages WHERE id = ?", (message_id,)
        ).fetchone()
        if row is None:
            return "missing"
        attempts = int(row[0])
        if retry and attempts < max_attempts:
            delay = min(300, 2 ** max(0, attempts - 1) * 5)
            self.conn.execute(
                """UPDATE outbound_messages
                   SET status='queued', sent=0, locked_at=NULL, available_at=?, last_error=?
                   WHERE id=?""",
                (now + delay, error[:1000], message_id),
            )
            status = "queued"
        else:
            self.conn.execute(
                """UPDATE outbound_messages
                   SET status='failed', sent=0, locked_at=NULL, failed_at=?, last_error=?
                   WHERE id=?""",
                (now, error[:1000], message_id),
            )
            status = "failed"
        self.conn.commit()
        return status

    def retry_outbound_message(self, message_id: int) -> bool:
        now = int(time.time())
        cur = self.conn.execute(
            """UPDATE outbound_messages
               SET status='queued', sent=0, available_at=?, locked_at=NULL, failed_at=NULL, last_error=NULL
               WHERE id=? AND status='failed'""",
            (now, message_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_outbound_message(self, message_id: int):
        return self.conn.execute(
            """SELECT id, guild_id, channel_id, content, status, attempts, created_at,
                      available_at, sent_at, failed_at, last_error, discord_message_id, deleted_at
               FROM outbound_messages WHERE id = ?""",
            (message_id,),
        ).fetchone()

    def recent_outbound_messages(self, guild_id: int, limit: int = 20, include_deleted: bool = True):
        limit = max(1, min(int(limit), 100))
        if include_deleted:
            return self.conn.execute(
                """SELECT id, channel_id, content, status, attempts, created_at, sent_at,
                          failed_at, last_error, discord_message_id, deleted_at
                   FROM outbound_messages
                   WHERE guild_id = ?
                   ORDER BY id DESC LIMIT ?""",
                (guild_id, limit),
            ).fetchall()
        return self.conn.execute(
            """SELECT id, channel_id, content, status, attempts, created_at, sent_at,
                      failed_at, last_error, discord_message_id, deleted_at
               FROM outbound_messages
               WHERE guild_id = ? AND status != 'deleted'
               ORDER BY id DESC LIMIT ?""",
            (guild_id, limit),
        ).fetchall()

    # ---- deleting a message the bot already sent via Talk ----
    # Same outbox pattern as sending: the dashboard only marks intent, and
    # the bot (a separate process) is the one that actually talks to
    # Discord. A message can only be queued for deletion once it's really
    # on Discord (status='sent' with a known discord_message_id) - there's
    # nothing to delete yet for a still-queued or already-failed message.

    def request_message_delete(self, guild_id: int, message_id: int) -> bool:
        cur = self.conn.execute(
            """UPDATE outbound_messages
               SET status='delete_requested'
               WHERE id=? AND guild_id=? AND status='sent' AND discord_message_id IS NOT NULL""",
            (message_id, guild_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def claim_message_delete_requests(self, limit: int = 10) -> list[tuple[int, int, int, int]]:
        """Claim queued delete requests as (id, guild_id, channel_id,
        discord_message_id) tuples. Low-volume, dashboard-triggered action,
        so a simple claim-then-process is enough - no stale-lease recovery
        like the send queue needs. A crash mid-delete just leaves the row
        as 'deleting'; the Discord message either got deleted or didn't,
        and the dashboard's retry button covers either case."""
        limit = max(1, min(int(limit), 50))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                """SELECT id, guild_id, channel_id, discord_message_id
                   FROM outbound_messages
                   WHERE status = 'delete_requested'
                   ORDER BY id LIMIT ?""",
                (limit,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE outbound_messages SET status='deleting' WHERE id IN ({placeholders})",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise

    def mark_message_deleted(self, message_id: int) -> None:
        self.conn.execute(
            "UPDATE outbound_messages SET status='deleted', last_error=NULL, deleted_at=? WHERE id=?",
            (int(time.time()), message_id),
        )
        self.conn.commit()

    def mark_message_delete_failed(self, message_id: int, error: str) -> None:
        # Back to 'sent' rather than a terminal state, so the Delete button
        # reappears on the dashboard and the person can just try again.
        self.conn.execute(
            "UPDATE outbound_messages SET status='sent', last_error=? WHERE id=?",
            (error[:1000], message_id),
        )
        self.conn.commit()

    # Backwards-compatible helper retained for extensions that may still call it.
    def list_unsent_outbound_messages(self) -> list[tuple[int, int, int, str]]:
        return [(r[0], r[1], r[2], r[3]) for r in self.claim_outbound_messages(limit=50)]

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
            status = getattr(getattr(member, "status", None), "value", "offline")
            self.conn.execute(
                "INSERT INTO bot_members (guild_id, user_id, name, display_name, status) VALUES (?, ?, ?, ?, ?)",
                (guild_id, int(member.id), member.name, member.display_name, status),
            )
        self.conn.commit()

    def upsert_bot_member(
        self, guild_id: int, user_id: int, name: str, display_name: str, status: str = "offline"
    ) -> None:
        self.conn.execute(
            "INSERT INTO bot_members (guild_id, user_id, name, display_name, status) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET name=excluded.name, display_name=excluded.display_name, status=excluded.status",
            (guild_id, user_id, name, display_name, status),
        )
        self.conn.commit()

    def update_member_status(self, guild_id: int, user_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE bot_members SET status = ? WHERE guild_id = ? AND user_id = ?",
            (status, guild_id, user_id),
        )
        self.conn.commit()

    def remove_bot_member(self, guild_id: int, user_id: int) -> None:
        self.conn.execute("DELETE FROM bot_members WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        self.conn.commit()

    def list_bot_members(self, guild_id: int) -> list[tuple[int, str, str]]:
        return self.conn.execute("SELECT user_id, display_name, name FROM bot_members WHERE guild_id = ? ORDER BY LOWER(display_name), LOWER(name)", (guild_id,)).fetchall()

    def list_bot_members_with_status(self, guild_id: int) -> list[tuple[int, str, str, str]]:
        """Like list_bot_members but also returns each member's cached
        presence status, for the Analytics members/online drill-downs."""
        return self.conn.execute(
            "SELECT user_id, display_name, name, status FROM bot_members "
            "WHERE guild_id = ? ORDER BY LOWER(display_name), LOWER(name)",
            (guild_id,),
        ).fetchall()

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
