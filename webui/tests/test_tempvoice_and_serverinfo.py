"""Dashboard side of temp-voice creator permissions and the Overview server info."""
from __future__ import annotations

import pytest

import conftest  # noqa: F401
from test_oauth_multiguild import GUILD_A, csrf, discord, login  # noqa: F401

RANDOM_ID = "9005"
ADMINISTRATOR, MANAGE_GUILD = 1 << 3, 1 << 5


@pytest.fixture(autouse=True)
def clean(app_module):
    def wipe():
        for table in ("temp_voice_settings", "dashboard_access", "dashboard_guild_settings"):
            app_module.db.conn.execute(f"DELETE FROM {table}")
        app_module.db.conn.execute("DELETE FROM bot_channels WHERE guild_id = ?", (GUILD_A,))
        app_module.db.conn.commit()
        app_module._guild_cache.clear()
    wipe()
    app_module.db.conn.execute(
        "INSERT INTO bot_channels (guild_id, channel_id, name, type, position) VALUES (?, 555, 'vc-log', 'text', 0)",
        (GUILD_A,),
    )
    app_module.db.conn.commit()
    yield
    wipe()


def test_settings_save_and_render(discord, app_module):
    client = login(app_module, "admin")
    resp = client.post(f"/guild/{GUILD_A}/tempvoice/settings", data={
        "perm_manage_channels": "on", "perm_create_instant_invite": "on", "create_role": "on",
        "role_hoist": "on", "name_template": "{user}'s Room", "log_channel_id": "555",
        "max_channels": "7", "_csrf_token": csrf(client),
    })
    assert resp.status_code == 303 and resp.headers["location"].endswith("?saved=1")
    saved = app_module.db.get_temp_voice_settings(GUILD_A)
    assert saved["owner_perms"] == (1 << 4) | (1 << 0)
    assert saved["create_role"] and saved["role_hoist"] and saved["log_channel_id"] == 555
    assert saved["name_template"] == "{user}'s Room" and saved["max_channels"] == 7
    page = client.get(f"/guild/{GUILD_A}/tempvoice")
    assert page.status_code == 200 and "Creator permissions" in page.text


def test_server_wide_permission_fields_are_ignored(discord, app_module):
    client = login(app_module, "admin")
    client.post(f"/guild/{GUILD_A}/tempvoice/settings", data={
        "perm_administrator": "on", "perm_manage_guild": "on", "max_channels": "5", "_csrf_token": csrf(client),
    })
    assert app_module.db.get_temp_voice_settings(GUILD_A)["owner_perms"] == 0


@pytest.mark.parametrize("bad", [{"max_channels": "0"}, {"max_channels": "101"}, {"max_channels": "x"},
                                 {"max_channels": "5", "log_channel_id": "999"}])
def test_bad_values_are_rejected(discord, app_module, bad):
    client = login(app_module, "admin")
    resp = client.post(f"/guild/{GUILD_A}/tempvoice/settings", data={**bad, "_csrf_token": csrf(client)})
    assert "error=" in resp.headers["location"]
    assert app_module.db.get_temp_voice_settings(GUILD_A)["max_channels"] == 25  # untouched default


def test_staff_cannot_change_creator_permissions(discord, app_module):
    app_module.db.upsert_bot_member(GUILD_A, int(RANDOM_ID), "random", "Random Person", "online")
    app_module.db.set_dashboard_access(GUILD_A, RANDOM_ID, "staff", "test")
    app_module._guild_cache.clear()
    client = login(app_module, "random")
    resp = client.post(f"/guild/{GUILD_A}/tempvoice/settings", data={
        "perm_manage_roles": "on", "max_channels": "5", "_csrf_token": csrf(client),
    })
    assert resp.headers["location"].endswith("staff_only")
    assert app_module.db.get_temp_voice_settings(GUILD_A)["owner_perms"] != 1 << 28


def test_overview_shows_server_info_and_missing_permissions(discord, app_module):
    db = app_module.db
    db.upsert_bot_member(GUILD_A, 1, "human", "Human", "online", False)
    db.upsert_bot_member(GUILD_A, 2, "robot", "Robot", "online", True)
    db.set_guild_info(GUILD_A, owner_id=1, boost_level=1, boost_count=3, verification_level="medium",
                      member_total=2, bot_permissions=(1 << 4))  # Manage Channels only
    page = login(app_module, "admin").get(f"/guild/{GUILD_A}")
    assert page.status_code == 200
    assert "Server info" in page.text and "Total members" in page.text and "Bots" in page.text
    assert "missing permissions" in page.text and "Manage Roles" in page.text
    assert "Manage Channels," not in page.text  # present, so not flagged


def test_administrator_bot_is_never_flagged(app_module):
    assert app_module.missing_bot_permissions(ADMINISTRATOR) == []
    assert app_module.missing_bot_permissions(0) == []  # not reported yet
