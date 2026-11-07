"""Regression tests for poll voter visibility in storage and dashboard."""
from __future__ import annotations


def test_poll_voters_groups_current_vote_by_option(app_module):
    db = app_module.db
    poll_id = db.create_poll(111222333444555666, 987654321, "Test poll", ["A", "B"], 123, None)
    try:
        db.cast_poll_vote(poll_id, 101, 0)
        db.cast_poll_vote(poll_id, 202, 1)
        db.cast_poll_vote(poll_id, 303, 0)
        # Changing a vote must move that voter, not count them twice.
        db.cast_poll_vote(poll_id, 101, 1)
        assert db.poll_voters(poll_id, 2) == {0: [303], 1: [101, 202]}
        assert db.poll_results(poll_id, 2) == [1, 2]
    finally:
        db.conn.execute("DELETE FROM poll_votes WHERE poll_id=?", (poll_id,))
        db.conn.execute("DELETE FROM polls WHERE id=?", (poll_id,))
        db.conn.commit()


def test_dashboard_polls_page_lists_voters(authed_client, app_module):
    db = app_module.db
    guild_id = 111222333444555666
    poll_id = db.create_poll(guild_id, 987654321, "Who voted?", ["First", "Second"], 123, None)
    try:
        db.conn.execute(
            "INSERT OR REPLACE INTO bot_members (guild_id, user_id, name, display_name) VALUES (?, ?, ?, ?)",
            (guild_id, 456, "skytest", "SkyTestVoter"),
        )
        db.conn.commit()
        db.cast_poll_vote(poll_id, 456, 0)
        response = authed_client.get(f"/guild/{guild_id}/polls")
        assert response.status_code == 200
        assert "Voters (1)" in response.text
        assert "SkyTestVoter" in response.text
    finally:
        db.conn.execute("DELETE FROM poll_votes WHERE poll_id=?", (poll_id,))
        db.conn.execute("DELETE FROM polls WHERE id=?", (poll_id,))
        db.conn.execute("DELETE FROM bot_members WHERE guild_id=? AND user_id=?", (guild_id, 456))
        db.conn.commit()


def test_closed_poll_rejects_vote_at_database_boundary(app_module):
    db = app_module.db
    poll_id = db.create_poll(111222333444555666, 987654321, "Closed poll", ["A", "B"], 123, None)
    try:
        assert db.cast_poll_vote(poll_id, 101, 0) is True
        db.close_poll(poll_id)
        assert db.cast_poll_vote(poll_id, 202, 1) is False
        assert db.poll_results(poll_id, 2) == [1, 0]
    finally:
        db.conn.execute("DELETE FROM poll_votes WHERE poll_id=?", (poll_id,))
        db.conn.execute("DELETE FROM polls WHERE id=?", (poll_id,))
        db.conn.commit()


def test_vote_rejects_invalid_option_index(app_module):
    db = app_module.db
    poll_id = db.create_poll(111222333444555666, 987654321, "Option bounds", ["A", "B"], 123, None)
    try:
        assert db.cast_poll_vote(poll_id, 101, -1) is False
        assert db.cast_poll_vote(poll_id, 101, 2) is False
        assert db.poll_results(poll_id, 2) == [0, 0]
    finally:
        db.conn.execute("DELETE FROM poll_votes WHERE poll_id=?", (poll_id,))
        db.conn.execute("DELETE FROM polls WHERE id=?", (poll_id,))
        db.conn.commit()


def test_dashboard_can_retry_discord_update_for_closed_poll(authed_client, app_module):
    db = app_module.db
    guild_id = 111222333444555666
    poll_id = db.create_poll(guild_id, 987654321, "Retry finalization", ["A", "B"], 123, None)
    try:
        db.close_poll(poll_id)
        page = authed_client.get(f"/guild/{guild_id}/polls")
        assert page.status_code == 200
        assert "Retry Discord update" in page.text
        import re
        token = re.search(r'name="_csrf_token" value="([A-Za-z0-9_-]+)"', page.text).group(1)
        response = authed_client.post(
            f"/guild/{guild_id}/polls/close",
            data={"poll_id": str(poll_id), "_csrf_token": token},
            headers={"origin": "http://testserver"},
        )
        assert response.status_code == 303
        assert response.headers["location"] == f"/guild/{guild_id}/polls?closequeued={poll_id}"
        row = db.conn.execute(
            "SELECT status FROM dashboard_poll_close_requests WHERE guild_id=? AND poll_id=? ORDER BY id DESC LIMIT 1",
            (guild_id, poll_id),
        ).fetchone()
        assert row == ("queued",)
    finally:
        db.conn.execute("DELETE FROM dashboard_poll_close_requests WHERE guild_id=? AND poll_id=?", (guild_id, poll_id))
        db.conn.execute("DELETE FROM poll_votes WHERE poll_id=?", (poll_id,))
        db.conn.execute("DELETE FROM polls WHERE id=?", (poll_id,))
        db.conn.commit()
