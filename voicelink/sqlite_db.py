"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.


ReedMuhn music integration derived from the MIT-licensed Vocard project.

See THIRD_PARTY_NOTICES.md and voicelink/VOCARD_LICENSE for full attribution.
"""

import copy
import json
import logging
import sqlite3
import time

from typing import Any, Dict, List, Optional

logger: logging.Logger = logging.getLogger("vocard.db")

_VALID_OPS = {"$set", "$unset", "$inc", "$push", "$pull"}


def _apply_ops(doc: Dict[str, Any], data: Dict[str, Dict[str, Any]]) -> None:
    """Applies SQLite-style update operators to `doc` in place. Mirrors
    the exact semantics of the original SQLiteMusicDB._update_db's inner
    loop (dotted-path traversal, auto-vivifying intermediate dicts,
    $push's $each/$slice form, $pull's $in form) so ported code that
    relies on those specific behaviors keeps working unchanged.
    """
    if not all(op in _VALID_OPS for op in data.keys()):
        raise ValueError(f"Invalid update operation. Must be one of {_VALID_OPS}")

    for mode, action in data.items():
        for key, value in action.items():
            cursors = key.split(".")
            nested = doc
            for c in cursors[:-1]:
                if not isinstance(nested, dict):
                    raise ValueError(f"Invalid path: {key}")
                nested = nested.setdefault(c, {})
            field = cursors[-1]

            try:
                if mode == "$set":
                    nested[field] = value
                elif mode == "$unset":
                    nested.pop(field, None)
                elif mode == "$inc":
                    if not isinstance(nested.get(field, 0), (int, float)):
                        raise ValueError(f"Cannot increment non-numeric field: {field}")
                    nested[field] = nested.get(field, 0) + value
                elif mode == "$push":
                    arr = nested.setdefault(field, [])
                    if not isinstance(arr, list):
                        raise ValueError(f"Cannot push to non-array field: {field}")
                    if isinstance(value, dict) and "$each" in value:
                        arr.extend(value["$each"])
                        if "$slice" in value:
                            arr[:] = arr[value["$slice"]:]
                    else:
                        arr.append(value)
                elif mode == "$pull":
                    if field in nested:
                        if not isinstance(nested[field], list):
                            raise ValueError(f"Cannot pull from non-array field: {field}")
                        values = value.get("$in", []) if isinstance(value, dict) else [value]
                        nested[field] = [item for item in nested[field] if item not in values]
            except Exception as e:
                raise ValueError(f"Error updating {key}: {str(e)}")


class SQLiteMusicDB:
    """Persistence interface used by the Vocard-derived music subsystem,
    backed by two SQLite tables of JSON blobs. Method names,
    signatures, caching behavior, and operator semantics are preserved so
    every existing voicelink/cogs call site keeps working unchanged."""

    _conn: Optional[sqlite3.Connection] = None
    _settings_buffer: Dict[int, Dict[str, Any]] = {}
    _users_buffer: Dict[int, Dict[str, Any]] = {}
    _last_access: Dict[int, float] = {}

    _CACHE_TTL: int = 300
    _MAX_CACHE_SIZE: int = 10000

    _user_base: Dict[str, Any] = {
        "_id": 0,
        "playlist": {
            "200": {
                "tracks": [],
                "perms": {"read": [], "write": [], "remove": []},
                "name": "Favourite",
                "type": "playlist",
            }
        },
        "history": [],
        "inbox": [],
    }

    # ---- init ----

    @classmethod
    async def init(cls, uri: str, db_name: str | None = None) -> None:
        """Initializes the SQLite music store.
        `uri` here is a filesystem path to a SQLite database file - pass
        ReedMuhn's own db path to share its connection's underlying file,
        or a separate path to keep Vocard's data in its own file. `db_name`
        `db_name` is retained as an unused compatibility parameter so the
        upstream-shaped call sites remain straightforward.
        """
        if not uri:
            logger.error("SQLite music-db initialization failed: no path given.")
            raise ValueError("A database file path must be provided.")
        if cls._conn is not None:
            logger.warning("Music DB is already initialized. Skipping reinitialization.")
            return
        cls._conn = sqlite3.connect(uri, check_same_thread=False, timeout=30)
        cls._conn.execute("PRAGMA busy_timeout = 30000")
        cls._conn.execute("PRAGMA journal_mode=WAL")
        cls._conn.execute("PRAGMA synchronous=NORMAL")
        cls._conn.execute(
            "CREATE TABLE IF NOT EXISTS vocard_settings (guild_id INTEGER PRIMARY KEY, data TEXT NOT NULL)"
        )
        cls._conn.execute(
            "CREATE TABLE IF NOT EXISTS vocard_users (user_id INTEGER PRIMARY KEY, data TEXT NOT NULL)"
        )
        cls._conn.commit()
        logger.info("SQLite music db initialized at %s", uri)

    @classmethod
    async def cleanup_cache(cls) -> None:
        current_time = time.time()
        expired = [
            gid for gid, last in cls._last_access.items()
            if current_time - last > cls._CACHE_TTL and (gid in cls._settings_buffer or gid in cls._users_buffer)
        ]
        for gid in expired:
            cls._settings_buffer.pop(gid, None)
            cls._users_buffer.pop(gid, None)
            cls._last_access.pop(gid, None)
        while len(cls._settings_buffer) + len(cls._users_buffer) > cls._MAX_CACHE_SIZE:
            oldest_id = min(cls._last_access.items(), key=lambda x: x[1])[0]
            cls._settings_buffer.pop(oldest_id, None)
            cls._users_buffer.pop(oldest_id, None)
            cls._last_access.pop(oldest_id, None)

    # ---- settings ----

    @classmethod
    def get_cached_settings(cls, guild_id: int) -> Dict[str, Any]:
        if guild_id not in cls._settings_buffer:
            return {}
        return copy.deepcopy(cls._settings_buffer[guild_id])

    @classmethod
    async def get_settings(cls, guild_id: int, *, deep_copy: bool = True, force_refresh: bool = False) -> Dict[str, Any]:
        if force_refresh or guild_id not in cls._settings_buffer:
            row = cls._conn.execute("SELECT data FROM vocard_settings WHERE guild_id = ?", (guild_id,)).fetchone()
            if row is None:
                settings = {"_id": guild_id}
                cls._conn.execute(
                    "INSERT INTO vocard_settings (guild_id, data) VALUES (?, ?)",
                    (guild_id, json.dumps(settings)),
                )
                cls._conn.commit()
            else:
                settings = json.loads(row[0])
            cls._settings_buffer[guild_id] = settings
            cls._last_access[guild_id] = time.time()
        buffer = cls._settings_buffer[guild_id]
        return copy.deepcopy(buffer) if deep_copy else buffer

    @classmethod
    async def update_settings(cls, guild_id: int, data: Dict[str, Dict[str, Any]], *, upsert: bool = False) -> bool:
        settings = await cls.get_settings(guild_id, deep_copy=False)
        try:
            _apply_ops(settings, data)
        except ValueError:
            if not upsert:
                raise
            settings = {"_id": guild_id, **data.get("$set", {})}
        cls._conn.execute(
            "INSERT INTO vocard_settings (guild_id, data) VALUES (?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET data = excluded.data",
            (guild_id, json.dumps(settings)),
        )
        cls._conn.commit()
        cls._settings_buffer[guild_id] = settings
        cls._last_access[guild_id] = time.time()
        return True

    # ---- users ----

    @classmethod
    async def get_user(cls, user_id: int, *, d_type: Optional[str] = None, need_copy: bool = True, force_refresh: bool = False) -> Dict[str, Any]:
        if force_refresh or user_id not in cls._users_buffer:
            row = cls._conn.execute("SELECT data FROM vocard_users WHERE user_id = ?", (user_id,)).fetchone()
            if row is None:
                user = {**copy.deepcopy(cls._user_base), "_id": user_id}
                cls._conn.execute(
                    "INSERT INTO vocard_users (user_id, data) VALUES (?, ?)",
                    (user_id, json.dumps(user)),
                )
                cls._conn.commit()
            else:
                user = json.loads(row[0])
            cls._users_buffer[user_id] = user
            cls._last_access[user_id] = time.time()

        user = cls._users_buffer[user_id]
        if d_type:
            if d_type not in cls._user_base:
                raise ValueError(f"Invalid data type: {d_type}")
            user = user.setdefault(d_type, copy.deepcopy(cls._user_base.get(d_type)))
        return copy.deepcopy(user) if need_copy else user

    @classmethod
    async def update_user(cls, user_id: int, data: Dict[str, Dict[str, Any]], *, upsert: bool = False) -> bool:
        user = await cls.get_user(user_id, need_copy=False)
        try:
            _apply_ops(user, data)
        except ValueError:
            if not upsert:
                raise
            user = {"_id": user_id, **data.get("$set", {})}
        cls._conn.execute(
            "INSERT INTO vocard_users (user_id, data) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
            (user_id, json.dumps(user)),
        )
        cls._conn.commit()
        cls._users_buffer[user_id] = user
        cls._last_access[user_id] = time.time()
        return True

    @classmethod
    async def delete_user(cls, user_id: int) -> bool:
        cur = cls._conn.execute("DELETE FROM vocard_users WHERE user_id = ?", (user_id,))
        cls._conn.commit()
        if cur.rowcount > 0:
            cls._users_buffer.pop(user_id, None)
            cls._last_access.pop(user_id, None)
            return True
        return False

    @classmethod
    async def get_users_by_criteria(cls, criteria: Dict[str, Any], *, limit: Optional[int] = None, skip: int = 0) -> List[Dict[str, Any]]:
        """Unused anywhere in Vocard's actual codebase (verified: no call
        site exists outside this class's own definition), so this only
        needs to support simple top-level-field equality, not the full
        query language - loads every user row and filters in Python,
        which is fine at ReedMuhn's scale and for a method nothing calls."""
        rows = cls._conn.execute("SELECT user_id, data FROM vocard_users").fetchall()
        users = []
        for user_id, data in rows:
            user = json.loads(data)
            if all(user.get(k) == v for k, v in criteria.items()):
                users.append(user)
        users = users[skip:]
        if limit:
            users = users[:limit]
        current_time = time.time()
        for user in users:
            cls._users_buffer[user["_id"]] = user
            cls._last_access[user["_id"]] = current_time
        return users
