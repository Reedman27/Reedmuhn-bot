"""Temporary voice channels (create-a-VC) settings and room records."""
import logging
import time

logger = logging.getLogger("db")


class TempvoiceMixin:

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
