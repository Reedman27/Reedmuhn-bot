"""The AutoMod warning window: warnings must not age out unless asked to.

A window of 0 (the default) means every warning since a member's last
escalation reset counts toward the ladder. A positive value restores the old
rolling-window behaviour. Saved warnings are never deleted either way.
"""
from __future__ import annotations

import sqlite3
import time

from core.db import Db

GUILD = 1
USER = 42
MOD = 7
DAY = 86400


def test_new_servers_default_to_never_expiring(tmp_path):
    db = Db(str(tmp_path / "bot.db"))
    assert db.get_automod_config(GUILD)["violation_window_seconds"] == 0


def test_databases_created_with_the_old_default_still_get_zero_for_new_servers(tmp_path):
    """An existing install keeps the old 3600 column default in its table
    definition; new guilds must still start at 0."""
    path = str(tmp_path / "bot.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE automod_config (
            guild_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            block_invites INTEGER NOT NULL DEFAULT 1,
            banned_words TEXT NOT NULL DEFAULT '',
            caps_percent INTEGER NOT NULL DEFAULT 70,
            caps_min_len INTEGER NOT NULL DEFAULT 10,
            mention_threshold INTEGER NOT NULL DEFAULT 5,
            spam_count INTEGER NOT NULL DEFAULT 5,
            spam_window_seconds INTEGER NOT NULL DEFAULT 5,
            duplicate_count INTEGER NOT NULL DEFAULT 4,
            duplicate_window_seconds INTEGER NOT NULL DEFAULT 30,
            violation_mute_threshold INTEGER NOT NULL DEFAULT 3,
            violation_window_seconds INTEGER NOT NULL DEFAULT 3600,
            violation_mute_duration_seconds INTEGER NOT NULL DEFAULT 600,
            fuzzy_words INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute("INSERT INTO automod_config (guild_id) VALUES (99)")  # an existing server
    conn.commit()
    conn.close()

    db = Db(path)
    assert db.get_automod_config(99)["violation_window_seconds"] == 3600  # untouched
    assert db.get_automod_config(GUILD)["violation_window_seconds"] == 0  # new server


def test_window_start_helper():
    assert Db.violation_window_start({"violation_window_seconds": 0}, 10_000) == 0
    assert Db.violation_window_start({"violation_window_seconds": 3600}, 10_000) == 6_400


def test_old_warnings_still_count_when_the_window_is_zero(tmp_path):
    db = Db(str(tmp_path / "bot.db"))
    now = int(time.time())
    db.add_warn(GUILD, USER, MOD, "old", now - 3 * DAY)
    db.add_warn(GUILD, USER, MOD, "older", now - 30 * DAY)
    db.add_warn(GUILD, USER, MOD, "new", now - 60)

    cfg = db.get_automod_config(GUILD)
    since = db.violation_window_start(cfg, now)
    assert db.count_recent_escalation_warnings(GUILD, USER, since) == 3


def test_a_positive_window_still_ages_warnings_out(tmp_path):
    db = Db(str(tmp_path / "bot.db"))
    now = int(time.time())
    db.add_warn(GUILD, USER, MOD, "old", now - 3 * DAY)
    db.add_warn(GUILD, USER, MOD, "new", now - 60)

    db.set_automod_violation_window(GUILD, 3600)
    cfg = db.get_automod_config(GUILD)
    since = db.violation_window_start(cfg, now)
    assert db.count_recent_escalation_warnings(GUILD, USER, since) == 1
    # ...but nothing was deleted.
    assert db.count_warns(GUILD, USER) == 2


def test_an_escalation_reset_still_starts_the_count_over(tmp_path):
    db = Db(str(tmp_path / "bot.db"))
    now = int(time.time())
    db.add_warn(GUILD, USER, MOD, "before the punishment", now - 2 * DAY)
    db.set_escalation_reset(GUILD, USER, now - DAY)
    db.add_warn(GUILD, USER, MOD, "after", now - 60)

    cfg = db.get_automod_config(GUILD)
    since = db.violation_window_start(cfg, now)
    assert db.count_recent_escalation_warnings(GUILD, USER, since) == 1


def test_snapshots_without_a_window_restore_as_never_expiring(tmp_path):
    db = Db(str(tmp_path / "bot.db"))
    db.set_automod_violation_window(GUILD, 3600)
    db._restore_automod_config(GUILD, {"enabled": True})
    assert db.get_automod_config(GUILD)["violation_window_seconds"] == 0
