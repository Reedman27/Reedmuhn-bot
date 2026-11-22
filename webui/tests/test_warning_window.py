"""Dashboard route for the AutoMod warning window (0 = never expire)."""
from __future__ import annotations

import re

from conftest import TEST_GUILD_ID

TOKEN_RE = re.compile(r'name="_csrf_token" value="([A-Za-z0-9_-]+)"')
BASE = f"/guild/{TEST_GUILD_ID}/automod"


def _save(client, value):
    token = TOKEN_RE.search(client.get(BASE).text).group(1)
    return client.post(BASE + "/escalation", data={"_csrf_token": token, "window_seconds": value})


def test_zero_means_never_expire_and_is_accepted(authed_client, app_module):
    app_module.db.set_automod_violation_window(TEST_GUILD_ID, 3600)
    resp = _save(authed_client, 0)
    assert resp.status_code == 303 and "error" not in resp.headers["location"]
    assert app_module.db.get_automod_config(TEST_GUILD_ID)["violation_window_seconds"] == 0


def test_positive_window_still_saves(authed_client, app_module):
    resp = _save(authed_client, 7200)
    assert resp.status_code == 303 and "error" not in resp.headers["location"]
    assert app_module.db.get_automod_config(TEST_GUILD_ID)["violation_window_seconds"] == 7200


def test_out_of_range_values_are_rejected(authed_client, app_module):
    app_module.db.set_automod_violation_window(TEST_GUILD_ID, 0)
    for bad in (-1, 604801):
        resp = _save(authed_client, bad)
        assert resp.status_code == 303 and "error=invalid" in resp.headers["location"]
    assert app_module.db.get_automod_config(TEST_GUILD_ID)["violation_window_seconds"] == 0


def test_page_explains_zero_and_shows_it_as_the_default(authed_client, app_module):
    app_module.db.set_automod_violation_window(TEST_GUILD_ID, 0)
    page = authed_client.get(BASE).text
    assert "0 = never expire" in page
    assert 'name="window_seconds" min="0"' in page
