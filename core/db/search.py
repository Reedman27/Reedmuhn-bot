"""Server-wide search and analytics queries used by the dashboard."""
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("db")


class SearchMixin:

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
        is_case: bool = False,
    ) -> int:
        """Returns the member_history row id, UNLESS is_case=True, in which
        case it returns the case number instead - that's what a caller
        actually wants to show a moderator ("Case #7"), and no caller of
        the is_case path needs the raw row id."""
        created_at = int(time.time()) if created_at is None else int(created_at)
        case_number = self._next_case_number(guild_id) if is_case else None
        cur = self.conn.execute(
            """INSERT INTO member_history
               (guild_id, user_id, event_type, actor_id, reason, details, created_at, case_number)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (guild_id, user_id, event_type, actor_id, reason, details, created_at, case_number),
        )
        self.conn.commit()
        logger.info("member history: guild=%s user=%s event=%s actor=%s reason=%s case=%s", guild_id, user_id, event_type, actor_id, reason, case_number)
        return case_number if is_case else cur.lastrowid

    def _next_case_number(self, guild_id: int) -> int:
        """Atomically hands out the next case number for a guild. The
        upsert-then-read happens inside the caller's existing connection,
        which is fine here since sqlite3 serializes writes on a single
        connection anyway - no risk of two calls handing out the same
        number from this process."""
        self.conn.execute(
            "INSERT INTO case_counters (guild_id, next_case) VALUES (?, 2) "
            "ON CONFLICT(guild_id) DO UPDATE SET next_case = next_case + 1",
            (guild_id,),
        )
        row = self.conn.execute(
            "SELECT next_case FROM case_counters WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        # The row now holds the number handed out *last* time (or 2 if this
        # was just created, meaning case #1 is the one we're recording now).
        current = row[0] - 1
        return current

    def get_case(self, guild_id: int, case_number: int) -> Optional[tuple]:
        cur = self.conn.execute(
            """SELECT id, user_id, event_type, actor_id, reason, details, created_at, voided
               FROM member_history WHERE guild_id = ? AND case_number = ?""",
            (guild_id, case_number),
        )
        return cur.fetchone()

    def list_cases_for_user(self, guild_id: int, user_id: int, limit: int = 50, include_voided: bool = True) -> list[tuple]:
        query = """SELECT case_number, event_type, actor_id, reason, details, created_at, voided
                   FROM member_history WHERE guild_id = ? AND user_id = ? AND case_number IS NOT NULL"""
        params: list = [guild_id, user_id]
        if not include_voided:
            query += " AND voided = 0"
        query += " ORDER BY case_number DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        return self.conn.execute(query, params).fetchall()

    def list_recent_cases(self, guild_id: int, limit: int = 20) -> list[tuple]:
        cur = self.conn.execute(
            """SELECT case_number, user_id, event_type, actor_id, reason, created_at, voided
               FROM member_history WHERE guild_id = ? AND case_number IS NOT NULL
               ORDER BY case_number DESC LIMIT ?""",
            (guild_id, max(1, min(int(limit), 200))),
        )
        return cur.fetchall()

    def count_active_cases_for_user(self, guild_id: int, user_id: int, event_type: Optional[str] = None) -> int:
        """Live (non-voided) case count for a user - the number an
        escalation rule would key off of."""
        query = "SELECT COUNT(*) FROM member_history WHERE guild_id = ? AND user_id = ? AND case_number IS NOT NULL AND voided = 0"
        params: list = [guild_id, user_id]
        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)
        return self.conn.execute(query, params).fetchone()[0]

    def edit_case_reason(self, guild_id: int, case_number: int, new_reason: str) -> bool:
        cur = self.conn.execute(
            "UPDATE member_history SET reason = ? WHERE guild_id = ? AND case_number = ?",
            (new_reason, guild_id, case_number),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def void_case(self, guild_id: int, case_number: int, voided: bool = True) -> bool:
        """Soft-delete: voided cases stay in the table (audit trail, case
        numbers never get reused) but are excluded from active counts and
        marked accordingly wherever they're displayed."""
        cur = self.conn.execute(
            "UPDATE member_history SET voided = ? WHERE guild_id = ? AND case_number = ?",
            (int(voided), guild_id, case_number),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def add_mod_note(self, guild_id: int, user_id: int, moderator_id: int, note: str, created_at: Optional[int] = None) -> int:
        created_at = int(time.time()) if created_at is None else int(created_at)
        cur = self.conn.execute(
            "INSERT INTO mod_notes (guild_id, user_id, moderator_id, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, note, created_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_mod_notes(self, guild_id: int, user_id: int, limit: int = 50) -> list[tuple]:
        cur = self.conn.execute(
            """SELECT id, moderator_id, note, created_at FROM mod_notes
               WHERE guild_id = ? AND user_id = ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, user_id, max(1, min(int(limit), 200))),
        )
        return cur.fetchall()

    def delete_mod_note(self, guild_id: int, note_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM mod_notes WHERE guild_id = ? AND id = ?", (guild_id, note_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

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
        bots = self.conn.execute(
            "SELECT COUNT(*) FROM bot_members WHERE guild_id = ? AND is_bot = 1", (guild_id,)
        ).fetchone()[0]
        return {
            "members": members, "online": online, "channels": channels, "roles": roles,
            "bots": bots, "humans": max(members - bots, 0),
        }
