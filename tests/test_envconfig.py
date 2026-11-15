"""Optional tuning knobs must never be able to stop the bot from starting."""
import importlib
import logging

import pytest

from core.envconfig import env_choice, env_int


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_uses_the_default(raw):
    environ = {} if raw is None else {"X": raw}
    assert env_int("X", 42, environ=environ) == 42


def test_valid_values_are_used_and_whitespace_is_tolerated():
    assert env_int("X", 1, environ={"X": " 25 "}) == 25
    assert env_int("X", 1, environ={"X": "-3"}) == -3


@pytest.mark.parametrize("raw", ["abc", "1.5", "10s", "0x10", "1,000"])
def test_garbage_falls_back_to_the_default_and_warns(raw, caplog):
    with caplog.at_level(logging.WARNING, logger="envconfig"):
        assert env_int("X", 7, environ={"X": raw}) == 7
    assert "X" in caplog.text and "default" in caplog.text


def test_out_of_range_values_are_clamped_and_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="envconfig"):
        assert env_int("X", 5, minimum=1, maximum=10, environ={"X": "0"}) == 1
        assert env_int("X", 5, minimum=1, maximum=10, environ={"X": "99"}) == 10
    assert caplog.text.count("X") >= 2


def test_in_range_values_pass_through_untouched(caplog):
    with caplog.at_level(logging.WARNING, logger="envconfig"):
        assert env_int("X", 5, minimum=1, maximum=10, environ={"X": "10"}) == 10
        assert env_int("X", 5, minimum=1, maximum=10, environ={"X": "1"}) == 1
    assert not caplog.records


def test_env_choice_is_case_insensitive_and_rejects_unknowns(caplog):
    choices = ("DEBUG", "INFO", "WARNING")
    assert env_choice("L", "INFO", choices, environ={"L": "debug"}) == "DEBUG"
    assert env_choice("L", "INFO", choices, environ={}) == "INFO"
    with caplog.at_level(logging.WARNING, logger="envconfig"):
        assert env_choice("L", "INFO", choices, environ={"L": "loud"}) == "INFO"
    assert "L" in caplog.text and "DEBUG" in caplog.text


# ---- the knobs themselves -------------------------------------------------

def _reload(module_name):
    return importlib.reload(importlib.import_module(module_name))


def test_economy_knobs_default_to_the_previous_hardcoded_values(monkeypatch):
    for name in ("XP_COOLDOWN_SECONDS", "XP_PER_MESSAGE", "DAILY_AMOUNT", "DAILY_STREAK_BONUS",
                 "DAILY_STREAK_BONUS_CAP", "DAILY_STREAK_GRACE_HOURS", "WORK_COOLDOWN_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    extras = _reload("cogs.fun.extras")
    assert (extras.XP_COOLDOWN, extras.XP_PER_MESSAGE, extras.DAILY_AMOUNT) == (60, 10, 250)
    assert (extras.DAILY_STREAK_BONUS, extras.DAILY_STREAK_BONUS_CAP) == (25, 500)
    assert (extras.DAILY_STREAK_GRACE, extras.WORK_COOLDOWN) == (48 * 3600, 3600)


def test_economy_knobs_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("XP_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("XP_PER_MESSAGE", "20")
    monkeypatch.setenv("DAILY_AMOUNT", "1000")
    monkeypatch.setenv("DAILY_STREAK_BONUS", "0")
    monkeypatch.setenv("DAILY_STREAK_GRACE_HOURS", "36")
    monkeypatch.setenv("WORK_COOLDOWN_SECONDS", "1800")
    try:
        extras = _reload("cogs.fun.extras")
        assert extras.XP_COOLDOWN == 30 and extras.XP_PER_MESSAGE == 20
        assert extras.DAILY_AMOUNT == 1000 and extras.DAILY_STREAK_BONUS == 0
        assert extras.DAILY_STREAK_GRACE == 36 * 3600 and extras.WORK_COOLDOWN == 1800
    finally:
        monkeypatch.undo()
        _reload("cogs.fun.extras")


def test_cooldowns_can_never_reach_zero_which_redis_rejects(monkeypatch):
    # SET key 1 NX EX 0 is an error in Redis, so 0 must be clamped, not passed on.
    monkeypatch.setenv("XP_COOLDOWN_SECONDS", "0")
    monkeypatch.setenv("WORK_COOLDOWN_SECONDS", "-5")
    try:
        extras = _reload("cogs.fun.extras")
        assert extras.XP_COOLDOWN >= 1 and extras.WORK_COOLDOWN >= 1
    finally:
        monkeypatch.undo()
        _reload("cogs.fun.extras")


def test_a_typo_in_env_does_not_stop_a_cog_from_loading(monkeypatch):
    # These used to be a bare int(...) that raised ValueError at import time.
    monkeypatch.setenv("FEED_RETENTION_DAYS", "thirty")
    monkeypatch.setenv("FEED_KEEP_PER_CHANNEL", "lots")
    monkeypatch.setenv("YOUTUBE_CHECK_INTERVAL_MINUTES", "often")
    try:
        feed = _reload("cogs.dashboard.channelfeed")
        youtube = _reload("cogs.notifications.youtube")
        assert feed.RETENTION_DAYS == 0 and feed.KEEP_PER_CHANNEL == 300
        assert youtube.CHECK_INTERVAL_MINUTES == 10
    finally:
        monkeypatch.undo()
        _reload("cogs.dashboard.channelfeed")
        _reload("cogs.notifications.youtube")
