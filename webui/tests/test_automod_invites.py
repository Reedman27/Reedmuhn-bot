"""Dashboard routes for the AutoMod invite-link controls."""
from __future__ import annotations

import re

from conftest import TEST_GUILD_ID

TOKEN_RE = re.compile(r'name="_csrf_token" value="([A-Za-z0-9_-]+)"')
BASE = f"/guild/{TEST_GUILD_ID}/automod"


def _token(client) -> str:
    page = client.get(BASE)
    assert page.status_code == 200
    return TOKEN_RE.search(page.text).group(1)


def _post(client, path, **data):
    return client.post(BASE + path, data={"_csrf_token": _token(client), **data})


def test_page_shows_invite_controls(authed_client):
    page = authed_client.get(BASE)
    assert "Allowed invites and servers" in page.text
    assert "Channels where invites are allowed" in page.text


def test_settings_roundtrip(authed_client, app_module):
    resp = _post(authed_client, "/invites/settings", invite_mode="delete", allow_own="on")
    assert resp.status_code == 303
    cfg = app_module.db.get_automod_config(TEST_GUILD_ID)
    assert cfg["invite_mode"] == "delete" and cfg["invite_allow_own"] is True
    _post(authed_client, "/invites/settings", invite_mode="strike")
    cfg = app_module.db.get_automod_config(TEST_GUILD_ID)
    assert cfg["invite_mode"] == "strike" and cfg["invite_allow_own"] is False


def test_bad_mode_is_rejected(authed_client, app_module):
    app_module.db.set_automod_invite_mode(TEST_GUILD_ID, "log")
    resp = _post(authed_client, "/invites/settings", invite_mode="nuke")
    assert resp.status_code == 303 and "error=invitemode" in resp.headers["location"]
    assert app_module.db.get_automod_config(TEST_GUILD_ID)["invite_mode"] == "log"


def test_allowlist_add_and_delete(authed_client, app_module):
    resp = _post(authed_client, "/invites/allow/add", entry="discord.gg/partner1")
    assert resp.status_code == 303
    assert ("code", "partner1") in app_module.db.list_automod_invite_allow(TEST_GUILD_ID)
    assert "partner1" in authed_client.get(BASE).text
    _post(authed_client, "/invites/allow/delete", kind="code", value="partner1")
    assert ("code", "partner1") not in app_module.db.list_automod_invite_allow(TEST_GUILD_ID)


def test_allowlist_rejects_junk(authed_client):
    resp = _post(authed_client, "/invites/allow/add", entry="not a code!")
    assert resp.status_code == 303 and "error=inviteallow" in resp.headers["location"]


def test_exempt_channel_must_be_a_real_text_channel(authed_client, app_module):
    resp = _post(authed_client, "/invites/exempt-channels/add", channel_id="999")
    assert resp.status_code == 303 and "error=channel" in resp.headers["location"]
    assert app_module.db.list_automod_invite_exempt_channels(TEST_GUILD_ID) == []


def test_exempt_channel_add_and_delete(authed_client, app_module):
    app_module.db.conn.execute(
        "INSERT OR REPLACE INTO bot_channels (guild_id, channel_id, name, type, position) VALUES (?, ?, ?, ?, ?)",
        (TEST_GUILD_ID, 4242, "advertising", "text", 1),
    )
    app_module.db.conn.commit()
    _post(authed_client, "/invites/exempt-channels/add", channel_id="4242")
    assert app_module.db.list_automod_invite_exempt_channels(TEST_GUILD_ID) == [4242]
    _post(authed_client, "/invites/exempt-channels/delete", channel_id="4242")
    assert app_module.db.list_automod_invite_exempt_channels(TEST_GUILD_ID) == []


def test_new_routes_require_login(anon_client):
    for path in ("/invites/settings", "/invites/allow/add", "/invites/allow/delete",
                 "/invites/exempt-channels/add", "/invites/exempt-channels/delete"):
        resp = anon_client.post(BASE + path, data={})
        assert resp.status_code in (303, 302, 403), (path, resp.status_code)
        assert "/login" in resp.headers.get("location", "") or resp.status_code == 403
