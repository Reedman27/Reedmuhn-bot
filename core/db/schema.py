"""Connection setup and the table/index definitions (_create_tables)."""
import logging
import os
import sqlite3

logger = logging.getLogger("db")


class SchemaMixin:
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
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self._create_tables()

    def _create_tables(self) -> None:
        # Extras: XP/levels, economy, giveaways, live counters and notifications.
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_xp (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, xp INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,user_id))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_economy (
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, balance INTEGER NOT NULL DEFAULT 0,
            last_daily INTEGER, PRIMARY KEY(guild_id,user_id))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL UNIQUE, prize TEXT NOT NULL, winners INTEGER NOT NULL,
            end_at INTEGER NOT NULL, ended INTEGER NOT NULL DEFAULT 0)""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_extras_giveaways_due ON extras_giveaways(ended,end_at)")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_counters (
            guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, kind TEXT NOT NULL,
            PRIMARY KEY(guild_id,kind))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_twitch (
            guild_id INTEGER NOT NULL, username TEXT NOT NULL, channel_id INTEGER NOT NULL,
            last_live INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(guild_id,username))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_feeds (
            guild_id INTEGER NOT NULL, url TEXT NOT NULL, channel_id INTEGER NOT NULL,
            last_id TEXT NOT NULL DEFAULT '', PRIMARY KEY(guild_id,url))""")
        # Level-up announcements: per-guild on/off, which channel to post in
        # (NULL = wherever the member's message that triggered the level-up
        # was sent), and a {user}/{level}/{server}-style message template.
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_level_config (
            guild_id INTEGER PRIMARY KEY, announce_enabled INTEGER NOT NULL DEFAULT 1,
            announce_channel_id INTEGER, announce_message TEXT NOT NULL DEFAULT '🎉 {user} reached **level {level}**!')""")
        # Level-up role rewards. Stacking (Arcane-style): reaching a level
        # grants every reward role at or below it that the member doesn't
        # already have, rather than swapping a single "current level" role.
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_level_roles (
            guild_id INTEGER NOT NULL, level INTEGER NOT NULL, role_id INTEGER NOT NULL,
            PRIMARY KEY(guild_id,level))""")
        # Channels excluded from earning XP (e.g. a bot-commands or media-dump
        # channel that would otherwise dominate the leaderboard).
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_noxp_channels (
            guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, PRIMARY KEY(guild_id,channel_id))""")
        # XP multiplier roles (booster/VIP-style roles that earn XP faster).
        self.conn.execute("""CREATE TABLE IF NOT EXISTS extras_xp_boost_roles (
            guild_id INTEGER NOT NULL, role_id INTEGER NOT NULL, multiplier REAL NOT NULL DEFAULT 1.5,
            PRIMARY KEY(guild_id,role_id))""")
        # extras_economy predates the daily_streak column - add it for
        # anyone upgrading from an older bot.db without wiping their data.
        economy_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(extras_economy)")}
        if "daily_streak" not in economy_cols:
            self.conn.execute("ALTER TABLE extras_economy ADD COLUMN daily_streak INTEGER NOT NULL DEFAULT 0")
        # The XP curve changed from a flat 100*level^2 to a smoother,
        # community-standard progression (see _extras_xp_for_level) - do a
        # one-time recompute of every stored level so leaderboards and rank
        # cards are correct immediately instead of waiting for each member's
        # next message. Gated behind user_version so this only runs once.
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            for gid, uid, xp, old_level in self.conn.execute("SELECT guild_id, user_id, xp, level FROM extras_xp").fetchall():
                new_level = self._extras_level_from_xp(xp)
                if new_level != old_level:
                    self.conn.execute(
                        "UPDATE extras_xp SET level=? WHERE guild_id=? AND user_id=?", (new_level, gid, uid)
                    )
            self.conn.execute("PRAGMA user_version = 1")

        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS scheduled_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_name TEXT NOT NULL,
                guild_id INTEGER NOT NULL,
                run_at INTEGER NOT NULL,
                data TEXT NOT NULL
            )"""
        )
        # Retry bookkeeping for scheduled events. A transient Discord/API
        # failure must not silently drop a scheduled moderation action, so a
        # failed event is rescheduled with a growing backoff and only given
        # up on once its attempts run out (see scheduler.run_loop).
        sched_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(scheduled_events)")}
        if "attempts" not in sched_cols:
            self.conn.execute("ALTER TABLE scheduled_events ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "last_error" not in sched_cols:
            self.conn.execute("ALTER TABLE scheduled_events ADD COLUMN last_error TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_events_due ON scheduled_events(run_at)")
        # Role rewards (level roles, invite milestones) Discord refused at the
        # moment they were earned. Each reward is earned exactly once, so a
        # failed grant has to be retried from here - waiting for another
        # level-up or milestone crossing may mean waiting forever.
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_role_rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                UNIQUE(guild_id, user_id, role_id)
            )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_role_rewards_due ON pending_role_rewards(next_attempt_at)")
        # Reaction-role adds/removes Discord refused. Separate from
        # pending_role_rewards because these carry a direction (add vs remove)
        # and are keyed to a reaction that will never fire again.
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_role_ops (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                UNIQUE(guild_id, user_id, role_id)
            )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_role_ops_due ON pending_role_ops(next_attempt_at)")
        # Channels whose Muted-role overwrite couldn't be written (rate limit,
        # momentary permission problem). Without this the policy sweep just
        # reported a failure count and left silent holes in the mute policy.
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS pending_muted_channel_syncs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at INTEGER NOT NULL,
                UNIQUE(guild_id, channel_id)
            )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_muted_syncs_due ON pending_muted_channel_syncs(next_attempt_at)")
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
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_strip_roles INTEGER NOT NULL DEFAULT 1")
            cols.add("muted_strip_roles")
        if "muted_strip_roles_migration_v2" not in cols:
            self.conn.execute("ALTER TABLE guild_config ADD COLUMN muted_strip_roles_migration_v2 INTEGER NOT NULL DEFAULT 0")
            self.conn.execute("UPDATE guild_config SET muted_strip_roles = 1, muted_strip_roles_migration_v2 = 1")
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
        if "role_id" not in temp_voice_cols:
            self.conn.execute("ALTER TABLE temp_voice_channels ADD COLUMN role_id INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS temp_voice_settings (
                guild_id INTEGER PRIMARY KEY,
                owner_perms INTEGER NOT NULL DEFAULT 0,
                create_role INTEGER NOT NULL DEFAULT 0,
                role_hoist INTEGER NOT NULL DEFAULT 1,
                log_channel_id INTEGER NOT NULL DEFAULT 0,
                name_template TEXT NOT NULL DEFAULT '',
                max_channels INTEGER NOT NULL DEFAULT 25
            )"""
        )
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
            """CREATE TABLE IF NOT EXISTS dashboard_log_channel_create_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                created_at INTEGER NOT NULL,
                completed_at INTEGER,
                error TEXT,
                channel_id INTEGER
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
        if "delete_on_close" not in ticket_cfg_cols:
            self.conn.execute("ALTER TABLE ticket_config ADD COLUMN delete_on_close INTEGER NOT NULL DEFAULT 0")
        if "delete_delay_seconds" not in ticket_cfg_cols:
            self.conn.execute("ALTER TABLE ticket_config ADD COLUMN delete_delay_seconds INTEGER NOT NULL DEFAULT 10")
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
            """CREATE TABLE IF NOT EXISTS modmail_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                category_id INTEGER,
                log_channel_id INTEGER,
                anonymous_staff INTEGER NOT NULL DEFAULT 0
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS modmail_threads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL,
                closed_at INTEGER,
                closed_by INTEGER
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_modmail_threads_user ON modmail_threads(user_id, status)"
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_modmail_threads_channel ON modmail_threads(channel_id)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS modmail_blocks (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                blocked_by INTEGER,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )"""
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
            """CREATE TABLE IF NOT EXISTS webui_sessions (
                session_id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_webui_sessions_expiry ON webui_sessions(expires_at)")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS webui_oauth_tokens (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL
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
        # Per-user attribution for the multi-server dashboard. Older databases
        # created these tables without it, so add the columns when missing.
        for _table in ("webui_login_events", "webui_action_log"):
            _cols = {row[1] for row in self.conn.execute(f"PRAGMA table_info({_table})")}
            if "user_id" not in _cols:
                self.conn.execute(f"ALTER TABLE {_table} ADD COLUMN user_id TEXT")
            if "username" not in _cols:
                self.conn.execute(f"ALTER TABLE {_table} ADD COLUMN username TEXT")
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
            """CREATE TABLE IF NOT EXISTS dashboard_access (
                guild_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                level TEXT NOT NULL CHECK (level IN ('full', 'staff')),
                added_by TEXT,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS dashboard_guild_settings (
                guild_id INTEGER PRIMARY KEY,
                managers_allowed INTEGER NOT NULL DEFAULT 1
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
            """CREATE TABLE IF NOT EXISTS automod_escalation_state (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reset_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automod_gif_allowlist (
                guild_id INTEGER NOT NULL,
                identifier TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, identifier)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automod_gif_blocklist (
                guild_id INTEGER NOT NULL,
                identifier TEXT NOT NULL,
                kind TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, identifier)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS votekick_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                required_votes INTEGER NOT NULL DEFAULT 5,
                duration_seconds INTEGER NOT NULL DEFAULT 600
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS votekicks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                initiator_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                result TEXT,
                resolved_at INTEGER
            )"""
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_votekicks_guild_status ON votekicks (guild_id, status, expires_at)"
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS votekick_votes (
                votekick_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                vote TEXT NOT NULL CHECK (vote IN ('yes','no')),
                created_at INTEGER NOT NULL,
                PRIMARY KEY (votekick_id, user_id)
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
        bot_roles_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(bot_roles)")}
        for column, definition in (("permissions", "INTEGER NOT NULL DEFAULT 0"), ("managed", "INTEGER NOT NULL DEFAULT 0")):
            if column not in bot_roles_cols:
                self.conn.execute(f"ALTER TABLE bot_roles ADD COLUMN {column} {definition}")
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
        if "is_bot" not in member_cols:
            self.conn.execute("ALTER TABLE bot_members ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_guilds (
                guild_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )"""
        )
        bot_guilds_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(bot_guilds)")}
        if "everyone_permissions" not in bot_guilds_cols:
            self.conn.execute("ALTER TABLE bot_guilds ADD COLUMN everyone_permissions INTEGER NOT NULL DEFAULT 0")
        # Read-only server facts cached by the bot so the dashboard can show
        # them without a Discord connection of its own.
        for column, definition in (
            ("owner_id", "INTEGER NOT NULL DEFAULT 0"),
            ("created_at", "INTEGER NOT NULL DEFAULT 0"),
            ("boost_level", "INTEGER NOT NULL DEFAULT 0"),
            ("boost_count", "INTEGER NOT NULL DEFAULT 0"),
            ("verification_level", "TEXT NOT NULL DEFAULT ''"),
            ("member_total", "INTEGER NOT NULL DEFAULT 0"),
            ("emoji_count", "INTEGER NOT NULL DEFAULT 0"),
            ("bot_permissions", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in bot_guilds_cols:
                self.conn.execute(f"ALTER TABLE bot_guilds ADD COLUMN {column} {definition}")
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
        member_history_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(member_history)")}
        for column, definition in (("case_number", "INTEGER"), ("voided", "INTEGER NOT NULL DEFAULT 0")):
            if column not in member_history_cols:
                self.conn.execute(f"ALTER TABLE member_history ADD COLUMN {column} {definition}")
        self.conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_member_history_case_number
               ON member_history (guild_id, case_number) WHERE case_number IS NOT NULL"""
        )
        # Hands out sequential per-guild case numbers for the moderation
        # events worth numbering (warn/kick/mute/tempban/timeout/ban) -
        # kept separate from member_history's own id, which is a single
        # counter shared across every guild and every event type
        # (automod_violation, verify, etc.) that has no business being
        # part of a case sequence.
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS case_counters (
                guild_id INTEGER PRIMARY KEY,
                next_case INTEGER NOT NULL DEFAULT 1
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS mod_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_mod_notes_user
               ON mod_notes (guild_id, user_id, created_at DESC)"""
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
                max_saves INTEGER NOT NULL DEFAULT 3,
                high_score_alerts INTEGER NOT NULL DEFAULT 0,
                saves_enabled INTEGER NOT NULL DEFAULT 0,
                server_saves INTEGER NOT NULL DEFAULT 0
            )"""
        )
        # Databases created by older versions are upgraded in place.
        counting_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(counting)")}
        for col, ddl in (
            ("save_milestone", "INTEGER NOT NULL DEFAULT 50"),
            ("max_saves", "INTEGER NOT NULL DEFAULT 3"),
            ("high_score_alerts", "INTEGER NOT NULL DEFAULT 0"),
            ("saves_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("server_saves", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in counting_cols:
                self.conn.execute(f"ALTER TABLE counting ADD COLUMN {col} {ddl}")
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
        # Invite-link controls (see cogs/automod.py). Defaults keep the old
        # behavior for upgrading servers: every invite is blocked and a hit
        # counts as a strike on the escalation ladder.
        if "invite_mode" not in automod_cfg_cols:
            self.conn.execute(
                "ALTER TABLE automod_config ADD COLUMN invite_mode TEXT NOT NULL DEFAULT 'strike'"
            )
        if "invite_allow_own" not in automod_cfg_cols:
            self.conn.execute(
                "ALTER TABLE automod_config ADD COLUMN invite_allow_own INTEGER NOT NULL DEFAULT 0"
            )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automod_invite_allow (
                guild_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (guild_id, kind, value)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS automod_invite_exempt_channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, channel_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS antinuke_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                auto_recovery INTEGER NOT NULL DEFAULT 1,
                default_punishment TEXT NOT NULL DEFAULT 'BAN',
                log_channel_id INTEGER,
                threshold INTEGER NOT NULL DEFAULT 3,
                window_seconds INTEGER NOT NULL DEFAULT 10,
                watched_actions TEXT NOT NULL DEFAULT 'channel_delete,role_delete,ban,kick,webhook_create,bot_add'
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS antinuke_whitelist (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                added_by INTEGER,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS antinuke_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                trigger_action TEXT NOT NULL,
                punishment TEXT NOT NULL,
                hit_count INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS raid_config (
                guild_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                join_threshold INTEGER NOT NULL DEFAULT 10,
                window_seconds INTEGER NOT NULL DEFAULT 60,
                action TEXT NOT NULL DEFAULT 'alert',
                new_account_hours INTEGER NOT NULL DEFAULT 168,
                cooldown_seconds INTEGER NOT NULL DEFAULT 300,
                log_channel_id INTEGER
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS raid_incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                join_count INTEGER NOT NULL,
                window_seconds INTEGER NOT NULL,
                action_taken TEXT NOT NULL,
                kicked_count INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS invite_joins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                inviter_id INTEGER,
                invite_code TEXT,
                created_at INTEGER NOT NULL
            )"""
        )
        self.conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_invite_joins_inviter
               ON invite_joins (guild_id, inviter_id)"""
        )
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS invite_milestones (
                guild_id INTEGER NOT NULL,
                invite_count INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, invite_count)
            )"""
        )
        self.conn.execute("""CREATE TABLE IF NOT EXISTS ai_config (
            guild_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            provider TEXT NOT NULL DEFAULT 'openai_compatible',
            base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
            api_key TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT 'gpt-4o-mini',
            system_prompt TEXT NOT NULL DEFAULT 'You are ReedMuhn, a helpful Discord assistant. Be concise and follow the server context when provided.',
            max_tokens INTEGER NOT NULL DEFAULT 800,
            temperature REAL NOT NULL DEFAULT 0.7
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_config_enabled ON ai_config(enabled)")
        ai_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(ai_config)")}
        if "use_channel_context" not in ai_cols:
            self.conn.execute("ALTER TABLE ai_config ADD COLUMN use_channel_context INTEGER NOT NULL DEFAULT 0")
        if "context_message_limit" not in ai_cols:
            self.conn.execute("ALTER TABLE ai_config ADD COLUMN context_message_limit INTEGER NOT NULL DEFAULT 10")
        if "index_channels" not in ai_cols:
            self.conn.execute("ALTER TABLE ai_config ADD COLUMN index_channels INTEGER NOT NULL DEFAULT 0")
        if "index_message_limit" not in ai_cols:
            self.conn.execute("ALTER TABLE ai_config ADD COLUMN index_message_limit INTEGER NOT NULL DEFAULT 500")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS ai_channel_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            author_name TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(guild_id, message_id)
        )""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_channel_messages_guild_channel ON ai_channel_messages(guild_id, channel_id, created_at DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_channel_messages_guild_time ON ai_channel_messages(guild_id, created_at DESC)")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS command_toggles (guild_id INTEGER NOT NULL, command_name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (guild_id, command_name))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS starboard_config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, threshold INTEGER NOT NULL DEFAULT 5, enabled INTEGER NOT NULL DEFAULT 0)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS starboard_messages (guild_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL, starboard_message_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, star_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (guild_id, source_message_id))""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS suggestion_config (guild_id INTEGER PRIMARY KEY, channel_id INTEGER, enabled INTEGER NOT NULL DEFAULT 0, staff_role_id INTEGER)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS suggestions (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, message_id INTEGER NOT NULL, author_id INTEGER NOT NULL, content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', staff_id INTEGER, staff_reason TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_suggestions_guild ON suggestions(guild_id, id DESC)")
        # One open ticket per person per guild, enforced by the database rather
        # than by an application-level "check then insert", which two
        # simultaneous ticket opens could both pass. Wrapped because an older
        # database may already contain duplicate open tickets from before this
        # rule existed - in that case the rule simply isn't enforced until they
        # are closed, which is better than refusing to start.
        # Retry bookkeeping for WebUI-queued moderation actions (see
        # retry_mod_action). Placed here so it runs after the table exists.
        mod_action_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(dashboard_mod_actions)")}
        if "attempts" not in mod_action_cols:
            self.conn.execute("ALTER TABLE dashboard_mod_actions ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
        if "retry_at" not in mod_action_cols:
            self.conn.execute("ALTER TABLE dashboard_mod_actions ADD COLUMN retry_at INTEGER NOT NULL DEFAULT 0")
        self._ensure_one_open_ticket_index()
        # Same table the bot process's voicelink.SQLiteMusicDB writes to
        # (schema kept identical on purpose - see get_music_settings below).
        self.conn.execute("CREATE TABLE IF NOT EXISTS vocard_settings (guild_id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
        self.conn.commit()

    def _ensure_one_open_ticket_index(self) -> bool:
        """Creates the one-open-ticket-per-member unique index if it can be.

        A database that already holds duplicate open tickets can't get the index
        until those are closed. Rather than requiring a restart afterwards,
        close_ticket() calls this again, so the protection switches on by itself
        as soon as the last duplicate is closed."""
        if getattr(self, "_ticket_index_ok", False):
            return True
        try:
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_one_open_per_opener "
                "ON tickets(guild_id, opener_id) WHERE status='open'"
            )
            self._ticket_index_ok = True
        except sqlite3.IntegrityError:
            self._ticket_index_ok = False
            logger.warning(
                "couldn't create the one-open-ticket-per-member index: existing duplicate open "
                "tickets. It will be created automatically once they are closed."
            )
        return self._ticket_index_ok
