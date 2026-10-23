"""Route-wide security regression tests for the WebUI.

These enumerate the app's real routes instead of hand-picking a few, so a new
route added later is automatically covered:

* every state-changing route rejects missing / forged CSRF tokens
* every non-public route redirects to /login when logged out
* every rendered page puts a CSRF token in every POST form
* logout is POST-only and actually ends the session
* the existing login lockout keeps working with CSRF enabled
"""
from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount

from conftest import TEST_GUILD_ID, TEST_PASSWORD

TOKEN_RE = re.compile(r'name="_csrf_token" value="([A-Za-z0-9_-]+)"')
PARAM_RE = re.compile(r"\{[^}]+\}")
FORM_RE = re.compile(r"<form\b[^>]*>", re.IGNORECASE)
POST_FORM_RE = re.compile(r"<form\b[^>]*\bmethod\s*=\s*[\"']?post\b[^>]*>", re.IGNORECASE)

PUBLIC_GET_PATHS = {"/login"}


def _routes(method: str):
    import main

    out = []
    for route in main.app.routes:
        if isinstance(route, Mount) or not isinstance(route, APIRoute):
            continue
        if method in route.methods:
            out.append(route.path)
    return sorted(out)


def _fill(path: str) -> str:
    """Replace {guild_id} with the seeded guild and any other path parameter
    with a harmless integer so the URL matches the route. {metric} needs a
    real metric name, otherwise the page just redirects."""
    import main

    path = path.replace("{guild_id}", str(TEST_GUILD_ID))
    path = path.replace("{metric}", next(iter(main.ANALYTICS_METRICS)))
    return PARAM_RE.sub("1", path)


def _token(client) -> str:
    page = client.get("/dashboard-log")
    assert page.status_code == 200
    return TOKEN_RE.search(page.text).group(1)


def test_route_inventory_is_what_the_tests_assume():
    posts, gets = _routes("POST"), _routes("GET")
    assert "/login" in posts and "/logout" in posts
    assert len(posts) > 100, "POST route discovery looks broken"
    assert len(gets) > 40, "GET route discovery looks broken"
    # The app only exposes GET and POST; if someone adds PUT/PATCH/DELETE the
    # middleware already covers them, but the inventory should be revisited.
    assert not _routes("PUT") and not _routes("PATCH") and not _routes("DELETE")


@pytest.mark.parametrize("path", _routes("POST"))
def test_every_post_route_rejects_missing_token_when_logged_in(authed_client, path):
    resp = authed_client.post(_fill(path), data={})
    assert resp.status_code == 403, f"{path} accepted a POST with no CSRF token"
    assert "CSRF" in resp.text


@pytest.mark.parametrize("path", _routes("POST"))
def test_every_post_route_rejects_forged_token_when_logged_in(authed_client, path):
    resp = authed_client.post(_fill(path), data={"_csrf_token": "forged-token-value"})
    assert resp.status_code == 403, f"{path} accepted a forged CSRF token"


@pytest.mark.parametrize("path", [p for p in _routes("POST") if p != "/login"])
def test_every_post_route_rejects_logged_out_requests_without_token(anon_client, path):
    resp = anon_client.post(_fill(path), data={})
    assert resp.status_code == 403
    assert resp.headers.get("set-cookie") is None or "session=" not in resp.headers["set-cookie"]


@pytest.mark.parametrize("path", [p for p in _routes("POST") if p != "/login"])
def test_valid_token_is_not_blocked_by_csrf_layer(authed_client, path):
    """With a valid token the request must reach the route (which may still
    return 422/303 for the empty body) - i.e. CSRF isn't a blanket denial."""
    token = _token(authed_client)
    resp = authed_client.post(_fill(path), data={"_csrf_token": token})
    assert not (resp.status_code == 403 and "CSRF" in resp.text), f"{path} blocked a valid token"
    assert resp.status_code < 500, f"{path} crashed on an empty form: {resp.status_code}"


def test_hostile_origin_and_referer_are_rejected_even_with_valid_token(authed_client):
    token = _token(authed_client)
    for header in ("Origin", "Referer"):
        resp = authed_client.post(
            "/switch-guild",
            data={"_csrf_token": token},
            headers={header: "https://attacker.example/page"},
        )
        assert resp.status_code == 403, header


def test_header_token_is_accepted_for_non_form_clients(authed_client):
    token = _token(authed_client)
    resp = authed_client.post("/switch-guild", data={}, headers={"X-CSRF-Token": token})
    assert not (resp.status_code == 403 and "CSRF" in resp.text)


def test_oversized_declared_body_is_rejected_before_parsing(authed_client):
    resp = authed_client.post(
        "/switch-guild", content=b"x", headers={"Content-Length": str(9 * 1024 * 1024)}
    )
    assert resp.status_code in (400, 413)


def _public_or_static(path: str) -> bool:
    return path in PUBLIC_GET_PATHS


@pytest.mark.parametrize("path", [p for p in _routes("GET") if not _public_or_static(p)])
def test_every_protected_get_route_redirects_to_login_when_logged_out(anon_client, path):
    resp = anon_client.get(_fill(path))
    assert resp.status_code == 303, f"{path} did not require auth (got {resp.status_code})"
    assert resp.headers["location"].startswith("/login")


@pytest.mark.parametrize("path", [p for p in _routes("GET") if p not in PUBLIC_GET_PATHS and "{guild_id}" in p])
def test_guild_pages_render_and_every_post_form_carries_a_token(authed_client, path):
    resp = authed_client.get(_fill(path))
    if resp.status_code in (303, 404):
        pytest.skip(f"{path} needs extra seeded data ({resp.status_code})")
    assert resp.status_code == 200, f"{path} -> {resp.status_code}"
    if "text/html" not in resp.headers.get("content-type", ""):
        return
    html = resp.text
    post_forms = POST_FORM_RE.findall(html)
    tokens = TOKEN_RE.findall(html)
    assert len(tokens) >= len(post_forms), (
        f"{path}: {len(post_forms)} POST forms but only {len(tokens)} CSRF fields"
    )
    # Forms must stay well-formed: the token is injected *inside* the form.
    for form in re.finditer(r"<form\b[^>]*>(.*?)</form\s*>", html, re.IGNORECASE | re.DOTALL):
        if POST_FORM_RE.match(form.group(0)):
            assert "_csrf_token" in form.group(1), f"{path}: a POST form has no token"


def test_get_forms_do_not_get_a_token(authed_client):
    """Tokens in GET forms would leak into URLs / history / Referer."""
    for path in _routes("GET"):
        if "{guild_id}" not in path:
            continue
        resp = authed_client.get(_fill(path))
        if resp.status_code != 200 or "text/html" not in resp.headers.get("content-type", ""):
            continue
        for form in re.finditer(r"<form\b[^>]*>(.*?)</form\s*>", resp.text, re.IGNORECASE | re.DOTALL):
            if not POST_FORM_RE.match(form.group(0)):
                assert "_csrf_token" not in form.group(1), f"{path}: token leaked into a GET form"


def test_html_responses_are_not_cacheable(authed_client):
    resp = authed_client.get("/dashboard-log")
    assert "no-store" in resp.headers.get("cache-control", "")


def test_static_assets_are_not_rewritten_or_sessioned(anon_client):
    resp = anon_client.get("/static/theme.js")
    assert resp.status_code == 200
    assert "_csrf_token" not in resp.text
    assert "set-cookie" not in {k.lower() for k in resp.headers}


def test_logout_is_post_only_and_ends_the_session(authed_client):
    assert authed_client.get("/logout").status_code == 405
    # still logged in after the GET
    assert authed_client.get("/dashboard-log").status_code == 200

    token = _token(authed_client)
    resp = authed_client.post("/logout", data={"_csrf_token": token})
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    # session is gone
    assert authed_client.get("/dashboard-log").status_code == 303


def test_cross_site_logout_without_token_is_blocked(authed_client):
    resp = authed_client.post("/logout", data={})
    assert resp.status_code == 403
    assert authed_client.get("/dashboard-log").status_code == 200


def test_login_page_logout_link_is_a_form_not_a_link(authed_client):
    html = authed_client.get("/dashboard-log").text
    assert 'href="/logout"' not in html
    assert re.search(r'<form[^>]*action="/logout"[^>]*method="post"', html, re.IGNORECASE)


def test_wrong_password_with_valid_token_is_rejected_and_logged_out(anon_client):
    token = TOKEN_RE.search(anon_client.get("/login").text).group(1)
    resp = anon_client.post("/login", data={"password": "wrong", "_csrf_token": token})
    assert resp.status_code == 303 and "error=failed" in resp.headers["location"]
    assert anon_client.get("/dashboard-log").status_code == 303


def test_login_lockout_still_applies_with_csrf_enabled(anon_client):
    for _ in range(5):
        token = TOKEN_RE.search(anon_client.get("/login").text).group(1)
        resp = anon_client.post("/login", data={"password": "wrong", "_csrf_token": token})
        assert "error=failed" in resp.headers["location"]
    token = TOKEN_RE.search(anon_client.get("/login").text).group(1)
    resp = anon_client.post("/login", data={"password": TEST_PASSWORD, "_csrf_token": token})
    assert "error=locked" in resp.headers["location"]
    assert anon_client.get("/dashboard-log").status_code == 303


def test_login_rotates_session_and_token(anon_client):
    pre = TOKEN_RE.search(anon_client.get("/login").text).group(1)
    anon_client.post("/login", data={"password": TEST_PASSWORD, "_csrf_token": pre})
    post = _token(anon_client)
    assert pre != post, "CSRF token must be regenerated after login (session.clear())"


def test_unknown_guild_is_not_accessible(authed_client):
    resp = authed_client.get("/guild/999/rules")
    assert resp.status_code == 303 and "not_accessible" in resp.headers["location"]


def test_non_ascii_wrong_password_is_a_normal_failure_not_a_500(anon_client, app_module):
    token = TOKEN_RE.search(anon_client.get("/login").text).group(1)
    resp = anon_client.post("/login", data={"password": "pässwörd✓", "_csrf_token": token})
    assert resp.status_code == 303 and "error=failed" in resp.headers["location"]
    # and it counts toward the lockout like any other failed attempt
    assert len(app_module._failed_attempts.get("testclient", [])) == 1


def test_non_ascii_configured_password_can_log_in(anon_client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "WEBUI_PASSWORD", "pässwörd✓-test-only")
    token = TOKEN_RE.search(anon_client.get("/login").text).group(1)
    resp = anon_client.post("/login", data={"password": "pässwörd✓-test-only", "_csrf_token": token})
    assert resp.status_code == 303 and resp.headers["location"] == "/"


@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), (" yes ", True), ("on", True),
    ("0", False), ("false", False), ("", False), ("   ", False), ("nope", False),
])
def test_https_only_flag_accepts_the_values_the_docs_tell_users_to_set(app_module, monkeypatch, value, expected):
    monkeypatch.setenv("WEBUI_HTTPS_ONLY", value)
    assert app_module._env_flag("WEBUI_HTTPS_ONLY") is expected


def test_https_only_flag_defaults_to_off_when_unset(app_module, monkeypatch):
    monkeypatch.delenv("WEBUI_HTTPS_ONLY", raising=False)
    assert app_module._env_flag("WEBUI_HTTPS_ONLY") is False


def test_session_cookie_is_httponly_samesite_lax_and_expires(authed_client):
    cookie = next(c for c in authed_client.cookies.jar if c.name == "session")
    assert cookie.has_nonstandard_attr("httponly") or cookie.has_nonstandard_attr("HttpOnly")
    import main
    opts = next(m for m in main.app.user_middleware if m.cls.__name__ == "SessionMiddleware").kwargs
    assert opts["same_site"] == "lax" and opts["max_age"] == 12 * 60 * 60


def test_security_headers_survive_the_csrf_html_rewrite(authed_client):
    resp = authed_client.get("/dashboard-log")
    assert resp.status_code == 200 and TOKEN_RE.search(resp.text)  # body was rewritten
    h = resp.headers
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "same-origin"
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert "default-src 'self'" in h["content-security-policy"]
    assert "permissions-policy" in h


def test_dashboard_action_log_still_records_authenticated_posts(authed_client, app_module):
    before = len(app_module.db.list_webui_action_log(1000))
    token = _token(authed_client)
    authed_client.post("/switch-guild", data={"_csrf_token": token})
    after = app_module.db.list_webui_action_log(1000)
    assert len(after) == before + 1
    assert after[0][3] == "POST" and after[0][4] == "/switch-guild"
