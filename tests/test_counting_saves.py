"""Rules for the counting game and its server-wide saves (database layer)."""
from __future__ import annotations

import pytest

from db import Db

GUILD = 1
ALICE, BOB, CARA = 101, 102, 103


@pytest.fixture
def db(tmp_path):
    d = Db(str(tmp_path / "bot.db"))
    d.set_counting_channel(GUILD, 555)
    return d


def count(db, start, end, users=(ALICE, BOB)):
    """Count start..end inclusive, alternating between users."""
    last = None
    for i, n in enumerate(range(start, end + 1)):
        last = db.apply_count_attempt(GUILD, users[i % len(users)], n)
        assert last["result"] == "ok", (n, last)
    return last


def test_no_channel_means_no_game(tmp_path):
    d = Db(str(tmp_path / "bot.db"))
    assert d.apply_count_attempt(GUILD, ALICE, 1) is None
    d.set_high_score_alerts(GUILD, True)  # creates a row with channel 0
    assert d.apply_count_attempt(GUILD, ALICE, 1) is None


def test_basic_counting_and_high_score(db):
    count(db, 1, 5)
    state = db.get_counting(GUILD)
    assert (state["current_number"], state["high_score"]) == (5, 5)


def test_wrong_number_resets_without_saves(db):
    count(db, 1, 5)  # Alice posted the last number
    out = db.apply_count_attempt(GUILD, BOB, 9)
    assert out["result"] == "reset" and out["lost"] == 5 and out["reason"] == "wrong"
    state = db.get_counting(GUILD)
    assert state["current_number"] == 0 and state["high_score"] == 5


def test_same_member_twice_is_a_repeat_even_with_right_number(db):
    db.apply_count_attempt(GUILD, ALICE, 1)
    out = db.apply_count_attempt(GUILD, ALICE, 2)
    assert out["result"] == "reset" and out["reason"] == "repeat"


def test_mistake_at_zero_is_only_a_nudge(db):
    out = db.apply_count_attempt(GUILD, ALICE, 7)
    assert out["result"] == "nudge" and out["expected"] == 1


def test_saves_are_off_by_default_and_earn_nothing(db):
    count(db, 1, 50)
    assert db.get_counting(GUILD)["server_saves"] == 0


def test_server_earns_a_save_at_each_multiple_of_the_interval(db):
    db.set_saves_enabled(GUILD, True)
    out = count(db, 1, 49)
    assert out["earned_save"] is False
    out = db.apply_count_attempt(GUILD, BOB, 50)
    assert out["earned_save"] is True and out["saves"] == 1
    count_out = db.apply_count_attempt(GUILD, ALICE, 51)
    assert count_out["earned_save"] is False
    for n, user in zip(range(52, 101), (BOB, ALICE) * 30):
        out = db.apply_count_attempt(GUILD, user, n)
    assert out["number"] == 100 and out["earned_save"] and out["saves"] == 2


def test_interval_is_editable(db):
    db.set_saves_enabled(GUILD, True)
    db.set_save_settings(GUILD, 20, 3)
    out = count(db, 1, 20)
    assert out["earned_save"] is True and db.get_counting(GUILD)["server_saves"] == 1


def test_a_save_absorbs_a_mistake_for_the_whole_server(db):
    db.set_saves_enabled(GUILD, True)
    db.set_save_settings(GUILD, 10, 3)
    count(db, 1, 10)  # banks one save
    out = db.apply_count_attempt(GUILD, CARA, 99)  # a different member miscounts
    assert out["result"] == "saved" and out["saves_left"] == 0 and out["current"] == 10
    state = db.get_counting(GUILD)
    assert state["current_number"] == 10 and state["server_saves"] == 0
    # the count carries on from where it was
    assert db.apply_count_attempt(GUILD, ALICE, 11)["result"] == "ok"
    # no saves left: the next mistake resets
    assert db.apply_count_attempt(GUILD, BOB, 99)["result"] == "reset"


def test_after_a_reset_the_server_has_to_climb_back_to_earn_another(db):
    db.set_saves_enabled(GUILD, True)
    db.set_save_settings(GUILD, 10, 3)
    count(db, 1, 10)                                      # 1 save banked
    db.apply_count_attempt(GUILD, CARA, 99)               # spend it
    db.apply_count_attempt(GUILD, BOB, 99)                # reset to 0
    assert db.get_counting(GUILD)["current_number"] == 0
    count(db, 1, 9, users=(CARA, ALICE))                  # Cara posted 9
    assert db.get_counting(GUILD)["server_saves"] == 0    # not yet
    out = db.apply_count_attempt(GUILD, ALICE, 10)
    assert out["result"] == "ok" and out["earned_save"] and out["saves"] == 1


def test_bank_is_capped(db):
    db.set_saves_enabled(GUILD, True)
    db.set_save_settings(GUILD, 5, 2)
    count(db, 1, 25)
    assert db.get_counting(GUILD)["server_saves"] == 2


def test_lowering_the_cap_trims_banked_saves(db):
    db.set_saves_enabled(GUILD, True)
    db.set_save_settings(GUILD, 5, 5)
    count(db, 1, 20)
    assert db.get_counting(GUILD)["server_saves"] == 4
    db.set_save_settings(GUILD, 5, 1)
    assert db.get_counting(GUILD)["server_saves"] == 1


def test_turning_saves_off_stops_them_being_spent(db):
    db.set_saves_enabled(GUILD, True)
    db.set_save_settings(GUILD, 5, 3)
    count(db, 1, 5)
    db.set_saves_enabled(GUILD, False)
    assert db.apply_count_attempt(GUILD, CARA, 99)["result"] == "reset"


def test_changing_the_channel_keeps_progress(db):
    db.set_saves_enabled(GUILD, True)
    count(db, 1, 8)
    db.set_counting_channel(GUILD, 777)
    state = db.get_counting(GUILD)
    assert state["channel_id"] == 777 and state["current_number"] == 8 and state["saves_enabled"]


def test_old_database_is_upgraded(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE counting (
            guild_id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL,
            current_number INTEGER NOT NULL DEFAULT 0, last_user_id INTEGER,
            high_score INTEGER NOT NULL DEFAULT 0)"""
    )
    conn.execute("INSERT INTO counting (guild_id, channel_id, current_number, high_score) VALUES (1, 555, 12, 40)")
    conn.commit()
    conn.close()
    d = Db(path)
    state = d.get_counting(1)
    assert state["current_number"] == 12 and state["high_score"] == 40
    assert state["saves_enabled"] is False and state["server_saves"] == 0 and state["save_milestone"] == 50
