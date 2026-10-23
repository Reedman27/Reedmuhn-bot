"""Regression tests for Discord-side failures; skipped without runtime deps."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_closing_already_closed_poll_still_retries_message_update():
    pytest.importorskip("discord")
    from cogs.polls import Polls

    class FakeDB:
        def __init__(self):
            self.closed = True

        def get_poll(self, poll_id):
            return {
                "id": poll_id, "guild_id": 11, "channel_id": 22, "message_id": 33,
                "question": "Q?", "options": ["A", "B"], "created_by": 44,
                "created_at": 1, "ends_at": None, "closed": self.closed,
            }

        def close_poll(self, poll_id):
            self.closed = True

        def poll_results(self, poll_id, num_options):
            return [1, 0]

        def poll_voters(self, poll_id, num_options):
            return {0: [55]}

    class FakeMessage:
        def __init__(self):
            self.edit_kwargs = None

        async def edit(self, **kwargs):
            self.edit_kwargs = kwargs

    class FakeChannel:
        def __init__(self):
            self.message = FakeMessage()

        async def fetch_message(self, message_id):
            assert message_id == 33
            return self.message

    class FakeGuild:
        id = 11

        def __init__(self, channel):
            self.channel = channel

        def get_channel(self, channel_id):
            assert channel_id == 22
            return self.channel

    db = FakeDB()
    channel = FakeChannel()
    fake_self = SimpleNamespace(bot=SimpleNamespace(db=db))
    error = await Polls._close_poll(fake_self, FakeGuild(channel), 1)
    assert error is None
    assert channel.message.edit_kwargs is not None
    assert channel.message.edit_kwargs["view"] is None
    assert channel.message.edit_kwargs["embed"].title.startswith("🔒 ")


@pytest.mark.asyncio
async def test_giveaway_announcement_failure_releases_claim_for_retry():
    pytest.importorskip("discord")
    pytest.importorskip("feedparser")
    from cogs.extras import Extras

    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE extras_giveaways (
            guild_id INTEGER, channel_id INTEGER, message_id INTEGER PRIMARY KEY,
            prize TEXT, winners INTEGER, end_at INTEGER, ended INTEGER
        )"""
    )
    conn.execute(
        "INSERT INTO extras_giveaways VALUES (1, 2, 3, 'Prize', 1, 0, 0)"
    )
    conn.commit()

    class FakeDB:
        def __init__(self, connection):
            self.conn = connection

    class FakeMessage:
        reactions = []

    class FakeChannel:
        async def fetch_message(self, message_id):
            assert message_id == 3
            return FakeMessage()

        async def send(self, *args, **kwargs):
            raise RuntimeError("Discord send failed")

    class FakeGuild:
        def __init__(self, channel):
            self.channel = channel

        def get_channel(self, channel_id):
            assert channel_id == 2
            return self.channel

    channel = FakeChannel()
    bot = SimpleNamespace(get_guild=lambda guild_id: FakeGuild(channel))
    fake_self = SimpleNamespace(bot=bot, db=FakeDB(conn))

    with pytest.raises(RuntimeError, match="Discord send failed"):
        await Extras._end_giveaway(fake_self, 3)
    ended = conn.execute("SELECT ended FROM extras_giveaways WHERE message_id=3").fetchone()[0]
    assert ended == 0
    conn.close()


@pytest.mark.asyncio
async def test_scheduled_poll_close_propagates_discord_edit_failure(monkeypatch):
    discord = pytest.importorskip("discord")
    from cogs.polls import Polls

    class FakeForbidden(Exception):
        pass

    class FakeHTTPException(Exception):
        pass

    monkeypatch.setattr(discord, "Forbidden", FakeForbidden)
    monkeypatch.setattr(discord, "HTTPException", FakeHTTPException)

    class FakeDB:
        def get_poll(self, poll_id):
            return {
                "id": poll_id, "guild_id": 11, "channel_id": 22, "message_id": 33,
                "question": "Q?", "options": ["A", "B"], "created_by": 44,
                "created_at": 1, "ends_at": None, "closed": True,
            }

        def poll_results(self, poll_id, num_options):
            return [1, 0]

        def poll_voters(self, poll_id, num_options):
            return {0: [55]}

    class FakeChannel:
        async def fetch_message(self, message_id):
            raise FakeForbidden("missing permission to edit poll")

    class FakeGuild:
        def get_channel(self, channel_id):
            return FakeChannel()

    fake_self = SimpleNamespace(bot=SimpleNamespace(db=FakeDB()))
    with pytest.raises(FakeForbidden, match="missing permission"):
        await Polls._close_poll(fake_self, FakeGuild(), 1, propagate_http_errors=True)
