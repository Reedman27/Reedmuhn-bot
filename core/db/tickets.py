"""Tickets and modmail."""
import logging
import sqlite3
import time
from typing import Optional

logger = logging.getLogger("db")


class TicketsMixin:

    # ---- tickets ----

    def get_ticket_config(self, guild_id: int) -> dict:
        row = self.conn.execute(
            "SELECT category_id, support_role_id, panel_channel_id, panel_message_id, panel_title, panel_description, "
            "delete_on_close, delete_delay_seconds "
            "FROM ticket_config WHERE guild_id=?", (guild_id,)
        ).fetchone()
        if row is None:
            return {
                "category_id": None, "support_role_id": None, "panel_channel_id": None, "panel_message_id": None,
                "panel_title": "Support", "panel_description": "Click the button below to open a private ticket with the support team.",
                "delete_on_close": False, "delete_delay_seconds": 10,
            }
        return {
            "category_id": row[0], "support_role_id": row[1], "panel_channel_id": row[2], "panel_message_id": row[3],
            "panel_title": row[4], "panel_description": row[5],
            "delete_on_close": bool(row[6]), "delete_delay_seconds": row[7],
        }

    def set_ticket_config(self, guild_id: int, category_id: int | None, support_role_id: int | None) -> None:
        self.conn.execute(
            """INSERT INTO ticket_config (guild_id, category_id, support_role_id) VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET category_id=excluded.category_id, support_role_id=excluded.support_role_id""",
            (guild_id, category_id, support_role_id),
        )
        self.conn.commit()

    def set_ticket_delete_on_close(self, guild_id: int, enabled: bool, delay_seconds: int = 10) -> None:
        """Whether closing a ticket deletes its channel outright (after a
        short grace period so the closer can see the confirmation) instead
        of the default lock-and-rename-to-closed- behavior, which keeps the
        channel around for a transcript/records but leaves the ticket
        category filling up with closed channels over time."""
        self.conn.execute(
            """INSERT INTO ticket_config (guild_id, delete_on_close, delete_delay_seconds) VALUES (?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET delete_on_close=excluded.delete_on_close,
                   delete_delay_seconds=excluded.delete_delay_seconds""",
            (guild_id, int(enabled), delay_seconds),
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
        limit = max(1, min(int(limit), 100))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id FROM dashboard_ticket_panel_requests WHERE status='queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dashboard_ticket_panel_requests SET status='processing', error=NULL WHERE id IN ({placeholders}) AND status='queued'",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
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

    def try_create_ticket(self, guild_id: int, channel_id: int, opener_id: int, subject: str):
        """create_ticket, but returns None instead of raising when the opener
        already has an open ticket.

        The "do they already have one?" check in the cog is a read, so two
        button clicks in the same instant both passed it and both created a
        ticket (and a channel). The partial unique index on
        (guild_id, opener_id) WHERE status='open' makes the database the
        arbiter; the loser gets None back and cleans up its channel."""
        try:
            return self.create_ticket(guild_id, channel_id, opener_id, subject)
        except sqlite3.IntegrityError:
            return None

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
        closed = cur.rowcount > 0
        if closed and not getattr(self, "_ticket_index_ok", False):
            self._ensure_one_open_ticket_index()
            self.conn.commit()
        return closed

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
        limit = max(1, min(int(limit), 100))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id, ticket_id, reason FROM dashboard_ticket_close_requests WHERE status='queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dashboard_ticket_close_requests SET status='processing', error=NULL WHERE id IN ({placeholders}) AND status='queued'",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
    def complete_ticket_close(self, request_id: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_ticket_close_requests SET status=?, completed_at=?, error=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, request_id),
        )
        self.conn.commit()

    # ---- modmail ----

    def get_modmail_config(self, guild_id: int) -> dict:
        row = self.conn.execute(
            "SELECT enabled, category_id, log_channel_id, anonymous_staff FROM modmail_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if row is None:
            return {"guild_id": guild_id, "enabled": False, "category_id": None, "log_channel_id": None, "anonymous_staff": False}
        return {
            "guild_id": guild_id, "enabled": bool(row[0]), "category_id": row[1],
            "log_channel_id": row[2], "anonymous_staff": bool(row[3]),
        }

    def set_modmail_config(self, guild_id: int, *, enabled: bool, category_id: Optional[int], log_channel_id: Optional[int], anonymous_staff: bool) -> None:
        self.conn.execute(
            """INSERT INTO modmail_config (guild_id, enabled, category_id, log_channel_id, anonymous_staff)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled, category_id=excluded.category_id,
                   log_channel_id=excluded.log_channel_id, anonymous_staff=excluded.anonymous_staff""",
            (guild_id, int(enabled), category_id, log_channel_id, int(anonymous_staff)),
        )
        self.conn.commit()

    def create_modmail_thread(self, guild_id: int, channel_id: int, user_id: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO modmail_threads (guild_id, channel_id, user_id, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, channel_id, user_id, int(time.time())),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_modmail_thread_by_channel(self, channel_id: int) -> Optional[tuple]:
        return self.conn.execute(
            "SELECT id, guild_id, channel_id, user_id, status, created_at FROM modmail_threads WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()

    def get_open_modmail_thread_for_user(self, user_id: int) -> Optional[tuple]:
        """Return a DM thread only when exactly one open thread exists.

        A user can have open modmail threads in multiple servers; picking an
        arbitrary row can route a DM to the wrong staff team. Returning None
        when ambiguous lets the DM handler ask which server the message is
        about instead.
        """
        rows = self.conn.execute(
            "SELECT id, guild_id, channel_id, user_id, status, created_at FROM modmail_threads "
            "WHERE user_id = ? AND status = 'open' ORDER BY id DESC LIMIT 2",
            (user_id,),
        ).fetchall()
        return rows[0] if len(rows) == 1 else None

    def close_modmail_thread(self, thread_id: int, closed_by: Optional[int]) -> None:
        self.conn.execute(
            "UPDATE modmail_threads SET status='closed', closed_at=?, closed_by=? WHERE id=?",
            (int(time.time()), closed_by, thread_id),
        )
        self.conn.commit()

    def list_modmail_threads(self, guild_id: int, status: Optional[str] = None, limit: int = 50) -> list[tuple]:
        query = "SELECT id, channel_id, user_id, status, created_at, closed_at, closed_by FROM modmail_threads WHERE guild_id = ?"
        params: list = [guild_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(query, params).fetchall()

    def block_modmail_user(self, guild_id: int, user_id: int, blocked_by: Optional[int]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO modmail_blocks (guild_id, user_id, blocked_by, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, blocked_by, int(time.time())),
        )
        self.conn.commit()

    def unblock_modmail_user(self, guild_id: int, user_id: int) -> None:
        self.conn.execute("DELETE FROM modmail_blocks WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        self.conn.commit()

    def is_modmail_blocked(self, guild_id: int, user_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM modmail_blocks WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone() is not None

    def list_modmail_blocks(self, guild_id: int) -> list[tuple]:
        return self.conn.execute(
            "SELECT user_id, blocked_by, created_at FROM modmail_blocks WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        ).fetchall()
