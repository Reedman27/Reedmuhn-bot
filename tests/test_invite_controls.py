"""Invite-link controls for AutoMod: action mode, allowlist, exempt channels."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.db import Db

GUILD = 1
OWN_GUILD_ID = GUILD
PARTNER_ID = 777000111222333444


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "bot.db"))


# ---------------------------------------------------------------- database

def test_defaults_keep_old_behavior(db):
    cfg = db.get_automod_config(GUILD)
    assert cfg["block_invites"] is True
    assert cfg["invite_mode"] == "strike"
    assert cfg["invite_allow_own"] is False
    assert db.list_automod_invite_allow(GUILD) == []
    assert db.list_automod_invite_exempt_channels(GUILD) == []


def test_mode_is_validated_and_persisted(db):
    db.set_automod_invite_mode(GUILD, "delete")
    assert db.get_automod_config(GUILD)["invite_mode"] == "delete"
    db.set_automod_invite_mode(GUILD, "log")
    assert db.get_automod_config(GUILD)["invite_mode"] == "log"
    with pytest.raises(ValueError):
        db.set_automod_invite_mode(GUILD, "nuke")
    assert db.get_automod_config(GUILD)["invite_mode"] == "log"


def test_allow_own_toggle(db):
    db.set_automod_invite_allow_own(GUILD, True)
    assert db.get_automod_config(GUILD)["invite_allow_own"] is True
    db.set_automod_invite_allow_own(GUILD, False)
    assert db.get_automod_config(GUILD)["invite_allow_own"] is False


@pytest.mark.parametrize("text, expected", [
    ("discord.gg/abc123", ("code", "abc123")),
    ("https://discord.com/invite/Xy-9", ("code", "Xy-9")),
    ("https://discordapp.com/invite/Hello", ("code", "Hello")),
    ("AbC123", ("code", "AbC123")),  # case is kept: codes are case-sensitive
    (str(PARTNER_ID), ("guild", str(PARTNER_ID))),
])
def test_parse_entry(text, expected):
    assert Db.parse_invite_allow_entry(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "x", "not a code!", "12345", "discord.gg/", "a" * 33])
def test_parse_entry_rejects_junk(text):
    with pytest.raises(ValueError):
        Db.parse_invite_allow_entry(text)


def test_allowlist_add_remove_and_dedupe(db):
    assert db.add_automod_invite_allow(GUILD, "discord.gg/abc123") == ("code", "abc123")
    db.add_automod_invite_allow(GUILD, "abc123")  # same entry again
    db.add_automod_invite_allow(GUILD, str(PARTNER_ID))
    assert db.list_automod_invite_allow(GUILD) == [("code", "abc123"), ("guild", str(PARTNER_ID))]
    assert db.remove_automod_invite_allow(GUILD, "code", "abc123") is True
    assert db.remove_automod_invite_allow(GUILD, "code", "abc123") is False
    assert db.list_automod_invite_allow(GUILD) == [("guild", str(PARTNER_ID))]


def test_allowlist_is_per_guild(db):
    db.add_automod_invite_allow(GUILD, "abc123")
    assert db.list_automod_invite_allow(GUILD + 1) == []


def test_allowlist_cap(db):
    for i in range(db.AUTOMOD_INVITE_ALLOW_LIMIT):
        db.add_automod_invite_allow(GUILD, f"code{i:04d}")
    with pytest.raises(ValueError):
        db.add_automod_invite_allow(GUILD, "onetoomany")
    db.add_automod_invite_allow(GUILD, "code0000")  # existing entry is still fine


def test_exempt_channels(db):
    db.add_automod_invite_exempt_channel(GUILD, 55)
    db.add_automod_invite_exempt_channel(GUILD, 55)
    db.add_automod_invite_exempt_channel(GUILD, 66)
    assert db.list_automod_invite_exempt_channels(GUILD) == [55, 66]
    assert db.remove_automod_invite_exempt_channel(GUILD, 55) is True
    assert db.remove_automod_invite_exempt_channel(GUILD, 55) is False
    assert db.list_automod_invite_exempt_channels(GUILD) == [66]


def test_existing_database_is_upgraded_in_place(tmp_path):
    """A DB made before these columns existed must open and keep its rows."""
    import sqlite3

    path = str(tmp_path / "old.db")
    first = Db(path)
    first.get_automod_config(GUILD)
    first.conn.close()
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE automod_config DROP COLUMN invite_mode")
    conn.execute("ALTER TABLE automod_config DROP COLUMN invite_allow_own")
    conn.commit()
    conn.close()
    upgraded = Db(path)
    cfg = upgraded.get_automod_config(GUILD)
    assert cfg["invite_mode"] == "strike" and cfg["invite_allow_own"] is False


# --------------------------------------------------------------- cog logic

class FakeBot:
    def __init__(self, db, invites=None):
        self.db = db
        self.invites = invites or {}  # code -> guild id
        self.fetch_calls = []

    def get_cog(self, name):
        return None

    async def fetch_invite(self, code, **kwargs):
        import discord

        self.fetch_calls.append(code)
        if code not in self.invites:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown invite")
        return SimpleNamespace(guild=SimpleNamespace(id=self.invites[code]))


def make_cog(db, invites=None):
    pytest.importorskip("discord")
    from collections import defaultdict
    from cogs.moderation.automod import AutoMod
    from core.automod_checks import UserMessageTracker

    cog = object.__new__(AutoMod)
    cog.bot = FakeBot(db, invites)
    cog.trackers = defaultdict(UserMessageTracker)
    cog._invite_cache = {}
    return cog


def make_message(content, channel_id=10, vanity=None):
    deleted = []

    async def delete():
        deleted.append(True)

    return SimpleNamespace(
        content=content,
        author=SimpleNamespace(id=42, bot=False, mention="<@42>"),
        guild=SimpleNamespace(id=GUILD, vanity_url_code=vanity),
        channel=SimpleNamespace(id=channel_id, mention="<#10>"),
        mentions=[],
        attachments=[],
        delete=delete,
        deleted=deleted,
    )


@pytest.mark.asyncio
async def test_no_allow_rules_blocks_everything_without_lookups(db):
    cog = make_cog(db)
    cfg = db.get_automod_config(GUILD)
    blocked = await cog._blocked_invite_codes(make_message("join discord.gg/abc123"), cfg)
    assert blocked == ["abc123"]
    assert cog.bot.fetch_calls == []  # old behavior, no network


@pytest.mark.asyncio
async def test_message_without_invite_is_clean(db):
    cog = make_cog(db)
    assert await cog._blocked_invite_codes(make_message("hello there"), db.get_automod_config(GUILD)) == []


@pytest.mark.asyncio
async def test_exempt_channel_allows_invites(db):
    db.add_automod_invite_exempt_channel(GUILD, 10)
    cog = make_cog(db)
    cfg = db.get_automod_config(GUILD)
    assert await cog._blocked_invite_codes(make_message("discord.gg/abc123", channel_id=10), cfg) == []
    assert await cog._blocked_invite_codes(make_message("discord.gg/abc123", channel_id=11), cfg) == ["abc123"]


@pytest.mark.asyncio
async def test_allowed_code_passes_and_is_case_exact(db):
    db.add_automod_invite_allow(GUILD, "abc123")
    cog = make_cog(db)
    cfg = db.get_automod_config(GUILD)
    assert await cog._blocked_invite_codes(make_message("discord.gg/abc123"), cfg) == []
    assert await cog._blocked_invite_codes(make_message("discord.gg/ABC123"), cfg) == ["ABC123"]


@pytest.mark.asyncio
async def test_partner_server_allowed_by_id(db):
    db.add_automod_invite_allow(GUILD, str(PARTNER_ID))
    cog = make_cog(db, invites={"partner": PARTNER_ID, "other": 999})
    cfg = db.get_automod_config(GUILD)
    assert await cog._blocked_invite_codes(make_message("discord.gg/partner"), cfg) == []
    assert await cog._blocked_invite_codes(make_message("discord.gg/other"), cfg) == ["other"]


@pytest.mark.asyncio
async def test_own_server_invites_allowed_only_when_enabled(db):
    cog = make_cog(db, invites={"mine": OWN_GUILD_ID})
    msg = make_message("discord.gg/mine")
    assert await cog._blocked_invite_codes(msg, db.get_automod_config(GUILD)) == ["mine"]
    db.set_automod_invite_allow_own(GUILD, True)
    assert await cog._blocked_invite_codes(msg, db.get_automod_config(GUILD)) == []


@pytest.mark.asyncio
async def test_own_vanity_needs_no_lookup(db):
    db.set_automod_invite_allow_own(GUILD, True)
    cog = make_cog(db)
    msg = make_message("discord.gg/MyServer", vanity="myserver")
    assert await cog._blocked_invite_codes(msg, db.get_automod_config(GUILD)) == []
    assert cog.bot.fetch_calls == []


@pytest.mark.asyncio
async def test_unresolvable_invite_is_blocked(db):
    db.add_automod_invite_allow(GUILD, str(PARTNER_ID))
    cog = make_cog(db, invites={})
    cfg = db.get_automod_config(GUILD)
    assert await cog._blocked_invite_codes(make_message("discord.gg/dead"), cfg) == ["dead"]


@pytest.mark.asyncio
async def test_invite_lookups_are_cached(db):
    db.add_automod_invite_allow(GUILD, str(PARTNER_ID))
    cog = make_cog(db, invites={"partner": PARTNER_ID})
    cfg = db.get_automod_config(GUILD)
    for _ in range(3):
        await cog._blocked_invite_codes(make_message("discord.gg/partner"), cfg)
    assert cog.bot.fetch_calls == ["partner"]


@pytest.mark.asyncio
async def test_mixed_message_blocked_if_any_invite_is_disallowed(db):
    db.add_automod_invite_allow(GUILD, "good")
    cog = make_cog(db)
    cfg = db.get_automod_config(GUILD)
    msg = make_message("discord.gg/good and also discord.gg/bad")
    assert await cog._blocked_invite_codes(msg, cfg) == ["bad"]


async def run_on_message(cog, message):
    handled = []

    async def fake_handle(msg, reason, cfg, queue_for_review=False):
        handled.append(reason)

    cog._handle_violation = fake_handle
    await cog.on_message(message)
    return handled


@pytest.mark.asyncio
async def test_mode_strike_goes_through_punishment_path(db):
    db.set_automod_enabled(GUILD, True)
    cog = make_cog(db)
    msg = make_message("discord.gg/abc123")
    handled = await run_on_message(cog, msg)
    assert handled == ["posting a Discord invite link"]


@pytest.mark.asyncio
async def test_mode_delete_removes_message_without_strike(db):
    db.set_automod_enabled(GUILD, True)
    db.set_automod_invite_mode(GUILD, "delete")
    cog = make_cog(db)
    msg = make_message("discord.gg/abc123")
    handled = await run_on_message(cog, msg)
    assert handled == []
    assert msg.deleted == [True]


@pytest.mark.asyncio
async def test_mode_log_leaves_message_up(db):
    db.set_automod_enabled(GUILD, True)
    db.set_automod_invite_mode(GUILD, "log")
    cog = make_cog(db)
    msg = make_message("discord.gg/abc123")
    handled = await run_on_message(cog, msg)
    assert handled == []
    assert msg.deleted == []


@pytest.mark.asyncio
async def test_mode_log_still_enforces_other_rules(db):
    db.set_automod_enabled(GUILD, True)
    db.set_automod_invite_mode(GUILD, "log")
    db.set_automod_words(GUILD, ["badword"])
    cog = make_cog(db)
    msg = make_message("discord.gg/abc123 badword")
    handled = await run_on_message(cog, msg)
    assert handled == ["using a banned word"]  # the invite itself was only logged


def test_snapshot_restore_brings_back_invite_settings(db):
    db.set_automod_invite_mode(GUILD, "delete")
    db.set_automod_invite_allow_own(GUILD, True)
    snapshot = db.get_automod_config(GUILD)
    db.set_automod_invite_mode(GUILD, "log")
    db.set_automod_invite_allow_own(GUILD, False)
    db._restore_automod_config(GUILD, snapshot)
    cfg = db.get_automod_config(GUILD)
    assert cfg["invite_mode"] == "delete" and cfg["invite_allow_own"] is True


def test_old_snapshot_without_invite_keys_leaves_them_alone(db):
    db.set_automod_invite_mode(GUILD, "log")
    old_snapshot = {k: v for k, v in db.get_automod_config(GUILD).items()
                    if k not in ("invite_mode", "invite_allow_own")}
    db._restore_automod_config(GUILD, old_snapshot)
    assert db.get_automod_config(GUILD)["invite_mode"] == "log"


@pytest.mark.asyncio
async def test_blocking_off_ignores_invites(db):
    db.set_automod_enabled(GUILD, True)
    db.set_automod_invites(GUILD, False)
    cog = make_cog(db)
    handled = await run_on_message(cog, make_message("discord.gg/abc123"))
    assert handled == []
