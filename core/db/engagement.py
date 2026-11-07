"""Engagement: counting, leveling, economy, giveaways and other extras, polls."""
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("db")


class EngagementMixin:

    # ---- counting ----

    def get_counting(self, guild_id: int) -> Optional[dict]:
        row = self.conn.execute(
            """SELECT channel_id, current_number, last_user_id, high_score, save_milestone,
                      max_saves, high_score_alerts, saves_enabled, server_saves
               FROM counting WHERE guild_id = ?""",
            (guild_id,),
        ).fetchone()
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
            "saves_enabled": bool(row[7]),
            "server_saves": row[8],
        }

    def set_counting_channel(self, guild_id: int, channel_id: int) -> None:
        # Changing the channel keeps the current number, high score and saves.
        self.conn.execute(
            """INSERT INTO counting (guild_id, channel_id) VALUES (?, ?)
               ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id""",
            (guild_id, channel_id),
        )
        self.conn.commit()

    def set_high_score_alerts(self, guild_id: int, enabled: bool) -> None:
        self.conn.execute(
            """INSERT INTO counting (guild_id, channel_id, high_score_alerts) VALUES (?, 0, ?)
               ON CONFLICT(guild_id) DO UPDATE SET high_score_alerts = excluded.high_score_alerts""",
            (guild_id, int(enabled)),
        )
        self.conn.commit()

    def set_saves_enabled(self, guild_id: int, enabled: bool) -> None:
        self.conn.execute(
            """INSERT INTO counting (guild_id, channel_id, saves_enabled) VALUES (?, 0, ?)
               ON CONFLICT(guild_id) DO UPDATE SET saves_enabled = excluded.saves_enabled""",
            (guild_id, int(enabled)),
        )
        self.conn.commit()

    def set_save_settings(self, guild_id: int, interval: int, max_saves: int) -> None:
        """interval: a save is earned each time the count reaches a multiple
        of it. max_saves: size of the server's save bank (banked saves above
        a lowered cap are trimmed)."""
        self.conn.execute(
            """INSERT INTO counting (guild_id, channel_id, save_milestone, max_saves, server_saves)
               VALUES (?, 0, ?, ?, 0)
               ON CONFLICT(guild_id) DO UPDATE SET
                   save_milestone = excluded.save_milestone,
                   max_saves = excluded.max_saves,
                   server_saves = MIN(counting.server_saves, excluded.max_saves)""",
            (guild_id, interval, max_saves),
        )
        self.conn.commit()

    def apply_count_attempt(self, guild_id: int, user_id: int, value: int) -> Optional[dict]:
        """Judges one number posted in the counting channel and stores the
        result in the same step. Nothing here awaits, so the event loop
        handles messages one at a time and two people can never be judged
        against the same stored number.

        Returns None if the server has no counting channel. Otherwise a dict
        whose "result" is one of:
          "ok"    - correct. Also: number, earned_save, saves, new_high.
          "nudge" - wrong while the count is still 0, so nothing was lost.
          "saved" - wrong, but a server save was spent. Also: current,
                    expected, saves_left.
          "reset" - wrong; the count went back to 0. Also: lost (the number
                    it had reached).
        Every non-ok result also carries "reason": "wrong" or "repeat"
        (the same member counting twice in a row, even with the right
        number).
        """
        state = self.get_counting(guild_id)
        if state is None or not state["channel_id"]:
            return None

        current = state["current_number"]
        expected = current + 1
        repeat = state["last_user_id"] is not None and user_id == state["last_user_id"]

        if value == expected and not repeat:
            saves = state["server_saves"]
            earned = False
            interval = state["save_milestone"]
            if state["saves_enabled"] and interval > 0 and expected % interval == 0 and saves < state["max_saves"]:
                saves += 1
                earned = True
            previous_high = state["high_score"]
            self.conn.execute(
                """UPDATE counting SET current_number = ?, last_user_id = ?,
                       high_score = MAX(high_score, ?), server_saves = ?
                   WHERE guild_id = ?""",
                (expected, user_id, expected, saves, guild_id),
            )
            self.conn.commit()
            self.record_bot_event("counting.advance", guild_id, user_id, None, f"number={expected}")
            return {
                "result": "ok",
                "number": expected,
                "earned_save": earned,
                "saves": saves,
                "new_high": previous_high > 0 and expected > previous_high,
            }

        reason = "repeat" if repeat else "wrong"

        if current == 0:
            return {"result": "nudge", "reason": reason, "expected": expected}

        if state["saves_enabled"] and state["server_saves"] > 0:
            left = state["server_saves"] - 1
            self.conn.execute(
                "UPDATE counting SET server_saves = ? WHERE guild_id = ?", (left, guild_id)
            )
            self.conn.commit()
            self.record_bot_event(
                "counting.save_used", guild_id, user_id, None, f"number={current} saves_left={left}"
            )
            return {
                "result": "saved", "reason": reason, "current": current,
                "expected": expected, "saves_left": left,
            }

        self.conn.execute(
            "UPDATE counting SET current_number = 0, last_user_id = NULL WHERE guild_id = ?",
            (guild_id,),
        )
        self.conn.commit()
        self.record_bot_event("counting.reset", guild_id, user_id, None, f"lost={current}")
        return {"result": "reset", "reason": reason, "lost": current}

    # ---- extras (leveling, economy, counters, twitch, feeds, giveaways) ----

    @staticmethod
    def _extras_level_from_xp(xp: int) -> int:
        """Mirrors cogs.extras.Extras._level_from_xp's curve. Duplicated
        deliberately, not imported - the WebUI process doesn't import cogs
        (see the note atop this file about core/db.py being a deliberate
        copy of this one, not an import).

        Curve: total XP needed for level n = (5*n*(2n^2 + 27n + 91)) // 6,
        the same shape used by several popular leveling bots (MEE6 among
        them) - early levels are quick, later ones stretch out considerably.
        Replaces the old flat 100*level^2 curve, which felt harsh early on
        and too flat later."""
        level = 0
        while EngagementMixin._extras_xp_for_level(level + 1) <= xp:
            level += 1
        return level

    @staticmethod
    def _extras_xp_for_level(level: int) -> int:
        return (5 * level * (2 * level * level + 27 * level + 91)) // 6

    def get_extras_xp(self, guild_id: int, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT xp FROM extras_xp WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ).fetchone()
        return int(row[0]) if row else 0

    def set_extras_xp(self, guild_id: int, user_id: int, xp: int) -> int:
        """Sets a member's XP to an absolute value (clamped to >= 0) and
        recomputes their level to match. Returns the resulting level.

        Kept for callers that only care about the new level;
        `set_extras_xp_detailed` is the one to use when the caller also has to
        apply the level-up side effects (role rewards / announcement)."""
        return self.set_extras_xp_detailed(guild_id, user_id, xp)["new_level"]

    def set_extras_xp_detailed(self, guild_id: int, user_id: int, xp: int) -> dict:
        """Absolute XP write inside one transaction. Returns
        {"xp", "old_xp", "old_level", "new_level"} so the caller can tell
        whether the change crossed any level boundaries - every XP write
        (bot or WebUI) goes through here or `add_extras_xp` so the level
        maths lives in exactly one place."""
        return self._write_extras_xp(guild_id, user_id, max(0, int(xp)), absolute=True)

    def add_extras_xp(self, guild_id: int, user_id: int, gained: int) -> dict:
        """Atomic relative XP change (read + write in one transaction), so two
        near-simultaneous messages can't both read the same starting XP and
        clobber each other. Same return shape as set_extras_xp_detailed."""
        return self._write_extras_xp(guild_id, user_id, int(gained), absolute=False)

    def _write_extras_xp(self, guild_id: int, user_id: int, value: int, absolute: bool) -> dict:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT xp, level FROM extras_xp WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            old_xp = int(row[0]) if row else 0
            old_level = int(row[1]) if row else 0
            xp = max(0, value if absolute else old_xp + value)
            new_level = self._extras_level_from_xp(xp)
            self.conn.execute(
                """INSERT INTO extras_xp(guild_id,user_id,xp,level) VALUES(?,?,?,?)
                   ON CONFLICT(guild_id,user_id) DO UPDATE SET xp=excluded.xp,level=excluded.level""",
                (guild_id, user_id, xp, new_level),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"xp": xp, "old_xp": old_xp, "old_level": old_level, "new_level": new_level}

    # ---- extras economy: atomic money operations ----
    # Every balance change below does its read and its write inside a single
    # BEGIN IMMEDIATE transaction. Bot and WebUI are separate processes on the
    # same SQLite file, so an application-level read/check/write sequence is
    # genuinely racy here - not just theoretically.

    def claim_extras_daily(
        self, guild_id: int, user_id: int, now: int, amount: int,
        streak_bonus: int, streak_bonus_cap: int, grace_seconds: int, cooldown_seconds: int = 86400,
    ) -> dict:
        """Atomically claim a daily reward.

        Returns {"claimed": True, "reward", "bonus", "balance", "streak"} or
        {"claimed": False, "remaining", "balance"} when still on cooldown. The
        cooldown check and the payout are one transaction, so two simultaneous
        claims can't both pass the check and pay out twice."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT balance, last_daily, daily_streak FROM extras_economy WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
            balance = int(row[0]) if row else 0
            last = int(row[1]) if row and row[1] is not None else None
            streak = int(row[2]) if row else 0
            if last is not None and now - last < cooldown_seconds:
                self.conn.commit()
                return {"claimed": False, "remaining": cooldown_seconds - (now - last), "balance": balance}
            streak = streak + 1 if last is not None and now - last < grace_seconds else 1
            bonus = min(streak_bonus_cap, streak_bonus * (streak - 1))
            reward = amount + bonus
            balance += reward
            self.conn.execute(
                """INSERT INTO extras_economy(guild_id,user_id,balance,last_daily,daily_streak) VALUES(?,?,?,?,?)
                   ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance,
                       last_daily=excluded.last_daily, daily_streak=excluded.daily_streak""",
                (guild_id, user_id, balance, now, streak),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {"claimed": True, "reward": reward, "bonus": bonus, "balance": balance, "streak": streak}

    def add_extras_balance(self, guild_id: int, user_id: int, delta: int) -> int:
        """Atomic balance increment/decrement (never below 0). Returns the new
        balance. Preserves last_daily/daily_streak."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO extras_economy(guild_id,user_id,balance,last_daily,daily_streak) VALUES(?,?,0,NULL,0)",
                (guild_id, user_id),
            )
            self.conn.execute(
                "UPDATE extras_economy SET balance = MAX(0, balance + ?) WHERE guild_id=? AND user_id=?",
                (int(delta), guild_id, user_id),
            )
            row = self.conn.execute(
                "SELECT balance FROM extras_economy WHERE guild_id=? AND user_id=?", (guild_id, user_id)
            ).fetchone()
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return int(row[0]) if row else 0

    def transfer_extras_balance(self, guild_id: int, sender_id: int, recipient_id: int, amount: int) -> bool:
        """Atomic coin transfer. The debit is a conditional UPDATE guarded on
        `balance >= amount`, so two simultaneous payments can never overspend;
        the recipient is credited only if that UPDATE actually matched a row.
        Returns False when the sender couldn't cover the amount."""
        amount = int(amount)
        if amount <= 0:
            return False
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "UPDATE extras_economy SET balance = balance - ? WHERE guild_id=? AND user_id=? AND balance >= ?",
                (amount, guild_id, sender_id, amount),
            )
            if cur.rowcount == 0:
                self.conn.commit()
                return False
            self.conn.execute(
                """INSERT INTO extras_economy(guild_id,user_id,balance,last_daily,daily_streak) VALUES(?,?,?,NULL,0)
                   ON CONFLICT(guild_id,user_id) DO UPDATE SET balance = balance + ?""",
                (guild_id, recipient_id, amount, amount),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return True

    # ---- role rewards that Discord refused (retry/repair queue) ----

    def queue_role_reward(
        self, guild_id: int, user_id: int, role_id: int, kind: str,
        reason: str = "", next_attempt_at: Optional[int] = None, error: Optional[str] = None,
    ) -> None:
        """Remember a role reward that was earned but not successfully granted
        so it can be retried later (or repaired by hand from the dashboard)."""
        now = int(time.time())
        self.conn.execute(
            """INSERT INTO pending_role_rewards(guild_id,user_id,role_id,kind,reason,attempts,next_attempt_at,last_error,created_at)
               VALUES(?,?,?,?,?,0,?,?,?)
               ON CONFLICT(guild_id,user_id,role_id) DO UPDATE SET
                   kind=excluded.kind, reason=excluded.reason, last_error=excluded.last_error""",
            (guild_id, user_id, role_id, kind, reason,
             int(next_attempt_at if next_attempt_at is not None else now), (error or None) and str(error)[:500], now),
        )
        self.conn.commit()
        logger.warning(
            "queued failed role reward for retry: guild=%s user=%s role=%s kind=%s error=%s",
            guild_id, user_id, role_id, kind, error,
        )

    def list_due_role_rewards(self, now: int, limit: int = 50) -> list[tuple]:
        """(id, guild_id, user_id, role_id, kind, reason, attempts) for every
        queued reward whose backoff has elapsed."""
        return self.conn.execute(
            """SELECT id, guild_id, user_id, role_id, kind, reason, attempts FROM pending_role_rewards
               WHERE next_attempt_at <= ? ORDER BY next_attempt_at LIMIT ?""",
            (int(now), int(limit)),
        ).fetchall()

    def list_pending_role_rewards(self, guild_id: int, limit: int = 100) -> list[tuple]:
        """Same rows as above, per guild and regardless of backoff - for the
        dashboard, so a failed reward is visible instead of invisible."""
        return self.conn.execute(
            """SELECT id, user_id, role_id, kind, reason, attempts, next_attempt_at, last_error
               FROM pending_role_rewards WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, int(limit)),
        ).fetchall()

    # ---- extras: level-up announcements, role rewards, no-XP channels, XP boost roles ----

    def get_extras_level_config(self, guild_id: int) -> dict:
        row = self.conn.execute(
            "SELECT announce_enabled, announce_channel_id, announce_message FROM extras_level_config WHERE guild_id=?",
            (guild_id,),
        ).fetchone()
        if row is None:
            return {"enabled": True, "channel_id": None, "message": "🎉 {user} reached **level {level}**!"}
        return {"enabled": bool(row[0]), "channel_id": row[1], "message": row[2]}

    def set_extras_level_config(self, guild_id: int, enabled: bool, channel_id: int | None, message: str) -> None:
        self.conn.execute(
            """INSERT INTO extras_level_config(guild_id,announce_enabled,announce_channel_id,announce_message)
               VALUES(?,?,?,?)
               ON CONFLICT(guild_id) DO UPDATE SET announce_enabled=excluded.announce_enabled,
                   announce_channel_id=excluded.announce_channel_id, announce_message=excluded.announce_message""",
            (guild_id, int(enabled), channel_id, message),
        )
        self.conn.commit()

    def list_extras_level_roles(self, guild_id: int):
        """Returns [(level, role_id), ...] ordered lowest level first."""
        return self.conn.execute(
            "SELECT level, role_id FROM extras_level_roles WHERE guild_id=? ORDER BY level ASC", (guild_id,)
        ).fetchall()

    def set_extras_level_role(self, guild_id: int, level: int, role_id: int) -> None:
        self.conn.execute(
            """INSERT INTO extras_level_roles(guild_id,level,role_id) VALUES(?,?,?)
               ON CONFLICT(guild_id,level) DO UPDATE SET role_id=excluded.role_id""",
            (guild_id, level, role_id),
        )
        self.conn.commit()

    def remove_extras_level_role(self, guild_id: int, level: int) -> bool:
        cur = self.conn.execute("DELETE FROM extras_level_roles WHERE guild_id=? AND level=?", (guild_id, level))
        self.conn.commit()
        return cur.rowcount > 0

    def list_extras_noxp_channels(self, guild_id: int) -> list[int]:
        return [row[0] for row in self.conn.execute(
            "SELECT channel_id FROM extras_noxp_channels WHERE guild_id=?", (guild_id,)
        ).fetchall()]

    def is_extras_noxp_channel(self, guild_id: int, channel_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM extras_noxp_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id)
        ).fetchone() is not None

    def add_extras_noxp_channel(self, guild_id: int, channel_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO extras_noxp_channels(guild_id,channel_id) VALUES(?,?)", (guild_id, channel_id)
        )
        self.conn.commit()

    def remove_extras_noxp_channel(self, guild_id: int, channel_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM extras_noxp_channels WHERE guild_id=? AND channel_id=?", (guild_id, channel_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_extras_xp_boost_roles(self, guild_id: int):
        """Returns [(role_id, multiplier), ...]."""
        return self.conn.execute(
            "SELECT role_id, multiplier FROM extras_xp_boost_roles WHERE guild_id=?", (guild_id,)
        ).fetchall()

    def set_extras_xp_boost_role(self, guild_id: int, role_id: int, multiplier: float) -> None:
        self.conn.execute(
            """INSERT INTO extras_xp_boost_roles(guild_id,role_id,multiplier) VALUES(?,?,?)
               ON CONFLICT(guild_id,role_id) DO UPDATE SET multiplier=excluded.multiplier""",
            (guild_id, role_id, multiplier),
        )
        self.conn.commit()

    def remove_extras_xp_boost_role(self, guild_id: int, role_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM extras_xp_boost_roles WHERE guild_id=? AND role_id=?", (guild_id, role_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_extras_rank(self, guild_id: int, user_id: int) -> int | None:
        """1-based leaderboard position by XP, or None if they have no XP row."""
        row = self.conn.execute(
            "SELECT COUNT(*) + 1 FROM extras_xp WHERE guild_id=? AND xp > (SELECT xp FROM extras_xp WHERE guild_id=? AND user_id=?)",
            (guild_id, guild_id, user_id),
        ).fetchone()
        exists = self.conn.execute(
            "SELECT 1 FROM extras_xp WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ).fetchone()
        return int(row[0]) if exists else None


    def list_extras_giveaways(self, guild_id: int, include_ended: bool = False):
        """Returns [(id, channel_id, message_id, prize, winners, end_at, ended), ...]."""
        if include_ended:
            return self.conn.execute(
                "SELECT id, channel_id, message_id, prize, winners, end_at, ended FROM extras_giveaways WHERE guild_id=? ORDER BY end_at DESC",
                (guild_id,),
            ).fetchall()
        return self.conn.execute(
            "SELECT id, channel_id, message_id, prize, winners, end_at, ended FROM extras_giveaways WHERE guild_id=? AND ended=0 ORDER BY end_at ASC",
            (guild_id,),
        ).fetchall()

    def list_extras_counters(self, guild_id: int):
        """Returns [(kind, channel_id), ...] for configured live counter channels."""
        return self.conn.execute(
            "SELECT kind, channel_id FROM extras_counters WHERE guild_id=?", (guild_id,)
        ).fetchall()

    def upsert_extras_counter(self, guild_id: int, kind: str, channel_id: int) -> None:
        self.conn.execute(
            "INSERT INTO extras_counters(guild_id,channel_id,kind) VALUES(?,?,?) "
            "ON CONFLICT(guild_id,kind) DO UPDATE SET channel_id=excluded.channel_id",
            (guild_id, channel_id, kind),
        )
        self.conn.commit()

    def delete_extras_counter(self, guild_id: int, kind: str) -> bool:
        cur = self.conn.execute("DELETE FROM extras_counters WHERE guild_id=? AND kind=?", (guild_id, kind))
        self.conn.commit()
        return cur.rowcount > 0

    def list_extras_twitch(self, guild_id: int):
        """Returns [(username, channel_id, last_live), ...]."""
        return self.conn.execute(
            "SELECT username, channel_id, last_live FROM extras_twitch WHERE guild_id=? ORDER BY username",
            (guild_id,),
        ).fetchall()

    def upsert_extras_twitch(self, guild_id: int, username: str, channel_id: int) -> None:
        self.conn.execute(
            "INSERT INTO extras_twitch(guild_id,username,channel_id,last_live) VALUES(?,?,?,0) "
            "ON CONFLICT(guild_id,username) DO UPDATE SET channel_id=excluded.channel_id",
            (guild_id, username.lower().strip(), channel_id),
        )
        self.conn.commit()

    def delete_extras_twitch(self, guild_id: int, username: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM extras_twitch WHERE guild_id=? AND username=?", (guild_id, username.lower().strip())
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_extras_feeds(self, guild_id: int):
        """Returns [(url, channel_id), ...]."""
        return self.conn.execute(
            "SELECT url, channel_id FROM extras_feeds WHERE guild_id=? ORDER BY url", (guild_id,)
        ).fetchall()

    def upsert_extras_feed(self, guild_id: int, url: str, channel_id: int) -> None:
        self.conn.execute(
            "INSERT INTO extras_feeds(guild_id,url,channel_id,last_id) VALUES(?,?,?,'') "
            "ON CONFLICT(guild_id,url) DO UPDATE SET channel_id=excluded.channel_id",
            (guild_id, url, channel_id),
        )
        self.conn.commit()

    def delete_extras_feed(self, guild_id: int, url: str) -> bool:
        cur = self.conn.execute("DELETE FROM extras_feeds WHERE guild_id=? AND url=?", (guild_id, url))
        self.conn.commit()
        return cur.rowcount > 0

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

    def cast_poll_vote(self, poll_id: int, user_id: int, option_index: int) -> bool:
        """Atomically cast/change a vote only while the poll is open.

        The poll's closed flag and vote write are checked under one SQLite
        write transaction so a click racing with poll closure cannot sneak a
        vote in after the poll has been finalized. Invalid option indexes are
        rejected at the storage boundary as well as in the UI.
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT options, closed FROM polls WHERE id=?", (poll_id,)
            ).fetchone()
            if row is None or bool(row[1]):
                self.conn.rollback()
                return False
            try:
                options = json.loads(row[0])
            except (TypeError, ValueError):
                self.conn.rollback()
                return False
            if not isinstance(option_index, int) or option_index < 0 or option_index >= len(options):
                self.conn.rollback()
                return False
            self.conn.execute(
                "INSERT INTO poll_votes (poll_id, user_id, option_index) VALUES (?, ?, ?) "
                "ON CONFLICT(poll_id, user_id) DO UPDATE SET option_index=excluded.option_index",
                (poll_id, user_id, option_index),
            )
            self.conn.commit()
            return True
        except Exception:
            self.conn.rollback()
            raise

    def poll_voters(self, poll_id: int, num_options: int | None = None) -> dict[int, list[int]]:
        """Return voter user IDs grouped by option index, in stable vote order."""
        grouped: dict[int, list[int]] = {}
        rows = self.conn.execute(
            "SELECT user_id, option_index FROM poll_votes WHERE poll_id=? ORDER BY option_index, user_id",
            (poll_id,),
        ).fetchall()
        for user_id, option_index in rows:
            if option_index < 0 or (num_options is not None and option_index >= num_options):
                continue
            grouped.setdefault(option_index, []).append(user_id)
        return grouped

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
        limit = max(1, min(int(limit), 100))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id, poll_id FROM dashboard_poll_close_requests WHERE status='queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dashboard_poll_close_requests SET status='processing', error=NULL WHERE id IN ({placeholders}) AND status='queued'",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
    def complete_poll_close(self, request_id: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_poll_close_requests SET status=?, completed_at=?, error=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, request_id),
        )
        self.conn.commit()
