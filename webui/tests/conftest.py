"""Shared setup for the WebUI security tests.

Everything runs against a throwaway SQLite database and a throwaway password.
The real ``data/`` directory, ``.env`` and secret key are never read or written.

The environment must be configured *before* ``main`` is imported, because the
module reads ``DB_PATH``/``WEBUI_PASSWORD`` at import time and opens the
database immediately.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

WEBUI_DIR = Path(__file__).resolve().parents[1]
TEST_PASSWORD = "unit-test-only-password-not-for-use"
TEST_GUILD_ID = 111222333444555666

_TEST_DATA = tempfile.TemporaryDirectory(prefix="reedmuhn-webui-tests-")
os.environ["WEBUI_PASSWORD"] = TEST_PASSWORD
os.environ["DB_PATH"] = str(Path(_TEST_DATA.name) / "bot.db")
os.environ.pop("REDIS_URL", None)
os.environ.pop("WEBUI_HTTPS_ONLY", None)
sys.path.insert(0, str(WEBUI_DIR))
os.chdir(WEBUI_DIR)

import main  # noqa: E402  (environment and cwd must be set first)

main.db.conn.execute(
    "INSERT OR IGNORE INTO bot_guilds (guild_id, name) VALUES (?, ?)",
    (TEST_GUILD_ID, "Test Guild"),
)
main.db.conn.commit()


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    main._failed_attempts.clear()
    yield
    main._failed_attempts.clear()


@pytest.fixture()
def app_module():
    return main


@pytest.fixture()
def anon_client():
    from fastapi.testclient import TestClient

    return TestClient(main.app, follow_redirects=False)


@pytest.fixture()
def authed_client(anon_client):
    """A client that has completed the real login flow (token + password)."""
    import re

    page = anon_client.get("/login")
    token = re.search(r'name="_csrf_token" value="([A-Za-z0-9_-]+)"', page.text).group(1)
    resp = anon_client.post("/login", data={"password": TEST_PASSWORD, "_csrf_token": token})
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    return anon_client
