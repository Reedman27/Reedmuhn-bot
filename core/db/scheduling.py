"""Scheduled events: tempban unbans, reminders, and other timed jobs."""
import json
import logging
from typing import Optional

logger = logging.getLogger("db")


class SchedulingMixin:

    # ---- scheduled events (generalized: tempban unbans + reminders both
    # live here, same shape as yagpdb's real ScheduledEvents table) ----

    def insert_scheduled_event(self, event_name: str, guild_id: int, run_at: int, data: dict) -> None:
        self.conn.execute(
            "INSERT INTO scheduled_events (event_name, guild_id, run_at, data) VALUES (?, ?, ?, ?)",
            (event_name, guild_id, run_at, json.dumps(data)),
        )

        self.conn.commit()

    def due_events(self, now: int) -> list[tuple[int, str, int, str, int]]:
        """Returns (id, event_name, guild_id, data, attempts) for every event
        that is due. `attempts` lets the scheduler apply a backoff and stop
        retrying an event that keeps failing instead of looping forever."""
        cur = self.conn.execute(
            "SELECT id, event_name, guild_id, data, attempts FROM scheduled_events WHERE run_at <= ?",
            (now,),
        )
        return cur.fetchall()

    def reschedule_scheduled_event(self, event_id: int, run_at: int, error: Optional[str] = None) -> bool:
        """Push a failed scheduled event into the future instead of deleting
        it, bumping its attempt counter. Keeping the same row (rather than
        delete + re-insert) is what makes the retry budget meaningful."""
        cur = self.conn.execute(
            "UPDATE scheduled_events SET run_at = ?, attempts = attempts + 1, last_error = ? WHERE id = ?",
            (int(run_at), (error or None) and str(error)[:500], event_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

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

    def replace_role_unmute_event(self, guild_id: int, user_id: int, role_id: int, run_at: int) -> None:
        """Keep at most one pending unmute for a member/Muted-role pair.

        Re-muting an already muted member must extend/replace the expiry
        instead of leaving an older scheduled event that could unmute them
        early. JSON is decoded in Python rather than depending on SQLite's
        optional JSON1 extension.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, data FROM scheduled_events WHERE guild_id=? AND event_name='unmute_role'",
                (guild_id,),
            ).fetchall()
            for event_id, raw_data in rows:
                try:
                    data = json.loads(raw_data)
                except (TypeError, json.JSONDecodeError):
                    continue
                if int(data.get("user_id", -1)) == int(user_id) and int(data.get("role_id", -1)) == int(role_id):
                    self.conn.execute("DELETE FROM scheduled_events WHERE id=?", (event_id,))
            self.conn.execute(
                "INSERT INTO scheduled_events (event_name, guild_id, run_at, data) VALUES (?, ?, ?, ?)",
                ("unmute_role", guild_id, run_at, json.dumps({"user_id": user_id, "role_id": role_id})),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def delete_scheduled_event(self, event_id: int, guild_id: Optional[int] = None) -> bool:
        if guild_id is None:
            cur = self.conn.execute("DELETE FROM scheduled_events WHERE id = ?", (event_id,))
        else:
            cur = self.conn.execute("DELETE FROM scheduled_events WHERE id = ? AND guild_id = ?", (event_id, guild_id))
        self.conn.commit()
        return cur.rowcount > 0
