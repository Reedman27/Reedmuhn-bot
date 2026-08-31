import concurrent.futures
import os
import sqlite3
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db import Db


def make_db():
    path = tempfile.mktemp(suffix='.db')
    return path, Db(path)


def close_db(path, *dbs):
    for db in dbs:
        db.conn.close()
    if os.path.exists(path):
        os.remove(path)


def test_features_and_mute_stash_round_trip():
    path, db = make_db()
    try:
        assert db.get_ai_config(1)["enabled"] is False
        db.set_ai_config(1, enabled=True, provider="ollama", base_url="http://127.0.0.1:11434/v1",
                          api_key="", model="llama3", system_prompt="test", max_tokens=900, temperature=.5)
        assert db.get_ai_config(1)["api_key"] == ""
        db.set_command_enabled(1, "ask", False)
        assert db.is_command_enabled(1, "ask") is False
        db.save_stripped_roles(1, 5, [30, 40])
        assert db.get_stripped_roles(1, 5) == [30, 40]
        db.clear_stripped_roles(1, 5)
        assert db.get_stripped_roles(1, 5) == []
    finally:
        close_db(path, db)


def test_role_unmute_expiry_is_replaced():
    path, db = make_db()
    try:
        db.insert_scheduled_event("unmute_role", 1, 100, {"user_id": 5, "role_id": 9})
        db.insert_scheduled_event("unmute_role", 1, 150, {"user_id": 6, "role_id": 9})
        db.replace_role_unmute_event(1, 5, 9, 300)
        rows = db.list_scheduled_events(1, "unmute_role")
        assert len(rows) == 2
        assert {row[2] for row in rows} == {150, 300}
    finally:
        close_db(path, db)


def test_invite_counts():
    path, db = make_db()
    try:
        for uid in range(10, 13):
            db.record_invite_join(1, uid, 99, "abc")
        assert db.count_invites_for_user(1, 99) == 3
        assert db.list_invite_leaderboard(1, 10)[0] == (99, 3)
    finally:
        close_db(path, db)


def test_queue_claim_is_atomic_across_connections():
    path, a = make_db()
    b = Db(path)
    try:
        for i in range(20):
            a.queue_mod_action(1, 100 + i, "warn", None, "test")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            batches = list(ex.map(lambda db: db.claim_mod_actions(10), (a, b)))
        ids = [row[0] for batch in batches for row in batch]
        assert len(ids) == 20
        assert len(set(ids)) == 20
    finally:
        close_db(path, a, b)


def test_legacy_guild_config_migrates():
    path = tempfile.mktemp(suffix='.db')
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE guild_config (guild_id INTEGER PRIMARY KEY, welcome_channel_id INTEGER, welcome_message TEXT, autorole_id INTEGER)")
    con.execute("INSERT INTO guild_config(guild_id) VALUES (123)")
    con.commit(); con.close()
    db = Db(path)
    try:
        cols = {r[1] for r in db.conn.execute("PRAGMA table_info(guild_config)")}
        assert {"birthday_channel_id", "tempnick_mode", "welcome_card_enabled", "muted_strip_roles"} <= cols
        assert db.get_ai_config(123)["enabled"] is False
    finally:
        db.conn.close()
        os.remove(path)


def test_escalation_counts_manual_and_automod_without_double_counting_and_resets():
    path, db = make_db()
    try:
        now = int(__import__('time').time())
        db.add_warn(1, 5, 10, 'manual', now)
        db.add_automod_violation(1, 5, 'auto', now)
        assert db.count_recent_escalation_warnings(1, 5, now - 60) == 2
        db.set_escalation_reset(1, 5, now)
        assert db.count_recent_escalation_warnings(1, 5, now) == 2
        db.add_warn(1, 5, 10, 'after', now + 2)
        assert db.count_recent_escalation_warnings(1, 5, now + 1) == 1
    finally:
        close_db(path, db)


def test_gif_allowlist_and_blocklist_are_independent():
    path, db = make_db()
    try:
        db.add_automod_gif_allowlist(1, 'https://tenor.com/view/example-gif-123')
        db.add_automod_gif_blocklist(1, 'bad.gif')
        assert db.automod_gif_list_matches(1, ['https://tenor.com/view/example-gif-123'])[1] is True
        assert db.automod_gif_list_matches(1, ['bad.gif'])[0] is True
    finally:
        close_db(path, db)


def test_votekick_votes_persist_and_upsert():
    path, db = make_db()
    try:
        db.set_votekick_config(1, True, 3, 600)
        vote_id = db.create_votekick(1, 10, 20, 30, 'reason', 100, 700)
        assert vote_id is not None
        assert db.cast_votekick_vote(vote_id, 40, 'yes') == (True, 1, 0)
        assert db.cast_votekick_vote(vote_id, 40, 'no') == (True, 0, 1)
        assert db.get_votekick(vote_id)['status'] == 'open'
    finally:
        close_db(path, db)


def test_ai_index_settings_and_search_are_persistent():
    path, a = make_db(); b = Db(path)
    try:
        a.set_ai_config(1, enabled=True, provider="ollama", base_url="http://127.0.0.1:11434/v1",
                        api_key="", model="llama3", system_prompt="test", max_tokens=500, temperature=.2,
                        index_channels=True, index_message_limit=100)
        assert b.get_ai_config(1)["index_channels"] is True
        a.ai_index_message(1, 10, 1001, 50, "Alex", "we should add a ticket command", 100)
        a.ai_index_message(1, 10, 1002, 51, "Sam", "the ticket command should use a staff role", 101)
        hits = b.ai_search_messages(1, "ticket staff role", 10)
        assert len(hits) == 2
        assert b.ai_indexed_count(1) == 2
    finally:
        close_db(path, a, b)

def test_new_ai_database_is_created_automatically():
    path = tempfile.mktemp(suffix='.db')
    assert not os.path.exists(path)
    db = Db(path)
    try:
        assert os.path.exists(path)
        assert db.get_ai_config(99)["index_channels"] is False
    finally:
        close_db(path, db)
