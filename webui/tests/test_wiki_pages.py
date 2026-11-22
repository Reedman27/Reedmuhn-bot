"""The dashboard Wiki pages: content integrity, rendering and access."""
from __future__ import annotations

import re
from html import escape

import pytest

import conftest  # noqa: F401
from conftest import TEST_GUILD_ID
from test_oauth_multiguild import GUILD_A, discord, login  # noqa: F401

import wiki_pages
from help_commands import COMMANDS

BASE = f"/guild/{TEST_GUILD_ID}/wiki"
RANDOM_ID = "9005"


# ---- content integrity -------------------------------------------------------


def test_slugs_are_unique_and_url_safe():
    slugs = [t["slug"] for t in wiki_pages.TOPICS]
    assert len(slugs) == len(set(slugs))
    assert all(re.fullmatch(r"[a-z0-9-]+", s) for s in slugs)


def test_every_topic_is_in_a_known_group():
    assert {t["group"] for t in wiki_pages.TOPICS} <= set(wiki_pages.GROUPS)


def test_see_also_points_at_real_topics():
    for t in wiki_pages.TOPICS:
        for slug in t["see_also"]:
            assert slug in wiki_pages.BY_SLUG, f"{t['slug']} -> {slug}"


def test_every_command_prefix_matches_a_real_command():
    for t in wiki_pages.TOPICS:
        for prefix in t["commands"]:
            assert wiki_pages.commands_for([prefix]), f"{t['slug']}: no command starts with {prefix!r}"


def test_every_slash_command_is_documented_somewhere():
    documented = set()
    for t in wiki_pages.TOPICS:
        documented |= {c[0] for c in wiki_pages.commands_for(t["commands"])}
    every = {c[0] for lines in COMMANDS.values() for c in lines}
    assert every - documented == set(), "slash commands with no Wiki topic"


def test_dashboard_pages_named_by_topics_exist(app_module):
    paths = {getattr(r, "path", "") for r in app_module.app.routes}
    for t in wiki_pages.TOPICS:
        page = t["page"]
        if page is None:
            continue
        expected = "/guild/{guild_id}" + (f"/{page}" if page else "")
        assert expected in paths, f"{t['slug']}: no dashboard route {expected}"


def test_wiki_is_in_the_sidebar_and_open_to_staff(app_module):
    assert "wiki" in app_module.STAFF_PAGES
    base = (conftest.WEBUI_DIR / "templates" / "base.html").read_text(encoding="utf-8")
    assert '("wiki", "wiki"' in base


# ---- rendering ---------------------------------------------------------------


def test_index_lists_every_topic(authed_client):
    page = authed_client.get(BASE)
    assert page.status_code == 200
    for t in wiki_pages.TOPICS:
        assert f"?topic={t['slug']}" in page.text, t["slug"]


@pytest.mark.parametrize("slug", [t["slug"] for t in wiki_pages.TOPICS])
def test_every_topic_renders(authed_client, slug):
    page = authed_client.get(f"{BASE}?topic={slug}")
    assert page.status_code == 200
    assert escape(wiki_pages.BY_SLUG[slug]["title"], quote=False) in page.text


def test_unknown_topic_is_a_404(authed_client):
    assert authed_client.get(f"{BASE}?topic=nope").status_code == 404


def test_topic_text_is_escaped_not_treated_as_html(authed_client, monkeypatch):
    monkeypatch.setitem(
        wiki_pages.BY_SLUG,
        "xss-probe",
        wiki_pages.topic(
            "xss-probe", "<script>alert(1)</script>", "Basics", "<img src=x onerror=alert(1)>",
            sections=[wiki_pages.sec("<b>h</b>", ["<i>p</i>"], ["<u>li</u>"])],
        ),
    )
    html = authed_client.get(f"{BASE}?topic=xss-probe").text
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;" in html


def test_commands_come_with_their_requirements(authed_client):
    html = authed_client.get(f"{BASE}?topic=automod").text
    assert "<code>/automod toggle</code>" in html
    assert "Manage Server" in html


def test_warning_window_topic_says_zero_means_never(authed_client):
    html = authed_client.get(f"{BASE}?topic=warnings").text
    assert "never" in html and "604800" in html


# ---- access ------------------------------------------------------------------


def test_staff_can_read_wiki_but_not_the_pages_it_links_to(discord, app_module):
    app_module.db.set_dashboard_access(GUILD_A, RANDOM_ID, "staff", "test")
    app_module._guild_cache.clear()
    try:
        client = login(app_module, "random")
        assert client.get(f"/guild/{GUILD_A}/wiki").status_code == 200
        topic = client.get(f"/guild/{GUILD_A}/wiki?topic=automod")
        assert topic.status_code == 200
        assert "not available with Staff access" in topic.text
        allowed = client.get(f"/guild/{GUILD_A}/wiki?topic=moderation")
        assert f'href="/guild/{GUILD_A}/moderation"' in allowed.text
    finally:
        app_module.db.conn.execute("DELETE FROM dashboard_access")
        app_module.db.conn.commit()
        app_module._guild_cache.clear()


# ---- search ------------------------------------------------------------------


def test_search_finds_wiki_topics(authed_client):
    page = authed_client.get(f"/guild/{TEST_GUILD_ID}/search?q=warning+window")
    assert page.status_code == 200
    assert f"/guild/{TEST_GUILD_ID}/wiki?topic=warnings" in page.text


def test_search_for_wiki_itself_does_not_say_no_results(authed_client):
    page = authed_client.get(f"/guild/{TEST_GUILD_ID}/search?q=antinuke")
    assert "No results for" not in page.text
