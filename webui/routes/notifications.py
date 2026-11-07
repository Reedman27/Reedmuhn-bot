"""Routes for the notifications section of the dashboard (mirrors cogs/notifications/).

Imported by main.py purely for the side effect of registering these routes on
the shared FastAPI `app` object - see the bottom of main.py.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from main import *  # noqa: F401,F403 - shares app/db/templates/helpers with main.py
from main import (  # noqa: F401 - names a bare `import *` doesn't re-export
    _allowed_origins,
    _check_confirm_phrase,
    _client_ip,
    _current_identity,
    _dashboard_level,
    _env_flag,
    _failed_attempts,
    _fetch_guild_access,
    _flatten_snapshot,
    _forget_session,
    _guild_cache,
    _is_local_admin,
    _lifespan,
    _nav_visible,
    _origin_of,
    _parse_days,
    _require_can_grant,
    _revoke_discord_tokens,
    _safe_equal,
    _session_locks,
    _wake,
    _wake_fire_and_forget,
    _wake_tasks
)

@app.get("/guild/{guild_id}/youtube")
async def youtube_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    watches = [
        {
            "yt_channel_id": yt_id,
            "display": channel_name if channel_name else yt_id,
            "announce_channel_id": announce_channel_id,
            "announce_name": channel_label(guild_id, announce_channel_id),
            "role_id": role_id,
            "notify_videos": notify_videos,
            "notify_lives": notify_lives,
            "live_announce_channel_id": live_announce_channel_id,
        }
        for yt_id, announce_channel_id, _last_video_id, channel_name, role_id,
            notify_videos, notify_lives, live_announce_channel_id in db.list_youtube_watches(guild_id)
    ]
    return render(
        request, "youtube.html", guild_id, "youtube", watches=watches,
        text_channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"),
        roles=db.list_bot_roles(guild_id),
    )


@app.post("/guild/{guild_id}/youtube/add")
async def add_youtube_watch(request: Request, guild_id: int, channel: str = Form(...), announce_channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, announce_channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/youtube?error=channel", status_code=303)
    channel = channel.strip()
    if not channel:
        return RedirectResponse(f"/guild/{guild_id}/youtube?error=youtube", status_code=303)

    # Turning a pasted URL/handle into a real channel ID needs an HTTP
    # fetch, and this process doesn't keep a client session around for that
    # - it queues the raw input instead, and the bot (which already polls
    # YouTube on a timer) resolves and stores it on its next scheduler
    # tick. See scheduler._handle_add_youtube_watch.
    db.insert_scheduled_event(
        "add_youtube_watch", guild_id, int(time.time()),
        {"channel": channel, "announce_channel_id": announce_channel_id},
    )
    return RedirectResponse(f"/guild/{guild_id}/youtube?queued=1", status_code=303)


@app.post("/guild/{guild_id}/youtube/delete")
async def delete_youtube_watch(request: Request, guild_id: int, yt_channel_id: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_youtube_watch(guild_id, yt_channel_id)
    return RedirectResponse(f"/guild/{guild_id}/youtube", status_code=303)


@app.post("/guild/{guild_id}/youtube/settings")
async def save_youtube_settings(
    request: Request, guild_id: int, yt_channel_id: str = Form(...),
    notify_videos: str = Form(default=""), notify_lives: str = Form(default=""),
    role_id: str = Form(default=""), live_announce_channel_id: str = Form(default=""),
):
    if (r := await require_auth(request)):
        return r

    if not any(yt_id == yt_channel_id for yt_id, *_rest in db.list_youtube_watches(guild_id)):
        return RedirectResponse(f"/guild/{guild_id}/youtube?error=notfound", status_code=303)

    videos_on, lives_on = notify_videos == "on", notify_lives == "on"
    if not videos_on and not lives_on:
        return RedirectResponse(f"/guild/{guild_id}/youtube?error=notifytype", status_code=303)

    parsed_role_id = None
    if role_id:
        if not role_id.isdigit() or not validate_role(guild_id, int(role_id)):
            return RedirectResponse(f"/guild/{guild_id}/youtube?error=role", status_code=303)
        parsed_role_id = int(role_id)

    parsed_live_channel_id = None
    if live_announce_channel_id:
        if not live_announce_channel_id.isdigit() or not validate_channel(guild_id, int(live_announce_channel_id), ("text", "news")):
            return RedirectResponse(f"/guild/{guild_id}/youtube?error=channel", status_code=303)
        parsed_live_channel_id = int(live_announce_channel_id)

    db.set_youtube_notify(guild_id, yt_channel_id, videos_on, lives_on)
    db.set_youtube_role(guild_id, yt_channel_id, parsed_role_id)
    db.set_youtube_live_channel(guild_id, yt_channel_id, parsed_live_channel_id)
    return RedirectResponse(f"/guild/{guild_id}/youtube?saved=1", status_code=303)
