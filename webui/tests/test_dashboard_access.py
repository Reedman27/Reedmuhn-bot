"""Dashboard Access: who may open a server's dashboard, and how much of it.

Builds on the fake-Discord fixtures from test_oauth_multiguild. Users there:
admin (9001) owns A; modA (9002) has Manage Server in A; random (9005) is just
a member of A.
"""
from __future__ import annotations

import pytest

import conftest  # noqa: F401
from test_oauth_multiguild import GUILD_A, GUILD_B, csrf, discord, login  # noqa: F401

RANDOM_ID, MOD_ID = "9005", "9002"


@pytest.fixture(autouse=True)
def clean_access(app_module):
    def wipe():
        app_module.db.conn.execute("DELETE FROM dashboard_access")
        app_module.db.conn.execute("DELETE FROM dashboard_guild_settings")
        app_module.db.conn.commit()
        app_module._guild_cache.clear()
    wipe()
    # Names to look people up by (the bot's member cache) - the typed-name path.
    app_module.db.upsert_bot_member(GUILD_A, int(RANDOM_ID), "random", "Random Person", "online")
    yield
    wipe()


def grant(app_module, user_id, level, guild_id=GUILD_A):
    app_module.db.set_dashboard_access(guild_id, user_id, level, "test")
    app_module._guild_cache.clear()


def test_default_behaviour_is_unchanged(discord, app_module):
    assert login(app_module, "random").get(f"/guild/{GUILD_A}").status_code == 303
    assert login(app_module, "modA").get(f"/guild/{GUILD_A}").status_code == 200


def test_staff_gets_a_trimmed_dashboard(discord, app_module):
    grant(app_module, RANDOM_ID, "staff")
    client = login(app_module, "random")
    home = client.get(f"/guild/{GUILD_A}")
    assert home.status_code == 200
    assert f"/guild/{GUILD_A}/reports" in home.text and f"/guild/{GUILD_A}/quickstart" in home.text
    for hidden in ("automod", "emergency", "antinuke", "access", "youtube", "permissions"):
        assert f"/guild/{GUILD_A}/{hidden}\"" not in home.text, hidden
    assert client.get(f"/guild/{GUILD_A}/quickstart").status_code == 200


@pytest.mark.parametrize("path", ["automod", "emergency", "access", "permissions", "ai"])
def test_staff_cannot_open_settings_pages(discord, app_module, path):
    grant(app_module, RANDOM_ID, "staff")
    resp = login(app_module, "random").get(f"/guild/{GUILD_A}/{path}")
    assert resp.status_code == 303 and resp.headers["location"].endswith("?error=staff_only")


def test_staff_cannot_post_to_config_routes_but_can_use_moderation_actions(discord, app_module):
    grant(app_module, RANDOM_ID, "staff")
    client = login(app_module, "random")
    token = csrf(client)
    blocked = client.post(f"/guild/{GUILD_A}/custom-commands/add",
                          data={"trigger": "nope", "response": "x", "_csrf_token": token})
    assert blocked.headers["location"].endswith("?error=staff_only")
    assert not any(c[0] == "nope" for c in app_module.db.list_custom_commands(GUILD_A))
    blocked = client.post(f"/guild/{GUILD_A}/access/add", data={"member": "9002", "level": "full", "_csrf_token": token})
    assert blocked.headers["location"].endswith("?error=staff_only")
    assert app_module.db.get_dashboard_access_level(GUILD_A, MOD_ID) is None
    allowed = client.post(f"/guild/{GUILD_A}/reports/dismiss", data={"report_id": "1", "_csrf_token": token})
    assert not allowed.headers.get("location", "").endswith("staff_only")


def test_staff_grant_in_one_server_does_not_leak_to_another(discord, app_module):
    grant(app_module, RANDOM_ID, "staff", GUILD_A)
    client = login(app_module, "random")
    assert client.get(f"/guild/{GUILD_B}").headers["location"] == "/?error=no_permission"


def test_full_access_sees_everything_but_cannot_manage_access(discord, app_module):
    grant(app_module, RANDOM_ID, "full")
    client = login(app_module, "random")
    home = client.get(f"/guild/{GUILD_A}")
    assert home.status_code == 200 and f"/guild/{GUILD_A}/automod\"" in home.text
    assert f"/guild/{GUILD_A}/access\"" not in home.text
    assert client.get(f"/guild/{GUILD_A}/access").status_code == 403
    denied = client.post(f"/guild/{GUILD_A}/access/add", data={"member": MOD_ID, "level": "full", "_csrf_token": csrf(client)})
    assert denied.status_code == 403
    assert app_module.db.get_dashboard_access_level(GUILD_A, MOD_ID) is None


def test_owner_can_grant_and_revoke_and_it_applies_immediately(discord, app_module):
    owner = login(app_module, "admin")
    other = login(app_module, "random")
    assert other.get(f"/guild/{GUILD_A}").status_code == 303
    token = csrf(owner)
    added = owner.post(f"/guild/{GUILD_A}/access/add", data={"member": "random", "level": "staff", "_csrf_token": token})
    assert added.headers["location"] == f"/guild/{GUILD_A}/access"
    assert other.get(f"/guild/{GUILD_A}").status_code == 200
    assert RANDOM_ID in owner.get(f"/guild/{GUILD_A}/access").text
    owner.post(f"/guild/{GUILD_A}/access/remove", data={"user_id": RANDOM_ID, "_csrf_token": token})
    assert other.get(f"/guild/{GUILD_A}").status_code == 303


def test_manager_can_grant_by_default(discord, app_module):
    mod = login(app_module, "modA")
    resp = mod.post(f"/guild/{GUILD_A}/access/add", data={"member": "Random Person", "level": "staff", "_csrf_token": csrf(mod)})
    assert resp.headers["location"] == f"/guild/{GUILD_A}/access"
    assert app_module.db.get_dashboard_access_level(GUILD_A, RANDOM_ID) == "staff"


def test_owner_only_mode_locks_out_managers_not_the_owner_or_listed_people(discord, app_module):
    owner = login(app_module, "admin")
    owner.post(f"/guild/{GUILD_A}/access/managers", data={"allowed": "0", "_csrf_token": csrf(owner)})
    assert app_module.db.get_dashboard_managers_allowed(GUILD_A) is False
    assert owner.get(f"/guild/{GUILD_A}").status_code == 200
    assert login(app_module, "modA").get(f"/guild/{GUILD_A}").headers["location"] == "/?error=no_permission"
    grant(app_module, MOD_ID, "staff")
    assert login(app_module, "modA").get(f"/guild/{GUILD_A}").status_code == 200


def test_bad_member_input_is_a_clean_error(discord, app_module):
    owner = login(app_module, "admin")
    resp = owner.post(f"/guild/{GUILD_A}/access/add", data={"member": "nobody-here", "level": "staff", "_csrf_token": csrf(owner)})
    assert resp.headers["location"].endswith("/access?error=member")
    resp = owner.post(f"/guild/{GUILD_A}/access/add", data={"member": RANDOM_ID, "level": "root", "_csrf_token": csrf(owner)})
    assert resp.headers["location"].endswith("/access?error=level")


def test_a_pasted_discord_id_is_accepted_even_if_the_person_is_not_cached(discord, app_module):
    owner = login(app_module, "admin")
    resp = owner.post(f"/guild/{GUILD_A}/access/add", data={"member": "123456789012345678", "level": "staff", "_csrf_token": csrf(owner)})
    assert resp.headers["location"] == f"/guild/{GUILD_A}/access"
    assert app_module.db.get_dashboard_access_level(GUILD_A, "123456789012345678") == "staff"
    short = owner.post(f"/guild/{GUILD_A}/access/add", data={"member": "12345", "level": "staff", "_csrf_token": csrf(owner)})
    assert short.headers["location"].endswith("?error=member")


def test_listing_an_administrator_as_staff_keeps_them_to_the_staff_view(discord, app_module):
    grant(app_module, MOD_ID, "staff")  # modA has Manage Server
    client = login(app_module, "modA")
    assert client.get(f"/guild/{GUILD_A}/automod").headers["location"].endswith("staff_only")
    assert client.get(f"/guild/{GUILD_A}/access").headers["location"].endswith("staff_only")


def test_staff_do_not_see_what_full_users_changed_in_the_dashboard_log(discord, app_module):
    app_module.db.record_webui_action(GUILD_A, "1.2.3.4", "POST", f"/guild/{GUILD_A}/automod/secret-setting", 303, "9001", "admin")
    grant(app_module, RANDOM_ID, "staff")
    assert "secret-setting" not in login(app_module, "random").get("/dashboard-log").text
    assert "secret-setting" in login(app_module, "admin").get("/dashboard-log").text
