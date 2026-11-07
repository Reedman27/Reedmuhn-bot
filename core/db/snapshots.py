"""Config snapshots: export/restore of ReedMuhn's own settings tables."""
import json
import logging
import time
from typing import Optional

from ._conn import _DeferredCommitConn

logger = logging.getLogger("db")


class SnapshotsMixin:

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
        back.

        This calls the same public setters used elsewhere (set_welcome,
        set_autorole, ...) so the write logic for each section lives in
        exactly one place. The problem: every one of those setters commits
        internally, so a docstring claiming "one transaction" was previously
        just wrong - a mid-restore exception could leave some sections
        restored and others not, silently, with no rollback.

        Real fix: BEGIN here, then swap self.conn for a thin proxy whose
        commit() is a no-op, so none of the inner setters can end the
        transaction early - sqlite3.Connection objects don't allow
        overwriting their own bound methods directly (attribute assignment on
        the C-level Connection raises AttributeError), so the proxy swaps out
        the whole `self.conn` reference on this Db instance instead, which is
        an ordinary Python attribute and swaps back in `finally` regardless of
        outcome. Only the true underlying connection's own commit()/rollback()
        (called directly, past the proxy) actually ends the transaction.
        """
        real_conn = self.conn
        real_conn.execute("BEGIN IMMEDIATE")
        self.conn = _DeferredCommitConn(real_conn)
        try:
            self._restore_config_snapshot_data_unsafe(guild_id, data)
        except Exception:
            real_conn.rollback()
            raise
        else:
            real_conn.commit()
        finally:
            self.conn = real_conn

    def _restore_config_snapshot_data_unsafe(self, guild_id: int, data: dict) -> None:
        """The actual restore logic. Never call directly - only through
        restore_config_snapshot_data, which provides the transaction guard
        this method relies on."""
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
        # Invite-link controls: only restored if the snapshot has them, so
        # snapshots taken before they existed leave the current values alone.
        if data.get("invite_mode") in self.AUTOMOD_INVITE_MODES:
            self.conn.execute(
                "UPDATE automod_config SET invite_mode=? WHERE guild_id=?", (data["invite_mode"], guild_id)
            )
        if "invite_allow_own" in data:
            self.conn.execute(
                "UPDATE automod_config SET invite_allow_own=? WHERE guild_id=?",
                (int(bool(data["invite_allow_own"])), guild_id),
            )
        self.conn.execute("DELETE FROM automod_escalation_tiers WHERE guild_id=?", (guild_id,))
        for tier in data.get("escalation_tiers", []):
            self.conn.execute(
                "INSERT INTO automod_escalation_tiers (guild_id, threshold, action, duration_seconds) VALUES (?, ?, ?, ?)",
                (guild_id, tier["threshold"], tier["action"], tier.get("duration_seconds")),
            )
