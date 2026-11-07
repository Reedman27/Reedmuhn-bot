"""Moderation: warns, rules, reports, staff stats, review queue, sticky roles, tempnick rules, dashboard mod actions, emergency tools, antinuke, raid detection, invite tracking."""
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("db")


class ModerationMixin:


    # ---- muted-role channel policy retry queue ----

    def queue_muted_channel_sync(self, guild_id: int, channel_id: int, role_id: int,
                                 next_attempt_at=None, error=None) -> None:
        now = int(time.time())
        self.conn.execute(
            """INSERT INTO pending_muted_channel_syncs(guild_id,channel_id,role_id,attempts,next_attempt_at,last_error,created_at)
               VALUES(?,?,?,0,?,?,?)
               ON CONFLICT(guild_id,channel_id) DO UPDATE SET role_id=excluded.role_id,
                   next_attempt_at=excluded.next_attempt_at, last_error=excluded.last_error""",
            (guild_id, channel_id, role_id,
             int(next_attempt_at if next_attempt_at is not None else now + 120),
             (error or None) and str(error)[:500], now),
        )
        self.conn.commit()

    def list_due_muted_channel_syncs(self, now: int, limit: int = 50) -> list[tuple]:
        """(id, guild_id, channel_id, role_id, attempts)."""
        return self.conn.execute(
            """SELECT id, guild_id, channel_id, role_id, attempts FROM pending_muted_channel_syncs
               WHERE next_attempt_at <= ? ORDER BY next_attempt_at LIMIT ?""",
            (int(now), int(limit)),
        ).fetchall()

    def list_pending_muted_channel_syncs(self, guild_id: int, limit: int = 100) -> list[tuple]:
        return self.conn.execute(
            """SELECT id, channel_id, role_id, attempts, next_attempt_at, last_error
               FROM pending_muted_channel_syncs WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, int(limit)),
        ).fetchall()

    def resolve_muted_channel_sync(self, row_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM pending_muted_channel_syncs WHERE id = ?", (row_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def defer_muted_channel_sync(self, row_id: int, next_attempt_at: int, error=None) -> None:
        self.conn.execute(
            "UPDATE pending_muted_channel_syncs SET attempts = attempts + 1, next_attempt_at = ?, last_error = ? WHERE id = ?",
            (int(next_attempt_at), (error or None) and str(error)[:500], row_id),
        )
        self.conn.commit()

    def has_pending_unmute(self, guild_id: int, user_id: int) -> bool:
        """True if a scheduled unmute is still waiting for this member.

        Used when someone rejoins with a strip-mute role stash: if their mute
        hasn't expired yet the stash has to stay put, and if it has, their
        roles can be handed back immediately rather than sitting stashed
        forever because the scheduled unmute fired while they were gone."""
        for (raw,) in self.conn.execute(
            "SELECT data FROM scheduled_events WHERE guild_id=? AND event_name='unmute_role'", (guild_id,)
        ):
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if int(data.get("user_id", -1)) == int(user_id):
                return True
        return False

    def get_stripped_roles(self, guild_id: int, user_id: int) -> list[int]:
        """Read the roles stashed by a strip-roles mute without clearing them."""
        row = self.conn.execute(
            "SELECT role_ids FROM mute_stripped_roles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
        if row is None:
            return []
        return [int(rid) for rid in row[0].split(",") if rid.strip()]

    def clear_stripped_roles(self, guild_id: int, user_id: int) -> None:
        """Delete a successfully restored role stash."""
        self.conn.execute(
            "DELETE FROM mute_stripped_roles WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        self.conn.commit()

    def pop_stripped_roles(self, guild_id: int, user_id: int) -> list[int]:
        """Legacy helper: read and clear the stashed roles in one operation."""
        role_ids = self.get_stripped_roles(guild_id, user_id)
        if role_ids:
            self.clear_stripped_roles(guild_id, user_id)
        return role_ids

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
        self.record_member_history(guild_id, user_id, "warn", moderator_id, reason, f"warning_id={cur.lastrowid}", created_at, is_case=True)
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
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id, review_id, decision, resolved_by FROM automod_queue_decisions ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                self.conn.executemany("DELETE FROM automod_queue_decisions WHERE id = ?", [(row[0],) for row in rows])
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
    def set_automod_queue_fuzzy(self, guild_id: int, enabled: bool) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET queue_fuzzy_matches = ? WHERE guild_id = ?", (int(enabled), guild_id)
        )
        self.conn.commit()

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
        limit = max(1, min(int(limit), 100))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            # retry_at gates re-queued actions so a transient Discord failure
            # backs off instead of spinning on the next poll.
            rows = self.conn.execute(
                "SELECT id, guild_id, user_id, action, duration_seconds, reason FROM dashboard_mod_actions "
                "WHERE status = 'queued' AND COALESCE(retry_at, 0) <= ? ORDER BY id LIMIT ?",
                (int(time.time()), limit),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dashboard_mod_actions SET status='processing', error=NULL WHERE id IN ({placeholders}) AND status='queued'",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
    def mod_action_attempts(self, request_id: int) -> int:
        row = self.conn.execute("SELECT attempts FROM dashboard_mod_actions WHERE id=?", (request_id,)).fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def retry_mod_action(self, request_id: int, error: str, retry_at: int) -> None:
        """Put a queued mod action back in the queue after a transient Discord
        failure. Previously any failure - including a 429 or a 5xx - marked the
        action failed and it was simply never performed."""
        self.conn.execute(
            "UPDATE dashboard_mod_actions SET status='queued', error=?, attempts=COALESCE(attempts,0)+1, retry_at=? WHERE id=?",
            (str(error)[:500], int(retry_at), request_id),
        )
        self.conn.commit()

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
        limit = max(1, min(int(limit), 50))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id, action, params FROM dashboard_emergency_requests WHERE status = 'queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dashboard_emergency_requests SET status='processing', error=NULL WHERE id IN ({placeholders}) AND status='queued'",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
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

    def update_lockdown_state(self, guild_id: int, remaining_channel_overwrites: dict) -> None:
        """Rewrites the lockdown state to only the channels still restricted.

        Used by Unlock when some channels couldn't be restored: clearing the
        whole state in that case would make the bot forget those channels were
        ever locked, so a later Unlock retry would have nothing left to work
        from and the channels could stay locked indefinitely with no record of
        it. If nothing remains, this is equivalent to clearing the state."""
        if not remaining_channel_overwrites:
            self.clear_lockdown_state(guild_id)
            return
        self.conn.execute(
            "UPDATE emergency_lockdown_state SET channel_overwrites=? WHERE guild_id=?",
            (json.dumps({str(k): v for k, v in remaining_channel_overwrites.items()}), guild_id),
        )
        self.conn.commit()

    def clear_lockdown_state(self, guild_id: int) -> None:
        self.conn.execute("DELETE FROM emergency_lockdown_state WHERE guild_id=?", (guild_id,))
        self.conn.commit()

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
        limit = max(1, min(int(limit), 100))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id, reason FROM dashboard_mute_role_sync_requests WHERE status = 'queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dashboard_mute_role_sync_requests SET status='processing', error=NULL WHERE id IN ({placeholders}) AND status='queued'",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
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

    # ---- antinuke ----

    _ANTINUKE_COLUMNS = (
        "guild_id", "enabled", "auto_recovery", "default_punishment",
        "log_channel_id", "threshold", "window_seconds", "watched_actions",
    )

    def get_antinuke_config(self, guild_id: int) -> dict:
        cols = ", ".join(self._ANTINUKE_COLUMNS)
        row = self.conn.execute(
            f"SELECT {cols} FROM antinuke_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()

        if row is None:
            # insert a default row so future updates have something to
            # UPDATE against, then re-read it - same pattern as automod_config.
            self.conn.execute("INSERT INTO antinuke_config (guild_id) VALUES (?)", (guild_id,))
            self.conn.commit()
            row = self.conn.execute(
                f"SELECT {cols} FROM antinuke_config WHERE guild_id = ?", (guild_id,)
            ).fetchone()

        result = dict(zip(self._ANTINUKE_COLUMNS, row))
        result["enabled"] = bool(result["enabled"])
        result["auto_recovery"] = bool(result["auto_recovery"])
        result["watched_actions"] = [a for a in result["watched_actions"].split(",") if a.strip()]
        return result

    def set_antinuke_enabled(self, guild_id: int, enabled: bool) -> None:
        self.get_antinuke_config(guild_id)
        self.conn.execute(
            "UPDATE antinuke_config SET enabled = ? WHERE guild_id = ?", (int(enabled), guild_id)
        )
        self.conn.commit()

    def set_antinuke_auto_recovery(self, guild_id: int, enabled: bool) -> None:
        self.get_antinuke_config(guild_id)
        self.conn.execute(
            "UPDATE antinuke_config SET auto_recovery = ? WHERE guild_id = ?", (int(enabled), guild_id)
        )
        self.conn.commit()

    def set_antinuke_punishment(self, guild_id: int, punishment: str) -> None:
        self.get_antinuke_config(guild_id)
        self.conn.execute(
            "UPDATE antinuke_config SET default_punishment = ? WHERE guild_id = ?", (punishment, guild_id)
        )
        self.conn.commit()

    def set_antinuke_log_channel(self, guild_id: int, channel_id: Optional[int]) -> None:
        self.get_antinuke_config(guild_id)
        self.conn.execute(
            "UPDATE antinuke_config SET log_channel_id = ? WHERE guild_id = ?", (channel_id, guild_id)
        )
        self.conn.commit()

    def set_antinuke_threshold(self, guild_id: int, threshold: int, window_seconds: int) -> None:
        self.get_antinuke_config(guild_id)
        self.conn.execute(
            "UPDATE antinuke_config SET threshold = ?, window_seconds = ? WHERE guild_id = ?",
            (threshold, window_seconds, guild_id),
        )
        self.conn.commit()

    def set_antinuke_watched_actions(self, guild_id: int, actions: list[str]) -> None:
        self.get_antinuke_config(guild_id)
        self.conn.execute(
            "UPDATE antinuke_config SET watched_actions = ? WHERE guild_id = ?",
            (",".join(a.strip() for a in actions if a.strip()), guild_id),
        )
        self.conn.commit()

    def list_antinuke_whitelist(self, guild_id: int) -> list[int]:
        cur = self.conn.execute(
            "SELECT user_id FROM antinuke_whitelist WHERE guild_id = ?", (guild_id,)
        )
        return [row[0] for row in cur.fetchall()]

    def add_antinuke_whitelist(self, guild_id: int, user_id: int, added_by: int) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO antinuke_whitelist (guild_id, user_id, added_by, created_at)
               VALUES (?, ?, ?, ?)""",
            (guild_id, user_id, added_by, int(time.time())),
        )
        self.conn.commit()

    def remove_antinuke_whitelist(self, guild_id: int, user_id: int) -> None:
        self.conn.execute(
            "DELETE FROM antinuke_whitelist WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        self.conn.commit()

    def record_antinuke_incident(
        self, guild_id: int, user_id: int, trigger_action: str, punishment: str, hit_count: int
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO antinuke_incidents (guild_id, user_id, trigger_action, punishment, hit_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, user_id, trigger_action, punishment, hit_count, int(time.time())),
        )
        self.conn.commit()
        logger.info(
            "antinuke incident: guild=%s user=%s action=%s punishment=%s hits=%s",
            guild_id, user_id, trigger_action, punishment, hit_count,
        )
        return cur.lastrowid

    def list_antinuke_incidents(self, guild_id: int, limit: int = 50) -> list:
        cur = self.conn.execute(
            """SELECT id, user_id, trigger_action, punishment, hit_count, created_at
               FROM antinuke_incidents WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, limit),
        )
        return cur.fetchall()

    # ---- raid detection ----

    _RAID_COLUMNS = (
        "guild_id", "enabled", "join_threshold", "window_seconds",
        "action", "new_account_hours", "cooldown_seconds", "log_channel_id",
    )

    def get_raid_config(self, guild_id: int) -> dict:
        cols = ", ".join(self._RAID_COLUMNS)
        row = self.conn.execute(f"SELECT {cols} FROM raid_config WHERE guild_id = ?", (guild_id,)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO raid_config (guild_id) VALUES (?)", (guild_id,))
            self.conn.commit()
            row = self.conn.execute(f"SELECT {cols} FROM raid_config WHERE guild_id = ?", (guild_id,)).fetchone()
        result = dict(zip(self._RAID_COLUMNS, row))
        result["enabled"] = bool(result["enabled"])
        return result

    def set_raid_config(
        self, guild_id: int, *, enabled: bool, join_threshold: int, window_seconds: int,
        action: str, new_account_hours: int, cooldown_seconds: int, log_channel_id: Optional[int],
    ) -> None:
        self.get_raid_config(guild_id)
        self.conn.execute(
            """UPDATE raid_config SET enabled=?, join_threshold=?, window_seconds=?, action=?,
               new_account_hours=?, cooldown_seconds=?, log_channel_id=? WHERE guild_id=?""",
            (int(enabled), join_threshold, window_seconds, action, new_account_hours, cooldown_seconds, log_channel_id, guild_id),
        )
        self.conn.commit()

    def record_raid_incident(self, guild_id: int, join_count: int, window_seconds: int, action_taken: str, kicked_count: int = 0) -> int:
        cur = self.conn.execute(
            """INSERT INTO raid_incidents (guild_id, join_count, window_seconds, action_taken, kicked_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (guild_id, join_count, window_seconds, action_taken, kicked_count, int(time.time())),
        )
        self.conn.commit()
        logger.info("raid incident: guild=%s joins=%s/%ss action=%s kicked=%s", guild_id, join_count, window_seconds, action_taken, kicked_count)
        return cur.lastrowid

    def list_raid_incidents(self, guild_id: int, limit: int = 50) -> list:
        cur = self.conn.execute(
            """SELECT id, join_count, window_seconds, action_taken, kicked_count, created_at
               FROM raid_incidents WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, limit),
        )
        return cur.fetchall()

    # ---- invite tracking ----

    def record_invite_join(self, guild_id: int, user_id: int, inviter_id: Optional[int], invite_code: Optional[str], created_at: Optional[int] = None) -> int:
        created_at = int(time.time()) if created_at is None else int(created_at)
        cur = self.conn.execute(
            "INSERT INTO invite_joins (guild_id, user_id, inviter_id, invite_code, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, inviter_id, invite_code, created_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def count_invites_for_user(self, guild_id: int, inviter_id: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM invite_joins WHERE guild_id = ? AND inviter_id = ?", (guild_id, inviter_id)
        ).fetchone()[0]

    def list_invite_leaderboard(self, guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
        cur = self.conn.execute(
            """SELECT inviter_id, COUNT(*) as c FROM invite_joins
               WHERE guild_id = ? AND inviter_id IS NOT NULL
               GROUP BY inviter_id ORDER BY c DESC LIMIT ?""",
            (guild_id, limit),
        )
        return cur.fetchall()

    def list_recent_invite_joins(self, guild_id: int, limit: int = 50) -> list[tuple[int, Optional[int], Optional[str], int]]:
        cur = self.conn.execute(
            """SELECT user_id, inviter_id, invite_code, created_at FROM invite_joins
               WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, limit),
        )
        return cur.fetchall()

    def count_quick_leaves(self, guild_id: int, within_hours: int = 24) -> int:
        """How many invited members left within `within_hours` of joining -
        a rough "fake/low-quality invite" signal (someone joining and
        immediately leaving, common with giveaway-hunting or invite-reward
        farming)."""
        return self.conn.execute(
            """SELECT COUNT(*) FROM invite_joins ij
               JOIN bot_events be ON be.guild_id = ij.guild_id AND be.target_id = ij.user_id
                   AND be.event_type = 'member.leave' AND be.created_at BETWEEN ij.created_at AND ij.created_at + ?
               WHERE ij.guild_id = ?""",
            (within_hours * 3600, guild_id),
        ).fetchone()[0]

    def set_invite_milestone(self, guild_id: int, invite_count: int, role_id: int) -> None:
        self.conn.execute(
            "INSERT INTO invite_milestones (guild_id, invite_count, role_id) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, invite_count) DO UPDATE SET role_id = excluded.role_id",
            (guild_id, invite_count, role_id),
        )
        self.conn.commit()

    def remove_invite_milestone(self, guild_id: int, invite_count: int) -> None:
        self.conn.execute(
            "DELETE FROM invite_milestones WHERE guild_id = ? AND invite_count = ?", (guild_id, invite_count)
        )
        self.conn.commit()

    def list_invite_milestones(self, guild_id: int) -> list[tuple[int, int]]:
        cur = self.conn.execute(
            "SELECT invite_count, role_id FROM invite_milestones WHERE guild_id = ? ORDER BY invite_count ASC",
            (guild_id,),
        )
        return cur.fetchall()
