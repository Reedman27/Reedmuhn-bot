"""Help ("manual") pages for the dashboard.

Read-only: one index page and one page per topic, selected with ?topic=.
A query parameter rather than a sub-path keeps these inside the single-segment
page names the Staff access level is allowed to open (see STAFF_PAGES in
main.py), so no change to the access check is needed.
"""
from __future__ import annotations

from fastapi import HTTPException, Request

from main import *  # noqa: F401,F403 - shares app/db/templates/helpers with main.py
from main import STAFF_PAGES  # noqa: F401 - names a bare `import *` doesn't re-export

from help_pages import BY_SLUG, commands_for, grouped_topics


def _can_open(request: Request, page) -> bool:
    if page is None:
        return False
    if getattr(request.state, "guild_level", "full") == "staff":
        return page in STAFF_PAGES
    return True


@app.get("/guild/{guild_id}/help")
async def help_page(request: Request, guild_id: int, topic: str = ""):
    if (r := await require_auth(request)):
        return r
    if not topic:
        return render(request, "help_index.html", guild_id, "help", groups=grouped_topics())
    entry = BY_SLUG.get(topic)
    if entry is None:
        raise HTTPException(status_code=404, detail="No such help topic")
    return render(
        request, "help_topic.html", guild_id, "help",
        topic=entry,
        commands=commands_for(entry["commands"]),
        see_also=[BY_SLUG[slug] for slug in entry["see_also"] if slug in BY_SLUG],
        can_open_page=_can_open(request, entry["page"]),
    )
