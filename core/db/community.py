"""Community features: custom commands, reaction roles, welcome/autorole, verification, birthdays, starboard, suggestions."""
import json
import logging
import time
from typing import Optional

logger = logging.getLogger("db")


class CommunityMixin:

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
                "muted_strip_roles": True,
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
            """INSERT INTO guild_config (guild_id, muted_strip_roles, muted_strip_roles_migration_v2) VALUES (?, ?, 1)
               ON CONFLICT(guild_id) DO UPDATE SET muted_strip_roles = excluded.muted_strip_roles,
                 muted_strip_roles_migration_v2 = 1""",
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


    # ---- pending reaction-role operations (add/remove that Discord refused) ----

    def queue_role_op(self, guild_id: int, user_id: int, role_id: int, action: str,
                      reason: str = "", next_attempt_at=None, error=None) -> None:
        """Remember a reaction-role add/remove Discord rejected. A reaction is a
        one-shot event - the member won't react again - so without this queue a
        429 or a momentary hierarchy problem meant the role was never applied."""
        now = int(time.time())
        self.conn.execute(
            """INSERT INTO pending_role_ops(guild_id,user_id,role_id,action,reason,attempts,next_attempt_at,last_error,created_at)
               VALUES(?,?,?,?,?,0,?,?,?)
               ON CONFLICT(guild_id,user_id,role_id) DO UPDATE SET
                   action=excluded.action, reason=excluded.reason,
                   next_attempt_at=excluded.next_attempt_at, last_error=excluded.last_error""",
            (guild_id, user_id, role_id, action, reason,
             int(next_attempt_at if next_attempt_at is not None else now),
             (error or None) and str(error)[:500], now),
        )
        self.conn.commit()
        logger.warning(
            "queued failed reaction-role %s for retry: guild=%s user=%s role=%s error=%s",
            action, guild_id, user_id, role_id, error,
        )

    def list_due_role_ops(self, now: int, limit: int = 50) -> list[tuple]:
        """(id, guild_id, user_id, role_id, action, reason, attempts)."""
        return self.conn.execute(
            """SELECT id, guild_id, user_id, role_id, action, reason, attempts FROM pending_role_ops
               WHERE next_attempt_at <= ? ORDER BY next_attempt_at LIMIT ?""",
            (int(now), int(limit)),
        ).fetchall()

    def list_pending_role_ops(self, guild_id: int, limit: int = 100) -> list[tuple]:
        """Per-guild view for the dashboard, so a permanently failing binding is
        visible instead of invisible."""
        return self.conn.execute(
            """SELECT id, user_id, role_id, action, attempts, next_attempt_at, last_error
               FROM pending_role_ops WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, int(limit)),
        ).fetchall()

    def resolve_role_op_for(self, guild_id: int, user_id: int, role_id: int) -> bool:
        """Clear any queued retry once the member's role state is correct."""
        cur = self.conn.execute(
            "DELETE FROM pending_role_ops WHERE guild_id = ? AND user_id = ? AND role_id = ?",
            (guild_id, user_id, role_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def resolve_role_op(self, op_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM pending_role_ops WHERE id = ?", (op_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def defer_role_op(self, op_id: int, next_attempt_at: int, error=None) -> None:
        self.conn.execute(
            "UPDATE pending_role_ops SET attempts = attempts + 1, next_attempt_at = ?, last_error = ? WHERE id = ?",
            (int(next_attempt_at), (error or None) and str(error)[:500], op_id),
        )
        self.conn.commit()

    def resolve_role_reward(self, reward_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM pending_role_rewards WHERE id = ?", (reward_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def resolve_role_reward_for(self, guild_id: int, user_id: int, role_id: int) -> bool:
        """Clear any queued retry for this exact reward - used once the member
        actually holds the role, however they got it."""
        cur = self.conn.execute(
            "DELETE FROM pending_role_rewards WHERE guild_id = ? AND user_id = ? AND role_id = ?",
            (guild_id, user_id, role_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def defer_role_reward(self, reward_id: int, next_attempt_at: int, error: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE pending_role_rewards SET attempts = attempts + 1, next_attempt_at = ?, last_error = ? WHERE id = ?",
            (int(next_attempt_at), (error or None) and str(error)[:500], reward_id),
        )
        self.conn.commit()

    def get_extras_balance(self, guild_id: int, user_id: int) -> int:
        row = self.conn.execute(
            "SELECT balance FROM extras_economy WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ).fetchone()
        return int(row[0]) if row else 0

    def set_extras_balance(self, guild_id: int, user_id: int, balance: int) -> None:
        """Sets a member's coin balance to an absolute value (clamped to
        >= 0). Preserves their existing last_daily cooldown timestamp."""
        balance = max(0, balance)
        self.conn.execute(
            """INSERT INTO extras_economy(guild_id,user_id,balance,last_daily) VALUES(?,?,?,NULL)
               ON CONFLICT(guild_id,user_id) DO UPDATE SET balance=excluded.balance""",
            (guild_id, user_id, balance),
        )
        self.conn.commit()

    def list_extras_xp_leaderboard(self, guild_id: int, limit: int = 10):
        """Returns [(user_id, xp, level), ...] ordered highest XP first."""
        return self.conn.execute(
            "SELECT user_id, xp, level FROM extras_xp WHERE guild_id=? ORDER BY xp DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()

    def list_extras_economy_leaderboard(self, guild_id: int, limit: int = 10):
        """Returns [(user_id, balance, daily_streak), ...] ordered highest balance first."""
        return self.conn.execute(
            "SELECT user_id, balance, daily_streak FROM extras_economy WHERE guild_id=? ORDER BY balance DESC LIMIT ?",
            (guild_id, limit),
        ).fetchall()

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
        limit = max(1, min(int(limit), 100))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id, channel_id, message_id, emoji, action FROM reaction_role_actions ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            if rows:
                ids = [row[0] for row in rows]
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(f"DELETE FROM reaction_role_actions WHERE id IN ({placeholders})", ids)
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
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
        limit = max(1, min(int(limit), 100))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id, channel_id, user_id, amount, reason FROM dashboard_purge_requests WHERE status = 'queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dashboard_purge_requests SET status='processing', error=NULL WHERE id IN ({placeholders}) AND status='queued'",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
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
        limit = max(1, min(int(limit), 100))
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            rows = self.conn.execute(
                "SELECT id, guild_id FROM dashboard_verify_post_requests WHERE status='queued' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
            ids = [row[0] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self.conn.execute(
                    f"UPDATE dashboard_verify_post_requests SET status='processing', error=NULL WHERE id IN ({placeholders}) AND status='queued'",
                    ids,
                )
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise
    def complete_verify_post(self, request_id: int, error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE dashboard_verify_post_requests SET status=?, completed_at=?, error=? WHERE id=?",
            ('failed' if error else 'completed', int(time.time()), error, request_id),
        )
        self.conn.commit()

    # ---- starboard ----
    def get_starboard_config(self, guild_id: int) -> tuple:
        row=self.conn.execute("SELECT channel_id, threshold, enabled FROM starboard_config WHERE guild_id=?", (guild_id,)).fetchone()
        return row if row else (None,5,0)

    def set_starboard_config(self, guild_id:int, channel_id, threshold:int, enabled:bool):
        self.conn.execute("INSERT INTO starboard_config(guild_id,channel_id,threshold,enabled) VALUES(?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id, threshold=excluded.threshold, enabled=excluded.enabled", (guild_id,channel_id,threshold,int(enabled))); self.conn.commit()

    def get_starboard_message(self,guild_id:int,source_message_id:int):
        return self.conn.execute("SELECT starboard_message_id, channel_id, star_count FROM starboard_messages WHERE guild_id=? AND source_message_id=?",(guild_id,source_message_id)).fetchone()

    def claim_starboard_post(self, guild_id: int, source_message_id: int, channel_id: int, star_count: int) -> bool:
        """Reserve the right to post a starboard message for this source.

        Returns True only for the caller that inserted the placeholder row, so
        two reaction events arriving together can't both send a starboard post.
        The real starboard_message_id is filled in by upsert_starboard_message
        once the send succeeds; a 0 placeholder that never gets filled in is
        released by release_starboard_claim."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO starboard_messages(guild_id,source_message_id,starboard_message_id,channel_id,star_count) VALUES(?,?,0,?,?)",
                (guild_id, source_message_id, channel_id, star_count),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return cur.rowcount > 0

    def release_starboard_claim(self, guild_id: int, source_message_id: int) -> None:
        """Drop an unfulfilled placeholder claim (the Discord send failed), so
        a later reaction can try again."""
        self.conn.execute(
            "DELETE FROM starboard_messages WHERE guild_id=? AND source_message_id=? AND starboard_message_id=0",
            (guild_id, source_message_id),
        )
        self.conn.commit()

    def upsert_starboard_message(self,guild_id:int,source_message_id:int,starboard_message_id:int,channel_id:int,star_count:int):
        self.conn.execute("INSERT INTO starboard_messages(guild_id,source_message_id,starboard_message_id,channel_id,star_count) VALUES(?,?,?,?,?) ON CONFLICT(guild_id,source_message_id) DO UPDATE SET starboard_message_id=excluded.starboard_message_id,channel_id=excluded.channel_id,star_count=excluded.star_count",(guild_id,source_message_id,starboard_message_id,channel_id,star_count)); self.conn.commit()

    def delete_starboard_message(self,guild_id:int,source_message_id:int):
        self.conn.execute("DELETE FROM starboard_messages WHERE guild_id=? AND source_message_id=?",(guild_id,source_message_id)); self.conn.commit()

    # ---- suggestions ----
    def get_suggestion_config(self,guild_id:int)->tuple:
        row=self.conn.execute("SELECT channel_id,enabled,staff_role_id FROM suggestion_config WHERE guild_id=?",(guild_id,)).fetchone(); return row if row else (None,0,None)

    def set_suggestion_config(self,guild_id:int,channel_id,enabled:bool,staff_role_id):
        self.conn.execute("INSERT INTO suggestion_config(guild_id,channel_id,enabled,staff_role_id) VALUES(?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET channel_id=excluded.channel_id,enabled=excluded.enabled,staff_role_id=excluded.staff_role_id",(guild_id,channel_id,int(enabled),staff_role_id)); self.conn.commit()

    def create_suggestion(self,guild_id:int,message_id:int,author_id:int,content:str)->int:
        now=int(time.time()); cur=self.conn.execute("INSERT INTO suggestions(guild_id,message_id,author_id,content,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(guild_id,message_id,author_id,content,'pending',now,now)); self.conn.commit(); return cur.lastrowid

    def get_suggestion(self,suggestion_id:int):
        return self.conn.execute("SELECT id,guild_id,message_id,author_id,content,status,staff_id,staff_reason,created_at,updated_at FROM suggestions WHERE id=?",(suggestion_id,)).fetchone()

    def list_suggestions(self,guild_id:int,limit:int=50):
        return self.conn.execute("SELECT id,message_id,author_id,content,status,staff_id,staff_reason,created_at,updated_at FROM suggestions WHERE guild_id=? ORDER BY id DESC LIMIT ?",(guild_id,limit)).fetchall()

    def claim_suggestion_status(self, suggestion_id: int, status: str, staff_id: int, reason: str = "") -> bool:
        """Atomically move a suggestion out of `pending`.

        Two staff members clicking Approve/Deny at the same moment both used to
        read status='pending' and both wrote, so the final state was whichever
        write landed last and both edited the Discord message. The conditional
        UPDATE means exactly one caller wins; the loser is told it was already
        reviewed."""
        cur = self.conn.execute(
            "UPDATE suggestions SET status=?,staff_id=?,staff_reason=?,updated_at=? WHERE id=? AND status='pending'",
            (status, staff_id, reason, int(time.time()), suggestion_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_suggestion_status(self,suggestion_id:int,status:str,staff_id:int,reason:str=''):
        self.conn.execute("UPDATE suggestions SET status=?,staff_id=?,staff_reason=?,updated_at=? WHERE id=?",(status,staff_id,reason,int(time.time()),suggestion_id)); self.conn.commit()

    def release_all_starboard_claims(self) -> int:
        """Drop every placeholder claim left by a previous process.

        A claim is only meaningful while a send is in flight. If the bot was
        killed between claim_starboard_post() and the send finishing, the
        placeholder row stayed forever and that message could never be
        starboarded. Called once at cog load, when nothing can be in flight.
        """
        cur = self.conn.execute("DELETE FROM starboard_messages WHERE starboard_message_id=0")
        self.conn.commit()
        return cur.rowcount
