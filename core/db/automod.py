"""AutoMod settings: detection config, invite-link controls, escalation tiers, exemption roles."""
import logging
import time
from typing import Optional

logger = logging.getLogger("db")


class AutomodMixin:

    # ---- automod ----

    _AUTOMOD_COLUMNS = (
        "enabled", "block_invites", "banned_words", "caps_percent", "caps_min_len",
        "mention_threshold", "spam_count", "spam_window_seconds",
        "duplicate_count", "duplicate_window_seconds",
        "violation_mute_threshold", "violation_window_seconds", "violation_mute_duration_seconds",
        "fuzzy_words", "queue_fuzzy_matches", "block_gifs",
        "invite_mode", "invite_allow_own",
    )

    AUTOMOD_INVITE_MODES = ("strike", "delete", "log")
    AUTOMOD_INVITE_ALLOW_LIMIT = 100

    def get_automod_config(self, guild_id: int) -> dict:
        cols = ", ".join(self._AUTOMOD_COLUMNS)
        row = self.conn.execute(
            f"SELECT {cols} FROM automod_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()

        if row is None:
            # insert a default row so future updates have something to
            # UPDATE against, then re-read it
            self.conn.execute("INSERT INTO automod_config (guild_id) VALUES (?)", (guild_id,))
            self.conn.commit()
            row = self.conn.execute(
                f"SELECT {cols} FROM automod_config WHERE guild_id = ?", (guild_id,)
            ).fetchone()

        result = dict(zip(self._AUTOMOD_COLUMNS, row))
        result["enabled"] = bool(result["enabled"])
        result["block_invites"] = bool(result["block_invites"])
        result["fuzzy_words"] = bool(result["fuzzy_words"])
        result["queue_fuzzy_matches"] = bool(result["queue_fuzzy_matches"])
        result["block_gifs"] = bool(result["block_gifs"])
        result["invite_allow_own"] = bool(result["invite_allow_own"])
        if result["invite_mode"] not in self.AUTOMOD_INVITE_MODES:
            result["invite_mode"] = "strike"
        result["banned_words"] = [w for w in result["banned_words"].split(",") if w.strip()]
        return result

    def set_automod_enabled(self, guild_id: int, enabled: bool) -> None:
        self.get_automod_config(guild_id)  # ensures a row exists
        self.conn.execute(
            "UPDATE automod_config SET enabled = ? WHERE guild_id = ?", (int(enabled), guild_id)
        )
        self.conn.commit()

    def set_automod_invites(self, guild_id: int, block: bool) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET block_invites = ? WHERE guild_id = ?", (int(block), guild_id)
        )
        self.conn.commit()

    # ---- invite-link controls ----

    def set_automod_invite_mode(self, guild_id: int, mode: str) -> None:
        """strike: delete + count toward the escalation ladder (the old
        behavior). delete: delete + log, but no strike. log: leave the
        message alone and only log it."""
        if mode not in self.AUTOMOD_INVITE_MODES:
            raise ValueError(f"mode must be one of {', '.join(self.AUTOMOD_INVITE_MODES)}")
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET invite_mode = ? WHERE guild_id = ?", (mode, guild_id)
        )
        self.conn.commit()

    def set_automod_invite_allow_own(self, guild_id: int, enabled: bool) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET invite_allow_own = ? WHERE guild_id = ?", (int(enabled), guild_id)
        )
        self.conn.commit()

    @staticmethod
    def parse_invite_allow_entry(text: str) -> tuple[str, str]:
        """Turns what a person typed into ("guild", "<server id>") or
        ("code", "<invite code>"). Accepts a server ID, a full invite link
        (discord.gg/abc, discord.com/invite/abc) or a bare invite code.
        Raises ValueError for anything else."""
        import re as _re
        raw = (text or "").strip()
        if not raw:
            raise ValueError("empty entry")
        if raw.isdigit():
            if not 15 <= len(raw) <= 22:
                raise ValueError("a server ID is 15-22 digits")
            return "guild", str(int(raw))
        match = _re.search(
            r"(?i)(?:discord\.gg|discordapp\.com/+invite|discord\.com/+invite)/+([a-zA-Z0-9-]+)", raw
        )
        code = match.group(1) if match else raw
        if not _re.fullmatch(r"[A-Za-z0-9-]{2,32}", code):
            raise ValueError("that doesn't look like an invite link, invite code or server ID")
        return "code", code

    def list_automod_invite_allow(self, guild_id: int) -> list[tuple[str, str]]:
        return [
            (row[0], row[1]) for row in self.conn.execute(
                "SELECT kind, value FROM automod_invite_allow WHERE guild_id = ? ORDER BY kind, value",
                (guild_id,),
            ).fetchall()
        ]

    def add_automod_invite_allow(self, guild_id: int, text: str) -> tuple[str, str]:
        kind, value = self.parse_invite_allow_entry(text)
        existing = self.list_automod_invite_allow(guild_id)
        if (kind, value) not in existing and len(existing) >= self.AUTOMOD_INVITE_ALLOW_LIMIT:
            raise ValueError(f"allowlist is full ({self.AUTOMOD_INVITE_ALLOW_LIMIT} entries)")
        self.conn.execute(
            "INSERT OR IGNORE INTO automod_invite_allow (guild_id, kind, value) VALUES (?, ?, ?)",
            (guild_id, kind, value),
        )
        self.conn.commit()
        return kind, value

    def remove_automod_invite_allow(self, guild_id: int, kind: str, value: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM automod_invite_allow WHERE guild_id = ? AND kind = ? AND value = ?",
            (guild_id, kind, value),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def list_automod_invite_exempt_channels(self, guild_id: int) -> list[int]:
        return [
            row[0] for row in self.conn.execute(
                "SELECT channel_id FROM automod_invite_exempt_channels WHERE guild_id = ? ORDER BY channel_id",
                (guild_id,),
            ).fetchall()
        ]

    def add_automod_invite_exempt_channel(self, guild_id: int, channel_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO automod_invite_exempt_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id),
        )
        self.conn.commit()

    def remove_automod_invite_exempt_channel(self, guild_id: int, channel_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM automod_invite_exempt_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def set_automod_block_gifs(self, guild_id: int, enabled: bool) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET block_gifs = ? WHERE guild_id = ?", (int(enabled), guild_id)
        )
        self.conn.commit()



    @staticmethod
    def normalize_gif_identifier(identifier: str) -> tuple[str, str]:
        value = (identifier or "").strip().strip("<>).,!?").lower()
        if not value:
            raise ValueError("GIF identifier cannot be empty")
        kind = "url" if value.startswith(("http://", "https://")) else "filename"
        return value, kind

    def list_automod_gif_allowlist(self, guild_id: int) -> list[tuple[str, str]]:
        return self.conn.execute(
            "SELECT identifier, kind FROM automod_gif_allowlist WHERE guild_id=? ORDER BY identifier", (guild_id,)
        ).fetchall()

    def add_automod_gif_allowlist(self, guild_id: int, identifier: str) -> None:
        value, kind = self.normalize_gif_identifier(identifier)
        self.conn.execute(
            "INSERT OR IGNORE INTO automod_gif_allowlist(guild_id,identifier,kind,created_at) VALUES(?,?,?,?)",
            (guild_id, value, kind, int(time.time())),
        )
        self.conn.commit()

    def remove_automod_gif_allowlist(self, guild_id: int, identifier: str) -> bool:
        value, _ = self.normalize_gif_identifier(identifier)
        cur=self.conn.execute("DELETE FROM automod_gif_allowlist WHERE guild_id=? AND identifier=?",(guild_id,value))
        self.conn.commit(); return cur.rowcount > 0

    def list_automod_gif_blocklist(self, guild_id: int) -> list[tuple[str, str]]:
        return self.conn.execute(
            "SELECT identifier, kind FROM automod_gif_blocklist WHERE guild_id=? ORDER BY identifier", (guild_id,)
        ).fetchall()

    def add_automod_gif_blocklist(self, guild_id: int, identifier: str) -> None:
        value, kind = self.normalize_gif_identifier(identifier)
        self.conn.execute(
            "INSERT OR IGNORE INTO automod_gif_blocklist(guild_id,identifier,kind,created_at) VALUES(?,?,?,?)",
            (guild_id, value, kind, int(time.time())),
        )
        self.conn.commit()

    def remove_automod_gif_blocklist(self, guild_id: int, identifier: str) -> bool:
        value, _ = self.normalize_gif_identifier(identifier)
        cur=self.conn.execute("DELETE FROM automod_gif_blocklist WHERE guild_id=? AND identifier=?",(guild_id,value))
        self.conn.commit(); return cur.rowcount > 0

    def automod_gif_list_matches(self, guild_id: int, identifiers: list[str]) -> tuple[bool, bool]:
        normalized=[]
        for ident in identifiers:
            try: normalized.append(self.normalize_gif_identifier(ident)[0])
            except ValueError: pass
        if not normalized: return False, False
        placeholders=','.join('?' for _ in normalized)
        params=[guild_id,*normalized]
        blocked=self.conn.execute(
            f"SELECT 1 FROM automod_gif_blocklist WHERE guild_id=? AND identifier IN ({placeholders}) LIMIT 1", params
        ).fetchone() is not None
        allowed=self.conn.execute(
            f"SELECT 1 FROM automod_gif_allowlist WHERE guild_id=? AND identifier IN ({placeholders}) LIMIT 1", params
        ).fetchone() is not None
        return blocked, allowed

    def get_escalation_reset(self, guild_id: int, user_id: int) -> int:
        row=self.conn.execute("SELECT reset_at FROM automod_escalation_state WHERE guild_id=? AND user_id=?",(guild_id,user_id)).fetchone()
        return int(row[0]) if row else 0

    def set_escalation_reset(self, guild_id: int, user_id: int, reset_at: int | None = None) -> None:
        reset_at=int(time.time()) if reset_at is None else int(reset_at)
        self.conn.execute(
            "INSERT INTO automod_escalation_state(guild_id,user_id,reset_at) VALUES(?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET reset_at=excluded.reset_at",
            (guild_id,user_id,reset_at),
        ); self.conn.commit()

    def count_recent_escalation_warnings(self, guild_id: int, user_id: int, since: int) -> int:
        reset=max(int(since), self.get_escalation_reset(guild_id,user_id))
        a=self.conn.execute("SELECT COUNT(*) FROM warns WHERE guild_id=? AND user_id=? AND created_at>=?",(guild_id,user_id,reset)).fetchone()[0]
        b=self.conn.execute("SELECT COUNT(*) FROM automod_violations WHERE guild_id=? AND user_id=? AND created_at>=?",(guild_id,user_id,reset)).fetchone()[0]
        return int(a+b)

    def get_votekick_config(self, guild_id: int) -> dict:
        row=self.conn.execute("SELECT enabled,required_votes,duration_seconds FROM votekick_config WHERE guild_id=?",(guild_id,)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO votekick_config(guild_id) VALUES(?)",(guild_id,)); self.conn.commit(); row=(0,5,600)
        return {"enabled":bool(row[0]),"required_votes":int(row[1]),"duration_seconds":int(row[2])}

    def set_votekick_config(self,guild_id:int,enabled:bool,required_votes:int|None=None,duration_seconds:int|None=None)->None:
        cfg=self.get_votekick_config(guild_id)
        rv=cfg["required_votes"] if required_votes is None else max(1,min(int(required_votes),100))
        ds=cfg["duration_seconds"] if duration_seconds is None else max(30,min(int(duration_seconds),86400))
        self.conn.execute("INSERT INTO votekick_config(guild_id,enabled,required_votes,duration_seconds) VALUES(?,?,?,?) ON CONFLICT(guild_id) DO UPDATE SET enabled=excluded.enabled,required_votes=excluded.required_votes,duration_seconds=excluded.duration_seconds",(guild_id,int(enabled),rv,ds)); self.conn.commit()

    def create_votekick(self,guild_id:int,channel_id:int,initiator_id:int,target_id:int,reason:str,created_at:int,expires_at:int)->int|None:
        active=self.conn.execute("SELECT id FROM votekicks WHERE guild_id=? AND target_id=? AND status='open'",(guild_id,target_id)).fetchone()
        if active: return None
        cur=self.conn.execute("INSERT INTO votekicks(guild_id,channel_id,initiator_id,target_id,reason,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",(guild_id,channel_id,initiator_id,target_id,reason,created_at,expires_at)); self.conn.commit(); return int(cur.lastrowid)

    def set_votekick_message_id(self,vote_id:int,message_id:int)->None:
        self.conn.execute("UPDATE votekicks SET message_id=? WHERE id=?",(message_id,vote_id)); self.conn.commit()

    def get_votekick(self,vote_id:int)->dict|None:
        row=self.conn.execute("SELECT id,guild_id,channel_id,message_id,initiator_id,target_id,reason,created_at,expires_at,status,result,resolved_at FROM votekicks WHERE id=?",(vote_id,)).fetchone()
        if not row:return None
        keys=('id','guild_id','channel_id','message_id','initiator_id','target_id','reason','created_at','expires_at','status','result','resolved_at')
        return dict(zip(keys,row))

    def list_open_votekicks(self, guild_id:int|None=None):
        if guild_id is None:
            rows=self.conn.execute("SELECT id,guild_id,channel_id,message_id,initiator_id,target_id,reason,created_at,expires_at,status,result,resolved_at FROM votekicks WHERE status='open' ORDER BY id").fetchall()
        else:
            rows=self.conn.execute("SELECT id,guild_id,channel_id,message_id,initiator_id,target_id,reason,created_at,expires_at,status,result,resolved_at FROM votekicks WHERE guild_id=? AND status='open' ORDER BY id",(guild_id,)).fetchall()
        keys=('id','guild_id','channel_id','message_id','initiator_id','target_id','reason','created_at','expires_at','status','result','resolved_at')
        return [dict(zip(keys,r)) for r in rows]

    def cast_votekick_vote(self,vote_id:int,user_id:int,vote:str)->tuple[bool,int,int]:
        if vote not in ('yes','no'): return False,0,0
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row=self.conn.execute("SELECT status FROM votekicks WHERE id=?",(vote_id,)).fetchone()
            if not row or row[0] != 'open': self.conn.rollback(); return False,0,0
            self.conn.execute("INSERT INTO votekick_votes(votekick_id,user_id,vote,created_at) VALUES(?,?,?,?) ON CONFLICT(votekick_id,user_id) DO UPDATE SET vote=excluded.vote,created_at=excluded.created_at",(vote_id,user_id,vote,int(time.time())))
            yes=self.conn.execute("SELECT COUNT(*) FROM votekick_votes WHERE votekick_id=? AND vote='yes'",(vote_id,)).fetchone()[0]
            no=self.conn.execute("SELECT COUNT(*) FROM votekick_votes WHERE votekick_id=? AND vote='no'",(vote_id,)).fetchone()[0]
            self.conn.commit(); return True,int(yes),int(no)
        except Exception:
            self.conn.rollback(); raise

    def count_votekick_votes(self, vote_id: int, vote: str) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM votekick_votes WHERE votekick_id=? AND vote=?", (vote_id, vote)).fetchone()[0])

    def close_votekick(self,vote_id:int,result:str)->bool:
        cur=self.conn.execute("UPDATE votekicks SET status='closed',result=?,resolved_at=? WHERE id=? AND status='open'",(result,int(time.time()),vote_id)); self.conn.commit(); return cur.rowcount>0

    def set_automod_fuzzy_words(self, guild_id: int, enabled: bool) -> None:
        """Toggles the 'alike words' fuzzy filter - see
        automod_checks.contains_banned_word's fuzzy= argument for what this
        actually changes about matching."""
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET fuzzy_words = ? WHERE guild_id = ?", (int(enabled), guild_id)
        )
        self.conn.commit()

    def set_automod_words(self, guild_id: int, words: list[str]) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET banned_words = ? WHERE guild_id = ?",
            (",".join(w.strip() for w in words if w.strip()), guild_id),
        )
        self.conn.commit()

    def set_automod_caps(self, guild_id: int, percent: int, min_len: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET caps_percent = ?, caps_min_len = ? WHERE guild_id = ?",
            (percent, min_len, guild_id),
        )
        self.conn.commit()

    def set_automod_mentions(self, guild_id: int, threshold: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET mention_threshold = ? WHERE guild_id = ?", (threshold, guild_id)
        )
        self.conn.commit()

    def set_automod_spam(self, guild_id: int, count: int, window_seconds: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET spam_count = ?, spam_window_seconds = ? WHERE guild_id = ?",
            (count, window_seconds, guild_id),
        )
        self.conn.commit()

    def set_automod_duplicates(self, guild_id: int, count: int, window_seconds: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET duplicate_count = ?, duplicate_window_seconds = ? WHERE guild_id = ?",
            (count, window_seconds, guild_id),
        )
        self.conn.commit()

    def set_automod_escalation(self, guild_id: int, violation_threshold: int, window_seconds: int, mute_duration_seconds: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            """UPDATE automod_config
               SET violation_mute_threshold = ?, violation_window_seconds = ?, violation_mute_duration_seconds = ?
               WHERE guild_id = ?""",
            (violation_threshold, window_seconds, mute_duration_seconds, guild_id),
        )
        self.conn.commit()

    def add_automod_violation(self, guild_id: int, user_id: int, reason: str, created_at: int) -> None:
        self.conn.execute(
            "INSERT INTO automod_violations (guild_id, user_id, reason, created_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, reason, created_at),
        )
        self.conn.commit()

    def count_recent_automod_violations(self, guild_id: int, user_id: int, since: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM automod_violations WHERE guild_id = ? AND user_id = ? AND created_at >= ?",
            (guild_id, user_id, since),
        ).fetchone()
        return row[0]

    def clear_automod_violations(self, guild_id: int, user_id: int) -> int:
        cur = self.conn.execute(
            "DELETE FROM automod_violations WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        self.conn.commit()
        return cur.rowcount

    def set_automod_violation_window(self, guild_id: int, window_seconds: int) -> None:
        self.get_automod_config(guild_id)
        self.conn.execute(
            "UPDATE automod_config SET violation_window_seconds = ? WHERE guild_id = ?",
            (window_seconds, guild_id),
        )
        self.conn.commit()

    # ---- automod escalation tiers ----
    # A tier fires once a member racks up `threshold` warnings within the
    # configured violation window. Several tiers can be configured per
    # guild (e.g. 3 -> mute, 5 -> kick, 8 -> ban) - see cogs/automod.py for
    # how they're matched and applied.

    def list_automod_escalation_tiers(self, guild_id: int) -> list[dict]:
        cur = self.conn.execute(
            """SELECT id, threshold, action, duration_seconds
               FROM automod_escalation_tiers WHERE guild_id = ? ORDER BY threshold ASC""",
            (guild_id,),
        )
        return [
            {"id": row[0], "threshold": row[1], "action": row[2], "duration_seconds": row[3]}
            for row in cur.fetchall()
        ]

    def set_automod_escalation_tier(self, guild_id: int, threshold: int, action: str, duration_seconds: Optional[int]) -> None:
        """Adds a tier, or replaces the existing tier at the same threshold
        (there can only be one action per threshold per guild)."""
        self.conn.execute(
            """INSERT OR REPLACE INTO automod_escalation_tiers (guild_id, threshold, action, duration_seconds)
               VALUES (?, ?, ?, ?)""",
            (guild_id, threshold, action, duration_seconds),
        )
        self.conn.commit()

    def remove_automod_escalation_tier(self, guild_id: int, tier_id: int) -> None:
        self.conn.execute(
            "DELETE FROM automod_escalation_tiers WHERE guild_id = ? AND id = ?", (guild_id, tier_id)
        )
        self.conn.commit()

    # ---- automod exemption roles / bot manager roles ----

    def list_automod_exempt_roles(self, guild_id: int) -> list[int]:
        cur = self.conn.execute(
            "SELECT role_id FROM automod_exempt_roles WHERE guild_id = ? ORDER BY role_id",
            (guild_id,),
        )
        return [row[0] for row in cur.fetchall()]

    def add_automod_exempt_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO automod_exempt_roles (guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id),
        )
        self.conn.commit()

    def remove_automod_exempt_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "DELETE FROM automod_exempt_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        self.conn.commit()

    def list_bot_manager_roles(self, guild_id: int) -> list[int]:
        cur = self.conn.execute(
            "SELECT role_id FROM bot_manager_roles WHERE guild_id = ? ORDER BY role_id",
            (guild_id,),
        )
        return [row[0] for row in cur.fetchall()]

    def add_bot_manager_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO bot_manager_roles (guild_id, role_id) VALUES (?, ?)",
            (guild_id, role_id),
        )
        self.conn.commit()

    def remove_bot_manager_role(self, guild_id: int, role_id: int) -> None:
        self.conn.execute(
            "DELETE FROM bot_manager_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        self.conn.commit()

    def list_automod_violations(self, guild_id: int, limit: int = 25) -> list[tuple[int, str, int]]:
        """Most recent AutoMod violations across the whole guild, newest
        first, as (user_id, reason, created_at) tuples - used by
        /logging import to backfill the automod log channel."""
        limit = max(1, min(int(limit), 200))
        cur = self.conn.execute(
            """SELECT user_id, reason, created_at FROM automod_violations
               WHERE guild_id = ? ORDER BY created_at DESC LIMIT ?""",
            (guild_id, limit),
        )
        return cur.fetchall()
