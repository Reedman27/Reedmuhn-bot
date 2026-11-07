"""Dashboard support: web sessions, login/action log, access control, cached Discord objects, 'Talk as the bot' queue, guild tracking, command toggles."""
import logging
import time
from typing import Optional

from .constants import TEMP_VOICE_DEFAULT_OWNER_PERMS, TEMP_VOICE_MAX_CHANNELS_CEILING, TEMP_VOICE_OWNER_MASK

logger = logging.getLogger("db")


class DashboardMixin:

    # ---- webui session revocation ----

    def create_webui_session(self, session_id: str, ttl_seconds: int = 43200) -> None:
        now = int(time.time())
        ttl_seconds = max(1, int(ttl_seconds))
        self.conn.execute("DELETE FROM webui_sessions WHERE expires_at <= ?", (now,))
        # An expired session used to leave its stored Discord OAuth tokens behind
        # forever. Tokens must never outlive the session they belong to.
        self.conn.execute(
            "DELETE FROM webui_oauth_tokens WHERE session_id NOT IN (SELECT session_id FROM webui_sessions)"
        )
        self.conn.execute(
            "INSERT INTO webui_sessions (session_id, created_at, expires_at) VALUES (?, ?, ?)",
            (session_id, now, now + ttl_seconds),
        )
        self.conn.commit()

    def is_webui_session_valid(self, session_id: str) -> bool:
        now = int(time.time())
        row = self.conn.execute(
            "SELECT 1 FROM webui_sessions WHERE session_id = ? AND expires_at > ?",
            (session_id, now),
        ).fetchone()
        return row is not None

    def revoke_webui_session(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM webui_sessions WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM webui_oauth_tokens WHERE session_id = ?", (session_id,))
        self.conn.commit()

    def save_webui_oauth_token(self, session_id: str, user_id: str, username: str,
                               access_token: str, refresh_token: str, expires_at: int) -> None:
        self.conn.execute(
            """INSERT INTO webui_oauth_tokens
               (session_id,user_id,username,access_token,refresh_token,expires_at)
               VALUES (?,?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
               user_id=excluded.user_id, username=excluded.username,
               access_token=excluded.access_token, refresh_token=excluded.refresh_token,
               expires_at=excluded.expires_at""",
            (session_id, user_id, username, access_token, refresh_token, int(expires_at)),
        )
        self.conn.commit()

    def get_webui_oauth_token(self, session_id: str):
        return self.conn.execute(
            "SELECT user_id,username,access_token,refresh_token,expires_at FROM webui_oauth_tokens WHERE session_id=?",
            (session_id,),
        ).fetchone()

    def revoke_webui_oauth_token(self, session_id: str) -> None:
        self.conn.execute("DELETE FROM webui_oauth_tokens WHERE session_id=?", (session_id,))
        self.conn.commit()

    # ---- webui login / dashboard action log ----

    def record_webui_login(self, ip: str, success: bool, user_id: str | None = None,
                           username: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO webui_login_events (created_at, ip, success, user_id, username) VALUES (?, ?, ?, ?, ?)",
            (int(time.time()), ip, int(success), user_id, username),
        )
        # Keep this table from growing unbounded on a long-lived box - a
        # rolling window of the most recent attempts is all the dashboard
        # log needs; older rows have no ongoing value.
        self.conn.execute(
            "DELETE FROM webui_login_events WHERE id NOT IN (SELECT id FROM webui_login_events ORDER BY id DESC LIMIT 500)"
        )
        self.conn.commit()

    def list_webui_login_events(self, limit: int = 50, user_id: str | None = None) -> list[tuple]:
        """Login events, newest first. Pass ``user_id`` to see only that
        Discord user's own sign-ins - the log must never show one user other
        people's IP addresses."""
        if user_id is None:
            return self.conn.execute(
                "SELECT created_at, ip, success FROM webui_login_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return self.conn.execute(
            "SELECT created_at, ip, success FROM webui_login_events WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    def record_webui_action(self, guild_id: int | None, ip: str, method: str, path: str, status_code: int,
                            user_id: str | None = None, username: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO webui_action_log (created_at, guild_id, ip, method, path, status_code, user_id, username)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(time.time()), guild_id, ip, method, path, status_code, user_id, username),
        )
        self.conn.execute(
            "DELETE FROM webui_action_log WHERE id NOT IN (SELECT id FROM webui_action_log ORDER BY id DESC LIMIT 1000)"
        )
        self.conn.commit()

    def list_webui_action_log(self, limit: int = 100, guild_ids=None) -> list[tuple]:
        """Dashboard actions, newest first. Pass ``guild_ids`` (the servers the
        viewer may manage) to restrict the log to those servers only."""
        if guild_ids is None:
            return self.conn.execute(
                "SELECT created_at, guild_id, ip, method, path, status_code, username, user_id FROM webui_action_log"
                " ORDER BY id DESC LIMIT ?", (limit,),
            ).fetchall()
        ids = [int(g) for g in guild_ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        return self.conn.execute(
            "SELECT created_at, guild_id, ip, method, path, status_code, username, user_id FROM webui_action_log"
            f" WHERE guild_id IN ({placeholders}) ORDER BY id DESC LIMIT ?", (*ids, limit),
        ).fetchall()

    # ---- dashboard access (who may open this server's dashboard) ----
    # Default (no rows, managers_allowed=1) is the original behaviour: the
    # server owner and anyone with Administrator / Manage Server. Rows here add
    # people on top of that; turning managers_allowed off narrows it to the
    # owner plus the people listed.

    def list_dashboard_access(self, guild_id: int) -> list[tuple[str, str, Optional[str], int]]:
        cur = self.conn.execute(
            "SELECT user_id, level, added_by, added_at FROM dashboard_access WHERE guild_id = ? ORDER BY added_at",
            (guild_id,),
        )
        return [tuple(row) for row in cur.fetchall()]

    def get_dashboard_access_level(self, guild_id: int, user_id: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT level FROM dashboard_access WHERE guild_id = ? AND user_id = ?",
            (guild_id, str(user_id)),
        ).fetchone()
        return row[0] if row else None

    def set_dashboard_access(self, guild_id: int, user_id: str, level: str, added_by: Optional[str]) -> None:
        if level not in ("full", "staff"):
            raise ValueError("level must be 'full' or 'staff'")
        self.conn.execute(
            "INSERT INTO dashboard_access (guild_id, user_id, level, added_by, added_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET level = excluded.level",
            (guild_id, str(user_id), level, added_by, int(time.time())),
        )
        self.conn.commit()

    def remove_dashboard_access(self, guild_id: int, user_id: str) -> None:
        self.conn.execute(
            "DELETE FROM dashboard_access WHERE guild_id = ? AND user_id = ?",
            (guild_id, str(user_id)),
        )
        self.conn.commit()

    def get_dashboard_managers_allowed(self, guild_id: int) -> bool:
        row = self.conn.execute(
            "SELECT managers_allowed FROM dashboard_guild_settings WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return True if row is None else bool(row[0])

    def set_dashboard_managers_allowed(self, guild_id: int, allowed: bool) -> None:
        self.conn.execute(
            "INSERT INTO dashboard_guild_settings (guild_id, managers_allowed) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET managers_allowed = excluded.managers_allowed",
            (guild_id, 1 if allowed else 0),
        )
        self.conn.commit()

    def find_member_id(self, guild_id: int, query: str) -> Optional[int]:
        """Resolve a pasted user ID, or an exact username/display name, to a cached member ID."""
        query = query.strip().lstrip("@")
        if not query:
            return None
        if query.isdigit():
            return int(query) if 15 <= len(query) <= 21 else None
        rows = self.conn.execute(
            "SELECT user_id FROM bot_members WHERE guild_id = ? AND (LOWER(name) = LOWER(?) OR LOWER(display_name) = LOWER(?))",
            (guild_id, query, query),
        ).fetchall()
        return int(rows[0][0]) if len(rows) == 1 else None  # ambiguous names resolve to nothing

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
            perms = getattr(getattr(role, "permissions", None), "value", 0)
            self.conn.execute(
                "INSERT INTO bot_roles (guild_id, role_id, name, position, permissions, managed) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, int(role.id), role.name, int(getattr(role, "position", 0)), int(perms), int(getattr(role, "managed", False))),
            )
        self.conn.commit()

    def upsert_bot_role(self, guild_id: int, role_id: int, name: str, position: int = 0, permissions: int = 0, managed: bool = False) -> None:
        if role_id == guild_id:
            return
        self.conn.execute(
            "INSERT INTO bot_roles (guild_id, role_id, name, position, permissions, managed) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, role_id) DO UPDATE SET name=excluded.name, position=excluded.position, "
            "permissions=excluded.permissions, managed=excluded.managed",
            (guild_id, role_id, name, position, permissions, int(managed)),
        )
        self.conn.commit()

    def remove_bot_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute("DELETE FROM bot_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id))
        self.conn.commit()

    def list_bot_roles(self, guild_id: int) -> list[tuple[int, str, int]]:
        return self.conn.execute("SELECT role_id, name, position FROM bot_roles WHERE guild_id = ? ORDER BY position DESC", (guild_id,)).fetchall()

    def list_bot_roles_full(self, guild_id: int) -> list[tuple[int, str, int, int, bool]]:
        """Same roles as list_bot_roles, but with permissions/managed too -
        kept as a separate method (rather than changing list_bot_roles'
        return shape) since that one's 3-tuple is unpacked directly in a
        dozen+ WebUI templates for role dropdowns; this one exists
        specifically for the security scanner."""
        cur = self.conn.execute(
            "SELECT role_id, name, position, permissions, managed FROM bot_roles WHERE guild_id = ? ORDER BY position DESC",
            (guild_id,),
        )
        return [(rid, name, pos, perms, bool(managed)) for rid, name, pos, perms, managed in cur.fetchall()]

    def set_everyone_permissions(self, guild_id: int, permissions: int) -> None:
        """@everyone is deliberately excluded from bot_roles (it's not a
        real assignable/orderable role, and every role dropdown in the
        WebUI reads from that table) - this stores just its permission
        bitfield for the security scanner, which does need to know about
        it."""
        self.conn.execute(
            "INSERT INTO bot_guilds (guild_id, name, everyone_permissions) VALUES (?, '', ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET everyone_permissions = excluded.everyone_permissions",
            (guild_id, permissions),
        )
        self.conn.commit()

    def set_guild_info(
        self, guild_id: int, *, owner_id: int = 0, created_at: int = 0, boost_level: int = 0, boost_count: int = 0,
        verification_level: str = "", member_total: int = 0, emoji_count: int = 0, bot_permissions: int = 0,
    ) -> None:
        self.conn.execute(
            """INSERT INTO bot_guilds (guild_id, name, owner_id, created_at, boost_level, boost_count,
                   verification_level, member_total, emoji_count, bot_permissions)
               VALUES (?, '', ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET owner_id = excluded.owner_id, created_at = excluded.created_at,
                   boost_level = excluded.boost_level, boost_count = excluded.boost_count,
                   verification_level = excluded.verification_level, member_total = excluded.member_total,
                   emoji_count = excluded.emoji_count, bot_permissions = excluded.bot_permissions""",
            (guild_id, owner_id, created_at, boost_level, boost_count, verification_level, member_total,
             emoji_count, bot_permissions),
        )
        self.conn.commit()

    def get_guild_info(self, guild_id: int) -> dict:
        row = self.conn.execute(
            """SELECT owner_id, created_at, boost_level, boost_count, verification_level, member_total,
                      emoji_count, bot_permissions FROM bot_guilds WHERE guild_id = ?""",
            (guild_id,),
        ).fetchone()
        keys = ("owner_id", "created_at", "boost_level", "boost_count", "verification_level",
                "member_total", "emoji_count", "bot_permissions")
        return dict(zip(keys, row)) if row else dict.fromkeys(keys, 0)

    def get_temp_voice_settings(self, guild_id: int) -> dict:
        """Settings for what the creator of a temp voice channel gets. Always
        clamped to TEMP_VOICE_OWNER_MASK on the way out, so a bad stored value
        can never widen what the bot grants."""
        row = self.conn.execute(
            "SELECT owner_perms, create_role, role_hoist, log_channel_id, name_template, max_channels "
            "FROM temp_voice_settings WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if row is None:
            return {
                "owner_perms": TEMP_VOICE_DEFAULT_OWNER_PERMS, "create_role": False, "role_hoist": True,
                "log_channel_id": 0, "name_template": "", "max_channels": 25,
            }
        return {
            "owner_perms": int(row[0]) & TEMP_VOICE_OWNER_MASK,
            "create_role": bool(row[1]), "role_hoist": bool(row[2]),
            "log_channel_id": int(row[3]), "name_template": row[4],
            "max_channels": max(1, min(int(row[5]), TEMP_VOICE_MAX_CHANNELS_CEILING)),
        }

    def set_temp_voice_settings(
        self, guild_id: int, *, owner_perms: int, create_role: bool, role_hoist: bool,
        log_channel_id: int, name_template: str, max_channels: int,
    ) -> None:
        name_template = (name_template or "").strip()[:60]
        self.conn.execute(
            """INSERT INTO temp_voice_settings
                   (guild_id, owner_perms, create_role, role_hoist, log_channel_id, name_template, max_channels)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET owner_perms = excluded.owner_perms,
                   create_role = excluded.create_role, role_hoist = excluded.role_hoist,
                   log_channel_id = excluded.log_channel_id, name_template = excluded.name_template,
                   max_channels = excluded.max_channels""",
            (guild_id, int(owner_perms) & TEMP_VOICE_OWNER_MASK, 1 if create_role else 0, 1 if role_hoist else 0,
             int(log_channel_id), name_template, max(1, min(int(max_channels), TEMP_VOICE_MAX_CHANNELS_CEILING))),
        )
        self.conn.commit()

    def set_temp_voice_role(self, channel_id: int, role_id: int) -> None:
        self.conn.execute("UPDATE temp_voice_channels SET role_id = ? WHERE channel_id = ?", (role_id, channel_id))
        self.conn.commit()

    def get_temp_voice_info(self, channel_id: int) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT guild_id, owner_id, user_limit, role_id FROM temp_voice_channels WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
        if not row:
            return None
        return {"guild_id": row[0], "owner_id": row[1], "user_limit": row[2], "role_id": row[3]}

    def count_temp_voice_channels(self, guild_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM temp_voice_channels WHERE guild_id = ?", (guild_id,)
        ).fetchone()[0]

    def find_temp_voice_by_owner(self, guild_id: int, owner_id: int) -> Optional[int]:
        row = self.conn.execute(
            "SELECT channel_id FROM temp_voice_channels WHERE guild_id = ? AND owner_id = ? LIMIT 1",
            (guild_id, owner_id),
        ).fetchone()
        return row[0] if row else None

    def get_everyone_permissions(self, guild_id: int) -> int:
        row = self.conn.execute("SELECT everyone_permissions FROM bot_guilds WHERE guild_id = ?", (guild_id,)).fetchone()
        return row[0] if row else 0

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
                "INSERT INTO bot_members (guild_id, user_id, name, display_name, status, is_bot) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, int(member.id), member.name, member.display_name, status, 1 if getattr(member, "bot", False) else 0),
            )
        self.conn.commit()

    def upsert_bot_member(
        self, guild_id: int, user_id: int, name: str, display_name: str, status: str = "offline", is_bot: bool = False
    ) -> None:
        self.conn.execute(
            "INSERT INTO bot_members (guild_id, user_id, name, display_name, status, is_bot) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET name=excluded.name, display_name=excluded.display_name, "
            "status=excluded.status, is_bot=excluded.is_bot",
            (guild_id, user_id, name, display_name, status, 1 if is_bot else 0),
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

    # ---- per-command toggles ----
    def is_command_enabled(self, guild_id: int, command_name: str) -> bool:
        row = self.conn.execute("SELECT enabled FROM command_toggles WHERE guild_id=? AND command_name=?", (guild_id, command_name)).fetchone()
        return True if row is None else bool(row[0])

    def set_command_enabled(self, guild_id: int, command_name: str, enabled: bool) -> None:
        self.conn.execute("INSERT INTO command_toggles(guild_id, command_name, enabled) VALUES(?,?,?) ON CONFLICT(guild_id, command_name) DO UPDATE SET enabled=excluded.enabled", (guild_id, command_name, int(enabled)))
        self.conn.commit()

    def get_disabled_commands(self, guild_id: int) -> set[str]:
        cur=self.conn.execute("SELECT command_name FROM command_toggles WHERE guild_id=? AND enabled=0", (guild_id,))
        return {r[0] for r in cur.fetchall()}
