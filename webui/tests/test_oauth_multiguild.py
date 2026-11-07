"""Multi-server Discord OAuth2 tests.

Every other WebUI test runs in the local-password mode, where the dashboard
deliberately shows *every* server. These tests switch real OAuth on and put a
fake Discord API behind it, so the access matrix the dashboard promises
(admin sees both servers, a moderator only their own, a random member nothing)
is actually exercised instead of just described.
"""
from __future__ import annotations

import asyncio
import re
import time

import pytest
from fastapi.testclient import TestClient

import conftest  # noqa: F401  (sets the environment before ``main`` is imported)

GUILD_A, GUILD_B, GUILD_C = 2001, 2002, 2003  # C: user manages it but the bot isn't in it
MANAGE_GUILD, ADMINISTRATOR = 0x20, 0x8
PUBLIC_ORIGIN = "https://dash.example.ts.net"
CSRF_RE = re.compile(r'name="_csrf_token" value="([A-Za-z0-9_-]+)"')

USERS = {
    # name: (discord id, guilds as (id, owner, permissions))
    "admin": ("9001", [(GUILD_A, True, 0), (GUILD_B, True, 0)]),
    "modA": ("9002", [(GUILD_A, False, MANAGE_GUILD), (GUILD_B, False, 0x400)]),
    "modB": ("9003", [(GUILD_A, False, 0x400), (GUILD_B, False, MANAGE_GUILD)]),
    "adminperm": ("9004", [(GUILD_A, False, ADMINISTRATOR)]),
    "random": ("9005", [(GUILD_A, False, 0x400), (GUILD_B, False, 0x400)]),
    "elsewhere": ("9006", [(GUILD_C, False, MANAGE_GUILD)]),
}


class FakeDiscord:
    def __init__(self):
        self.users = {k: (uid, list(g)) for k, (uid, g) in USERS.items()}
        self.guild_calls = 0
        self.refresh_calls = 0
        self.revoked: list[str] = []
        self.refresh_status = 200
        self.guilds_status = 200
        self.scope = "identify guilds"

    def user_for_token(self, token):
        name = token.split("-", 1)[1].rsplit("#", 1)[0]
        return name, self.users[name]


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status, self._payload = status, payload

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    world: FakeDiscord = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, data=None, headers=None):
        w = self.world
        if url.endswith("/oauth2/token/revoke"):
            w.revoked.append(data["token"])
            return FakeResponse(200, {})
        if data["grant_type"] == "authorization_code":
            name = data["code"].removeprefix("code-")
            return FakeResponse(200, {
                "access_token": f"at-{name}#0", "refresh_token": f"rt-{name}#0",
                "expires_in": 3600, "scope": w.scope, "token_type": "Bearer",
            })
        w.refresh_calls += 1
        if w.refresh_status != 200:
            return FakeResponse(w.refresh_status, {"error": "invalid_grant"})
        name = data["refresh_token"].split("-", 1)[1].rsplit("#", 1)[0]
        n = w.refresh_calls
        return FakeResponse(200, {
            "access_token": f"at-{name}#{n}", "refresh_token": f"rt-{name}#{n}",
            "expires_in": 3600, "scope": w.scope,
        })

    def get(self, url, headers=None):
        w = self.world
        token = headers["Authorization"].removeprefix("Bearer ")
        name, (uid, guilds) = w.user_for_token(token)
        if url.endswith("/users/@me"):
            return FakeResponse(200, {"id": uid, "username": name, "global_name": name.title()})
        w.guild_calls += 1
        if w.guilds_status != 200:
            return FakeResponse(w.guilds_status, {})
        return FakeResponse(200, [
            {"id": str(gid), "name": f"Guild {gid}", "owner": owner, "permissions": str(perms)}
            for gid, owner, perms in guilds
        ])


@pytest.fixture()
def discord(monkeypatch, app_module):
    world = FakeDiscord()
    FakeSession.world = world
    monkeypatch.setattr(app_module.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(app_module, "OAUTH_CONFIGURED", True)
    monkeypatch.setattr(app_module, "DISCORD_CLIENT_ID", "client-id")
    monkeypatch.setattr(app_module, "DISCORD_CLIENT_SECRET", "client-secret")
    monkeypatch.setattr(app_module, "DISCORD_REDIRECT_URI", f"{PUBLIC_ORIGIN}/auth/discord/callback")
    for gid in (GUILD_A, GUILD_B):
        app_module.db.upsert_bot_guild(gid, f"Guild {gid}")
    app_module._guild_cache.clear()
    yield world
    app_module._guild_cache.clear()


def login(app_module, who):
    client = TestClient(app_module.app, follow_redirects=False)
    start = client.get("/auth/discord")
    assert start.status_code == 303
    state = re.search(r"state=([^&]+)", start.headers["location"]).group(1)
    done = client.get(f"/auth/discord/callback?code=code-{who}&state={state}")
    assert done.status_code == 303 and done.headers["location"] == "/", done.headers
    return client


def csrf(client):
    return CSRF_RE.search(client.get("/dashboard-settings").text).group(1)


def can_open(client, guild_id):
    resp = client.get(f"/guild/{guild_id}")
    if resp.status_code == 200:
        return True
    assert resp.status_code == 303 and resp.headers["location"] == "/?error=no_permission", resp.headers
    return False


EXPECTED = {
    "admin": {GUILD_A: True, GUILD_B: True},
    "modA": {GUILD_A: True, GUILD_B: False},
    "modB": {GUILD_A: False, GUILD_B: True},
    "adminperm": {GUILD_A: True, GUILD_B: False},
    "random": {GUILD_A: False, GUILD_B: False},
    "elsewhere": {GUILD_A: False, GUILD_B: False, GUILD_C: False},
}


@pytest.mark.parametrize("who", sorted(EXPECTED))
def test_access_matrix(discord, app_module, who):
    client = login(app_module, who)
    for guild_id, allowed in EXPECTED[who].items():
        assert can_open(client, guild_id) is allowed, (who, guild_id)


def test_manage_server_in_a_guild_without_the_bot_is_not_enough(discord, app_module):
    client = login(app_module, "elsewhere")
    assert can_open(client, GUILD_C) is False


def test_guild_picker_only_lists_manageable_servers(discord, app_module):
    page = login(app_module, "modA").get("/")
    assert page.status_code == 200
    assert "Guild 2001" in page.text and "Guild 2002" not in page.text
    assert "Guild 2001" not in login(app_module, "random").get("/").text


def test_changing_the_guild_id_in_a_post_cannot_cross_servers(discord, app_module):
    client = login(app_module, "modA")
    token = csrf(client)
    denied = client.post(f"/guild/{GUILD_B}/custom-commands/add",
                         data={"trigger": "pwn", "response": "x", "_csrf_token": token})
    assert denied.status_code == 303 and denied.headers["location"] == "/?error=no_permission"
    assert not any(c[0] == "pwn" for c in app_module.db.list_custom_commands(GUILD_B))
    sel = client.post("/select-guild", data={"guild_id": GUILD_B, "_csrf_token": token})
    assert sel.headers["location"] == "/?error=no_permission"
    ok = client.post(f"/guild/{GUILD_A}/custom-commands/add",
                     data={"trigger": "fine", "response": "x", "_csrf_token": token})
    assert ok.headers["location"] == f"/guild/{GUILD_A}/commands"


def test_guild_list_is_cached_not_fetched_on_every_request(discord, app_module):
    client = login(app_module, "admin")
    for _ in range(5):
        assert can_open(client, GUILD_A)
    assert discord.guild_calls == 1


def test_permission_loss_is_noticed_once_the_cache_expires(discord, app_module, monkeypatch):
    client = login(app_module, "modA")
    assert can_open(client, GUILD_A)
    discord.users["modA"] = ("9002", [(GUILD_A, False, 0x400)])  # demoted in Discord
    app_module._guild_cache.clear()
    assert can_open(client, GUILD_A) is False


def test_rate_limited_discord_does_not_lock_out_a_cached_session(discord, app_module):
    client = login(app_module, "modA")
    assert can_open(client, GUILD_A)
    discord.guilds_status = 429
    cached_at, guilds = next(iter(app_module._guild_cache.values()))
    key = next(iter(app_module._guild_cache))
    app_module._guild_cache[key] = (cached_at - app_module.GUILD_CACHE_TTL - 1, guilds)  # expired, but within grace
    assert can_open(client, GUILD_A)


def test_rate_limited_discord_without_a_cache_fails_closed(discord, app_module):
    client = login(app_module, "modA")
    app_module._guild_cache.clear()
    discord.guilds_status = 429
    assert can_open(client, GUILD_A) is False


def test_revoked_discord_grant_ends_the_session(discord, app_module):
    client = login(app_module, "modA")
    app_module._guild_cache.clear()
    row = app_module.db.conn.execute("SELECT session_id FROM webui_oauth_tokens WHERE user_id='9002' ORDER BY rowid DESC LIMIT 1").fetchone()
    app_module.db.conn.execute("UPDATE webui_oauth_tokens SET expires_at=? WHERE session_id=?", (int(time.time()) - 5, row[0]))
    app_module.db.conn.commit()
    discord.refresh_status = 400
    assert client.get(f"/guild/{GUILD_A}").status_code == 303
    assert client.get("/").headers["location"] == "/login"
    assert not app_module.db.is_webui_session_valid(row[0])
    assert app_module.db.get_webui_oauth_token(row[0]) is None


def test_concurrent_requests_refresh_the_token_only_once(discord, app_module):
    import httpx

    client = login(app_module, "admin")
    app_module._guild_cache.clear()
    row = app_module.db.conn.execute("SELECT session_id FROM webui_oauth_tokens WHERE user_id='9001' ORDER BY rowid DESC LIMIT 1").fetchone()
    app_module.db.conn.execute("UPDATE webui_oauth_tokens SET expires_at=? WHERE session_id=?", (int(time.time()) - 5, row[0]))
    app_module.db.conn.commit()
    cookie = client.cookies.get("session")

    async def hammer():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     cookies={"session": cookie}) as c:
            return await asyncio.gather(*[c.get(f"/guild/{GUILD_A}/rules") for _ in range(6)])

    results = asyncio.run(hammer())
    assert [r.status_code for r in results] == [200] * 6
    assert discord.refresh_calls == 1


def test_logout_revokes_tokens_at_discord_and_locally(discord, app_module):
    client = login(app_module, "modA")
    sid = app_module.db.conn.execute("SELECT session_id FROM webui_oauth_tokens WHERE user_id='9002' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert client.post("/logout", data={"_csrf_token": csrf(client)}).status_code == 303
    assert discord.revoked and all(t.startswith(("at-modA", "rt-modA")) for t in discord.revoked)
    assert app_module.db.get_webui_oauth_token(sid) is None


def test_expired_sessions_do_not_leave_oauth_tokens_behind(app_module):
    db = app_module.db
    db.create_webui_session("old-session", 1)
    db.save_webui_oauth_token("old-session", "1", "x", "at", "rt", int(time.time()) + 100)
    db.conn.execute("UPDATE webui_sessions SET expires_at = 1 WHERE session_id='old-session'")
    db.conn.commit()
    db.create_webui_session("new-session", 60)
    assert db.get_webui_oauth_token("old-session") is None


def test_dashboard_log_is_scoped_to_the_viewer(discord, app_module):
    db = app_module.db
    db.conn.execute("DELETE FROM webui_action_log")
    db.conn.execute("DELETE FROM webui_login_events")
    db.conn.commit()
    db.record_webui_action(GUILD_B, "203.0.113.9", "POST", f"/guild/{GUILD_B}/secret-b", 303, "9003", "ModB")
    db.record_webui_action(GUILD_A, "203.0.113.7", "POST", f"/guild/{GUILD_A}/by-someone-else", 303, "9004", "AdminPerm")
    db.record_webui_login("203.0.113.9", True, "9003", "ModB")
    page = login(app_module, "modA").get("/dashboard-log")
    assert page.status_code == 200
    assert "secret-b" not in page.text            # a server modA can't manage
    assert "203.0.113.9" not in page.text         # someone else's login IP
    assert "by-someone-else" in page.text         # same server: visible ...
    assert "203.0.113.7" not in page.text         # ... but that admin's IP is hidden
    assert "hidden" in page.text


def test_random_user_sees_an_empty_dashboard_log(discord, app_module):
    db = app_module.db
    db.record_webui_action(GUILD_A, "198.51.100.5", "POST", f"/guild/{GUILD_A}/x-marker", 303, "9001", "Admin")
    page = login(app_module, "random").get("/dashboard-log")
    assert "x-marker" not in page.text and "198.51.100.5" not in page.text


def test_post_from_the_public_https_origin_is_accepted_behind_a_proxy(discord, app_module):
    client = login(app_module, "admin")
    token = csrf(client)
    good = client.post("/switch-guild", data={"_csrf_token": token}, headers={"Origin": PUBLIC_ORIGIN})
    assert good.status_code == 303
    evil = client.post("/switch-guild", data={"_csrf_token": token}, headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403


def test_non_ascii_state_is_a_clean_failure_not_a_500(discord, app_module):
    client = TestClient(app_module.app, follow_redirects=False)
    client.get("/auth/discord")
    resp = client.get("/auth/discord/callback?code=code-admin&state=%C3%A9%C3%A9")
    assert resp.status_code == 303 and resp.headers["location"] == "/login?error=oauth"


def test_login_requires_the_guilds_scope(discord, app_module):
    discord.scope = "identify"
    client = TestClient(app_module.app, follow_redirects=False)
    start = client.get("/auth/discord")
    state = re.search(r"state=([^&]+)", start.headers["location"]).group(1)
    resp = client.get(f"/auth/discord/callback?code=code-admin&state={state}")
    assert resp.headers["location"] == "/login?error=oauth"
    assert client.get("/").headers["location"] == "/login"
