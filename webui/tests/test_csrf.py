"""Regression tests for the WebUI session-bound CSRF protection.

Run from the repository root with:
    python -m pip install -r webui/requirements.txt pytest httpx
    python -m pytest webui/tests/test_csrf.py

Tests use a temporary SQLite database and a throwaway password. They do not use
or modify the production data directory.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys
import tempfile

from fastapi.testclient import TestClient

WEBUI_DIR = Path(__file__).resolve().parents[1]
_TEST_DATA = tempfile.TemporaryDirectory(prefix="reedmuhn-csrf-tests-")
os.environ["WEBUI_PASSWORD"] = "unit-test-only-password-not-for-use"
os.environ["DB_PATH"] = str(Path(_TEST_DATA.name) / "bot.db")
sys.path.insert(0, str(WEBUI_DIR))
os.chdir(WEBUI_DIR)

import main  # noqa: E402  (environment and working directory must be set first)

client_factory = lambda: TestClient(main.app, follow_redirects=False)
TOKEN_RE = re.compile(r'name="_csrf_token" value="([A-Za-z0-9_-]+)"')


def token_from_response(response) -> str:
    match = TOKEN_RE.search(response.text)
    assert match, "HTML response did not receive a CSRF form field"
    return match.group(1)


def csrf_token(client: TestClient) -> str:
    response = client.get("/login")
    assert response.status_code == 200
    return token_from_response(response)


def test_login_form_includes_session_bound_csrf_field():
    client = client_factory()
    token = csrf_token(client)
    assert len(token) >= 32


def test_login_rejects_missing_csrf_token_even_without_origin_headers():
    client = client_factory()
    response = client.post("/login", data={"password": "unit-test-only-password-not-for-use"})
    assert response.status_code == 403
    assert "CSRF" in response.text


def test_login_rejects_invalid_csrf_token():
    client = client_factory()
    csrf_token(client)
    response = client.post(
        "/login",
        data={"password": "unit-test-only-password-not-for-use", "_csrf_token": "not-the-token"},
    )
    assert response.status_code == 403


def test_valid_csrf_token_allows_same_session_login_without_origin_headers():
    client = client_factory()
    token = csrf_token(client)
    response = client.post(
        "/login",
        data={"password": "unit-test-only-password-not-for-use", "_csrf_token": token},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_authenticated_action_rejects_missing_token_without_origin_headers():
    client = client_factory()
    token = csrf_token(client)
    login_response = client.post(
        "/login",
        data={"password": "unit-test-only-password-not-for-use", "_csrf_token": token},
    )
    assert login_response.status_code == 303
    response = client.post("/switch-guild", data={})
    assert response.status_code == 403


def test_hostile_origin_is_rejected_even_with_valid_token():
    client = client_factory()
    token = csrf_token(client)
    # Login is token-protected separately to preserve compatibility with
    # reverse-proxy origin settings. This check exercises a protected action.
    login_response = client.post(
        "/login",
        data={"password": "unit-test-only-password-not-for-use", "_csrf_token": token},
    )
    assert login_response.status_code == 303
    # The global base template always contains the POST logout form, even when
    # the temporary test database has no Discord guilds configured.
    home_response = client.get("/dashboard-log")
    assert home_response.status_code == 200
    fresh_token = token_from_response(home_response)
    response = client.post(
        "/switch-guild",
        data={"_csrf_token": fresh_token},
        headers={"Origin": "https://attacker.example"},
    )
    assert response.status_code == 403


def test_get_logout_no_longer_performs_state_change():
    client = client_factory()
    response = client.get("/logout")
    assert response.status_code == 405
