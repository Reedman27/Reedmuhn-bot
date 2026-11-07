"""Temp voice: creator permissions are capped, one room per person, a server
cap, temporary roles, and logging."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest

from cogs.utility import tempvoice
from cogs.utility.tempvoice import TempVoice, build_owner_overwrite, channel_name_for, owner_has, owner_perm_names
from core.db import TEMP_VOICE_DEFAULT_OWNER_PERMS, TEMP_VOICE_OWNER_MASK, Db

GUILD = 10
ADMINISTRATOR = 1 << 3
MANAGE_GUILD = 1 << 5
KICK = 1 << 1


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "bot.db"))


# ---------------------------------------------------------- permission ceiling

def test_owner_perms_are_clamped_to_the_allowed_list():
    everything = (1 << 53) - 1  # every permission bit, including Administrator
    names = owner_perm_names(everything)
    assert set(names) == set(tempvoice.TEMP_VOICE_OWNER_PERMS)
    overwrite = build_owner_overwrite(ADMINISTRATOR | MANAGE_GUILD | KICK)
    assert overwrite.is_empty()


def test_db_never_stores_or_returns_server_wide_powers(db):
    db.set_temp_voice_settings(
        GUILD, owner_perms=ADMINISTRATOR | MANAGE_GUILD | (1 << 4), create_role=True, role_hoist=True,
        log_channel_id=0, name_template="", max_channels=25,
    )
    assert db.get_temp_voice_settings(GUILD)["owner_perms"] == 1 << 4
    # a hand-edited row is clamped on the way out too
    db.conn.execute("UPDATE temp_voice_settings SET owner_perms = ? WHERE guild_id = ?", (ADMINISTRATOR | (1 << 24), GUILD))
    db.conn.commit()
    assert db.get_temp_voice_settings(GUILD)["owner_perms"] == 1 << 24


def test_defaults_match_old_behaviour(db):
    settings = db.get_temp_voice_settings(GUILD)
    assert settings["owner_perms"] == TEMP_VOICE_DEFAULT_OWNER_PERMS
    assert set(owner_perm_names(settings["owner_perms"])) == {
        "manage_channels", "mute_members", "deafen_members", "move_members",
    }
    assert settings["create_role"] is False


def test_max_channels_is_bounded(db):
    db.set_temp_voice_settings(GUILD, owner_perms=0, create_role=False, role_hoist=False,
                               log_channel_id=0, name_template="", max_channels=10_000)
    assert db.get_temp_voice_settings(GUILD)["max_channels"] == 100
    assert owner_has(TEMP_VOICE_OWNER_MASK, "manage_roles")
    assert not owner_has(0, "manage_roles")


def test_channel_name_template():
    assert channel_name_for("", "Sam") == "Sam's Channel"
    assert channel_name_for("Room of {user}", "Sam") == "Room of Sam"
    assert channel_name_for("Lounge", "Sam") == "Lounge Sam"  # {user} is always present so rooms stay distinguishable
    assert len(channel_name_for("{user}", "x" * 300)) == 100


def test_server_info_counts_bots_and_people(db):
    db.upsert_bot_member(GUILD, 1, "a", "A", "online")
    db.upsert_bot_member(GUILD, 2, "b", "B", "offline", True)
    db.upsert_bot_member(GUILD, 3, "c", "C", "online", True)
    counts = db.get_server_counts(GUILD)
    assert (counts["members"], counts["bots"], counts["humans"]) == (3, 2, 1)
    db.set_guild_info(GUILD, owner_id=5, boost_level=2, boost_count=9, verification_level="high", member_total=40)
    info = db.get_guild_info(GUILD)
    assert info["owner_id"] == 5 and info["boost_count"] == 9 and info["member_total"] == 40


# ------------------------------------------------------------ cog behaviour

class FakeRole:
    def __init__(self, role_id, name, guild):
        self.id, self.name, self._guild = role_id, name, guild
        self.deleted = False

    async def delete(self, reason=None):
        self.deleted = True
        self._guild.roles.pop(self.id, None)


class FakeVoice(discord.VoiceChannel):  # isinstance checks in the cog
    category = None  # shadows discord.py's read-only property

    @property
    def members(self):
        return self._members

    def __init__(self, channel_id, name, guild):
        self.id, self.name, self.guild = channel_id, name, guild
        self._members = []
        self.overwrites_set = {}
        self.deleted = False

    async def set_permissions(self, target, *, overwrite=None, **_):
        self.overwrites_set[target.id] = overwrite

    async def delete(self, reason=None):
        self.deleted = True
        self.guild.channels.pop(self.id, None)


class FakeGuild:
    def __init__(self):
        self.id = GUILD
        self.channels, self.roles = {}, {}
        self._next = 1000
        self.log_messages = []

    def _id(self):
        self._next += 1
        return self._next

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    def get_role(self, role_id):
        return self.roles.get(role_id)

    async def create_voice_channel(self, name, category=None, user_limit=0):
        channel = FakeVoice(self._id(), name, self)
        self.channels[channel.id] = channel
        return channel

    async def create_role(self, *, name, hoist, permissions, reason=None):
        role = FakeRole(self._id(), name, self)
        role.hoist, role.permissions = hoist, permissions
        self.roles[role.id] = role
        return role


class FakeMember:
    def __init__(self, guild, user_id, name="Sam"):
        self.guild, self.id, self.display_name, self.mention = guild, user_id, name, f"<@{user_id}>"
        self.moved_to, self.roles_added = None, []

    async def move_to(self, channel):
        self.moved_to = channel
        channel.members.append(self)

    async def add_roles(self, role, reason=None):
        self.roles_added.append(role)


class FakeLog:
    def __init__(self):
        self.sent = []

    async def send(self, text, **_):
        self.sent.append(text)


@pytest.fixture
def cog(db):
    bot = SimpleNamespace(db=db, register_webui_wake=lambda *a, **k: None)
    TempVoice._delete_request_worker = SimpleNamespace(start=lambda: None, cancel=lambda: None)
    TempVoice._limit_request_worker = SimpleNamespace(start=lambda: None, cancel=lambda: None)
    instance = TempVoice(bot)
    return instance


def run(coro):
    return asyncio.run(coro)


def test_creator_gets_only_the_configured_permissions(cog, db):
    guild = FakeGuild()
    db.add_voice_hub(GUILD, 1, 0)
    db.set_temp_voice_settings(GUILD, owner_perms=(1 << 4) | (1 << 0), create_role=False, role_hoist=False,
                               log_channel_id=0, name_template="", max_channels=5)
    member = FakeMember(guild, 7)
    run(cog._create_temp_channel(member, SimpleNamespace(id=1, category=None)))
    channel = member.moved_to
    overwrite = channel.overwrites_set[7]
    assert overwrite.manage_channels is True and overwrite.create_instant_invite is True
    assert overwrite.move_members is None and overwrite.manage_roles is None
    assert db.find_temp_voice_by_owner(GUILD, 7) == channel.id


def test_one_room_per_person(cog, db):
    guild = FakeGuild()
    hub = SimpleNamespace(id=1, category=None)
    member = FakeMember(guild, 7)
    run(cog._create_temp_channel(member, hub))
    first = member.moved_to
    # simulate a second join well after the cooldown
    cog._last_create.clear()
    again = FakeMember(guild, 7)
    run(cog._create_temp_channel(again, hub))
    assert again.moved_to is first
    assert db.count_temp_voice_channels(GUILD) == 1


def test_server_cap_stops_new_rooms_and_logs(cog, db):
    guild = FakeGuild()
    log = FakeLog()
    guild.channels[500] = log
    log.send = log.send
    db.set_temp_voice_settings(GUILD, owner_perms=0, create_role=False, role_hoist=False,
                               log_channel_id=500, name_template="", max_channels=1)
    hub = SimpleNamespace(id=1, category=None)
    run(cog._create_temp_channel(FakeMember(guild, 1), hub))
    cog._last_create.clear()
    blocked = FakeMember(guild, 2)
    run(cog._create_temp_channel(blocked, hub))
    assert blocked.moved_to is None
    assert db.count_temp_voice_channels(GUILD) == 1
    assert any("limit" in line for line in log.sent)


def test_cooldown_blocks_rapid_recreation(cog, db):
    guild = FakeGuild()
    hub = SimpleNamespace(id=1, category=None)
    member = FakeMember(guild, 7)
    run(cog._create_temp_channel(member, hub))
    run(cog._delete_temp_channel(member.moved_to))
    again = FakeMember(guild, 7)
    run(cog._create_temp_channel(again, hub))  # inside the cooldown window
    assert again.moved_to is None


def test_temp_role_is_created_hoisted_and_removed_with_the_channel(cog, db):
    guild = FakeGuild()
    db.set_temp_voice_settings(GUILD, owner_perms=0, create_role=True, role_hoist=True,
                               log_channel_id=0, name_template="", max_channels=5)
    member = FakeMember(guild, 7, "Sam")
    run(cog._create_temp_channel(member, SimpleNamespace(id=1, category=None)))
    role = member.roles_added[0]
    assert role.hoist is True and role.permissions.value == 0 and role.name == "VC: Sam"
    assert db.get_temp_voice_info(member.moved_to.id)["role_id"] == role.id
    run(cog._delete_temp_channel(member.moved_to))
    assert role.deleted and db.get_temp_voice_info(member.moved_to.id) is None
