"""Routes for the music section of the dashboard (mirrors cogs/music/).

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

@app.get("/guild/{guild_id}/music")
async def music_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    settings = db.get_music_settings(guild_id)
    return render(
        request, "music.html", guild_id, "music",
        volume=settings.get("volume", 100),
        dj_role_id=settings.get("dj"),
        always_on=bool(settings.get("24/7", False)),
        self_deaf=bool(settings.get("self_deaf", False)),
        roles=db.list_bot_roles(guild_id),
    )


@app.post("/guild/{guild_id}/music/save")
async def music_save(
    request: Request, guild_id: int,
    volume: int = Form(100), dj_role_id: str = Form(""), always_on: str = Form(""),
    self_deaf: str = Form(""),
):
    if (r := await require_auth(request)):
        return r
    volume = max(1, min(150, volume))
    dj_id = int(dj_role_id) if dj_role_id else None
    db.update_music_settings(
        guild_id, volume=volume, dj=dj_id,
        **{"24/7": always_on == "on"}, self_deaf=(self_deaf == "on"),
    )
    return RedirectResponse(f"/guild/{guild_id}/music", status_code=303)
