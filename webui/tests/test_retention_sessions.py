"""Regression tests for revocable WebUI sessions and Channel Feed retention."""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from conftest import TEST_GUILD_ID
from test_route_security import _token


def test_logout_revokes_a_copied_signed_cookie(authed_client, app_module):
    # Simulate a cookie copied by an attacker before the user logs out.
    stolen_cookie = authed_client.cookies.get("session")
    assert stolen_cookie
    session_id = app_module.db.conn.execute(
        "SELECT session_id FROM webui_sessions ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]
    assert app_module.db.is_webui_session_valid(session_id)

    token = _token(authed_client)
    logout_response = authed_client.post("/logout", data={"_csrf_token": token})
    assert logout_response.status_code == 303
    assert not app_module.db.is_webui_session_valid(session_id)

    replay_client = TestClient(app_module.app, follow_redirects=False)
    replay_client.cookies.set("session", stolen_cookie)
    response = replay_client.get("/dashboard-log")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_server_side_webui_session_expiry_is_enforced(app_module):
    session_id = "test-expired-session-id"
    app_module.db.create_webui_session(session_id, ttl_seconds=60)
    app_module.db.conn.execute(
        "UPDATE webui_sessions SET expires_at = ? WHERE session_id = ?",
        (int(time.time()) - 1, session_id),
    )
    app_module.db.conn.commit()
    assert not app_module.db.is_webui_session_valid(session_id)
    app_module.db.revoke_webui_session(session_id)


def _clear_test_feed(db, guild_id: int) -> None:
    db.conn.execute("DELETE FROM message_feed WHERE guild_id = ?", (guild_id,))
    db.conn.commit()
    db.set_feed_enabled(guild_id, False)


def test_disabling_channel_feed_purges_messages_and_blocks_inflight_insert(app_module):
    database = app_module.db
    guild_id = TEST_GUILD_ID
    _clear_test_feed(database, guild_id)
    database.set_feed_enabled(guild_id, True)
    database.record_feed_message(
        guild_id, 555, 1001, 777, "test-user", None, "private test content", [], int(time.time())
    )
    count = database.conn.execute(
        "SELECT COUNT(*) FROM message_feed WHERE guild_id = ?", (guild_id,)
    ).fetchone()[0]
    assert count == 1

    database.set_feed_enabled(guild_id, False)
    database.record_feed_message(
        guild_id, 555, 1002, 777, "test-user", None, "late write must be ignored", [], int(time.time())
    )
    count = database.conn.execute(
        "SELECT COUNT(*) FROM message_feed WHERE guild_id = ?", (guild_id,)
    ).fetchone()[0]
    assert count == 0


def test_channel_feed_retention_expires_old_messages(app_module):
    database = app_module.db
    guild_id = TEST_GUILD_ID
    _clear_test_feed(database, guild_id)
    database.set_feed_enabled(guild_id, True)
    now = int(time.time())
    database.record_feed_message(
        guild_id, 556, 2001, 777, "test-user", None, "too old", [], now - 10 * 86400
    )
    database.record_feed_message(
        guild_id, 556, 2002, 777, "test-user", None, "recent", [], now
    )
    database.prune_feed_messages(keep_per_channel=300, retention_days=7)  # opt-in expiry
    rows = database.conn.execute(
        "SELECT message_id FROM message_feed WHERE guild_id = ? AND channel_id = ?",
        (guild_id, 556),
    ).fetchall()
    assert rows == [(2002,)]
    _clear_test_feed(database, guild_id)


def test_channel_feed_default_prune_keeps_old_rows_under_cap(app_module):
    database = app_module.db
    guild_id = TEST_GUILD_ID
    _clear_test_feed(database, guild_id)
    database.set_feed_enabled(guild_id, True)
    database.record_feed_message(
        guild_id, 556, 3001, 777, "test-user", None, "old but under cap", [], int(time.time()) - 90 * 86400
    )
    database.prune_feed_messages(keep_per_channel=300)  # retention disabled by default
    rows = database.conn.execute(
        "SELECT message_id FROM message_feed WHERE guild_id = ? AND channel_id = ?",
        (guild_id, 556),
    ).fetchall()
    assert rows == [(3001,)]
    _clear_test_feed(database, guild_id)
