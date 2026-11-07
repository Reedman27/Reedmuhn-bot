"""Routes for the community section of the dashboard (mirrors cogs/community/).

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

@app.get("/guild/{guild_id}/welcome")
async def welcome_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    cfg = db.get_guild_config(guild_id)
    return render(request, "welcome.html", guild_id, "welcome", cfg=cfg,
                  text_channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"),
                  roles=db.list_bot_roles(guild_id))


@app.post("/guild/{guild_id}/welcome")
async def save_welcome(request: Request, guild_id: int, channel_id: int = Form(...), message: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/welcome?error=channel", status_code=303)
    db.set_welcome(guild_id, channel_id, message.strip())
    return RedirectResponse(f"/guild/{guild_id}/welcome", status_code=303)


@app.post("/guild/{guild_id}/welcome/card")
async def save_welcome_card(request: Request, guild_id: int, enabled: str = Form(None)):
    if (r := await require_auth(request)):
        return r
    db.set_welcome_card_enabled(guild_id, enabled == "on")
    return RedirectResponse(f"/guild/{guild_id}/welcome", status_code=303)


@app.post("/guild/{guild_id}/autorole")
async def save_autorole(request: Request, guild_id: int, role_id: str = Form("")):
    if (r := await require_auth(request)):
        return r
    role_id = role_id.strip()
    if not role_id:
        db.clear_autorole(guild_id)
        return RedirectResponse(f"/guild/{guild_id}/welcome", status_code=303)
    try:
        role_id_int = int(role_id)
    except ValueError:
        return RedirectResponse(f"/guild/{guild_id}/welcome?error=role", status_code=303)
    if not validate_role(guild_id, role_id_int):
        return RedirectResponse(f"/guild/{guild_id}/welcome?error=role", status_code=303)
    db.set_autorole(guild_id, role_id_int)
    return RedirectResponse(f"/guild/{guild_id}/welcome", status_code=303)


@app.get("/guild/{guild_id}/birthdays")
async def birthdays_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    return render(
        request, "birthdays.html", guild_id, "birthdays",
        cfg=db.get_guild_config(guild_id),
        birthdays=db.list_birthdays(guild_id),
        month_names=MONTH_NAMES,
        text_channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"),
        members=db.list_bot_members(guild_id),
    )


@app.post("/guild/{guild_id}/birthdays/channel")
async def save_birthday_channel(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/birthdays?error=channel", status_code=303)
    db.set_birthday_channel(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/birthdays", status_code=303)


@app.post("/guild/{guild_id}/birthdays/delete")
async def delete_birthday(request: Request, guild_id: int, user_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    # Existing birthday records may belong to members who have since left.
    # The dashboard still allows removing those stale records.
    db.remove_birthday(guild_id, user_id)
    return RedirectResponse(f"/guild/{guild_id}/birthdays", status_code=303)


@app.get("/guild/{guild_id}/counting")
async def counting_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    counting = db.get_counting(guild_id)
    return render(request, "counting.html", guild_id, "counting", counting=counting,
                  text_channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"))


@app.post("/guild/{guild_id}/counting/channel")
async def save_counting_channel(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/counting?error=channel", status_code=303)
    db.set_counting_channel(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/counting", status_code=303)


@app.post("/guild/{guild_id}/counting/savesenabled")
async def save_counting_saves_enabled(request: Request, guild_id: int, enabled: str = Form(default="")):
    if (r := await require_auth(request)):
        return r
    db.set_saves_enabled(guild_id, enabled == "on")
    db.record_bot_event("counting.save_config", guild_id, None, None,
                         f"enabled={enabled == 'on'}", source="dashboard_counting")
    return RedirectResponse(f"/guild/{guild_id}/counting", status_code=303)


@app.post("/guild/{guild_id}/counting/saves")
async def save_counting_saves(request: Request, guild_id: int, milestone: int = Form(...), max_saves: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not 1 <= milestone <= 1_000_000 or not 0 <= max_saves <= 100:
        return RedirectResponse(f"/guild/{guild_id}/counting?error=invalid", status_code=303)
    db.set_save_settings(guild_id, milestone, max_saves)
    db.record_bot_event("counting.save_config", guild_id, None, None,
                         f"interval={milestone} cap={max_saves}", source="dashboard_counting")
    return RedirectResponse(f"/guild/{guild_id}/counting", status_code=303)


@app.post("/guild/{guild_id}/counting/highscorealerts")
async def save_counting_highscorealerts(request: Request, guild_id: int, enabled: str = Form(default="")):
    if (r := await require_auth(request)):
        return r
    db.set_high_score_alerts(guild_id, enabled == "on")
    return RedirectResponse(f"/guild/{guild_id}/counting", status_code=303)


@app.get("/guild/{guild_id}/starboard")
async def starboard_page(request: Request, guild_id: int):
    if (r := await require_auth(request)): return r
    channel_id,threshold,enabled=db.get_starboard_config(guild_id)
    return render(request,"starboard.html",guild_id,"starboard",channel_id=channel_id,threshold=threshold,enabled=bool(enabled),channels=db.list_bot_channels(guild_id,"text")+db.list_bot_channels(guild_id,"news"))


@app.post("/guild/{guild_id}/starboard/save")
async def starboard_save(request: Request,guild_id:int,channel_id:str=Form(""),threshold:int=Form(5),enabled:str=Form("")):
    if (r := await require_auth(request)): return r
    cid=int(channel_id) if channel_id else None
    if cid and not validate_channel(guild_id,cid,("text","news")): cid=None
    threshold=max(1,min(50,threshold))
    db.set_starboard_config(guild_id,cid,threshold,enabled=="on")
    return RedirectResponse(f"/guild/{guild_id}/starboard",status_code=303)


@app.get("/guild/{guild_id}/suggestions")
async def suggestions_page(request: Request,guild_id:int):
    if (r := await require_auth(request)): return r
    channel_id,enabled,staff_role_id=db.get_suggestion_config(guild_id)
    rows=db.list_suggestions(guild_id,50)
    suggestions=[{"id":r[0],"author_id":r[2],"content":r[3],"status":r[4],"staff_id":r[5],"reason":r[6],"created_at":datetime.fromtimestamp(r[7]).strftime("%Y-%m-%d %H:%M")} for r in rows]
    return render(request,"suggestions.html",guild_id,"suggestions",channel_id=channel_id,enabled=bool(enabled),staff_role_id=staff_role_id,channels=db.list_bot_channels(guild_id,"text")+db.list_bot_channels(guild_id,"news"),roles=db.list_bot_roles(guild_id),suggestions=suggestions)


@app.post("/guild/{guild_id}/suggestions/config")
async def suggestions_config(request: Request,guild_id:int,channel_id:str=Form(""),staff_role_id:str=Form(""),enabled:str=Form("")):
    if (r := await require_auth(request)): return r
    cid=int(channel_id) if channel_id else None; rid=int(staff_role_id) if staff_role_id else None
    if cid and not validate_channel(guild_id,cid,("text","news")): cid=None
    db.set_suggestion_config(guild_id,cid,enabled=="on",rid)
    return RedirectResponse(f"/guild/{guild_id}/suggestions",status_code=303)


@app.post("/guild/{guild_id}/suggestions/status")
async def suggestions_status(request: Request,guild_id:int,suggestion_id:int=Form(...),status:str=Form(...),reason:str=Form("")):
    if (r := await require_auth(request)): return r
    if status not in {"pending","approved","denied"}: return RedirectResponse(f"/guild/{guild_id}/suggestions",status_code=303)
    row=db.get_suggestion(suggestion_id)
    if row and row[1]==guild_id: db.set_suggestion_status(suggestion_id,status,0,reason[:1024])
    return RedirectResponse(f"/guild/{guild_id}/suggestions",status_code=303)


@app.get("/guild/{guild_id}/reactionroles")
async def reactionroles_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    rows = db.list_reaction_roles(guild_id)
    bindings = [
        {
            "message_id": msg_id,
            "channel_id": chan_id,
            "channel_name": channel_label(guild_id, chan_id),
            "emoji": emoji,
            "role_id": role_id,
            "role_name": role_label(guild_id, role_id),
            "jump_url": f"https://discord.com/channels/{guild_id}/{chan_id}/{msg_id}",
        }
        for msg_id, chan_id, emoji, role_id in rows
    ]
    return render(
        request, "reactionroles.html", guild_id, "reactionroles", bindings=bindings,
        text_channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"),
        roles=db.list_bot_roles(guild_id),
    )


@app.post("/guild/{guild_id}/reactionroles/menu")
async def queue_create_reaction_role_menu(
    request: Request, guild_id: int, pairs: str = Form(...),
    title: str = Form("Reaction Roles"), description: str = Form("React below to add/remove a role."),
    channel_id: int = Form(...),
):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/reactionroles?error=channel", status_code=303)
    parsed = []
    for item in re.split(r"[;\n]+", pairs or ""):
        item = item.strip()
        if not item or "=" not in item:
            continue
        emoji, role_ref = item.split("=", 1)
        emoji = emoji.strip()
        role_ref = role_ref.strip()
        role_match = re.fullmatch(r"(?:<@&(\d+)>|(\d+))", role_ref)
        if not emoji or not role_match:
            return RedirectResponse(f"/guild/{guild_id}/reactionroles?error=menu", status_code=303)
        role_id = int(role_match.group(1) or role_match.group(2))
        # Basic validation here; the bot performs the authoritative emoji and
        # role hierarchy checks when it executes the queued request.
        parsed.append((emoji, role_id))
        if len(parsed) >= 10:
            break
    if not parsed or any(not validate_role(guild_id, role_id) for _emoji, role_id in parsed):
        return RedirectResponse(f"/guild/{guild_id}/reactionroles?error=menu", status_code=303)
    db.insert_scheduled_event(
        "create_reaction_role_menu", guild_id, int(time.time()),
        {"channel_id": channel_id, "pairs": pairs, "title": title.strip(), "description": description.strip()},
    )
    return RedirectResponse(f"/guild/{guild_id}/reactionroles?queued_menu=1", status_code=303)


@app.post("/guild/{guild_id}/reactionroles/add")
async def queue_add_reaction_role(
    request: Request, guild_id: int, message: str = Form(...),
    emoji: str = Form(...), role_id: int = Form(...), channel_id: Optional[int] = Form(None),
):
    if (r := await require_auth(request)):
        return r
    if not validate_role(guild_id, role_id):
        return RedirectResponse(f"/guild/{guild_id}/reactionroles?error=role", status_code=303)

    link_channel_id, parsed_message_id = parse_message_reference(message)
    if parsed_message_id is None:
        return RedirectResponse(f"/guild/{guild_id}/reactionroles?error=message", status_code=303)

    target_channel_id = link_channel_id if link_channel_id is not None else channel_id
    if target_channel_id is None or not validate_channel(guild_id, target_channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/reactionroles?error=channel", status_code=303)

    emoji = emoji.strip()
    if not emoji:
        return RedirectResponse(f"/guild/{guild_id}/reactionroles?error=emoji", status_code=303)

    # The dashboard has no live Discord connection of its own (it only
    # shares bot.db over a mounted volume) - it can't place the reaction on
    # the message itself. It queues the request as a scheduled event with a
    # run_at of "now", and the bot (which does have a connection) picks it
    # up and does the actual work on its next scheduler tick - same
    # mechanism as tempbans/reminders, just with near-immediate timing
    # instead of a future one. See scheduler._handle_add_reaction_role.
    db.insert_scheduled_event(
        "add_reaction_role", guild_id, int(time.time()),
        {"channel_id": target_channel_id, "message_id": parsed_message_id, "emoji": emoji, "role_id": role_id},
    )
    return RedirectResponse(f"/guild/{guild_id}/reactionroles?queued=1", status_code=303)


@app.post("/guild/{guild_id}/reactionroles/delete")
async def delete_reaction_role(request: Request, guild_id: int, message_id: int = Form(...), emoji: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_reaction_role(guild_id, message_id, emoji)
    return RedirectResponse(f"/guild/{guild_id}/reactionroles", status_code=303)


@app.get("/guild/{guild_id}/stickyroles")
async def stickyroles_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    cfg = db.get_guild_config(guild_id)
    excluded_ids = set(db.list_sticky_role_exclusions(guild_id))
    roles = [
        (rid, name, position, rid in excluded_ids)
        for rid, name, position in db.list_bot_roles(guild_id)
    ]
    return render(
        request, "stickyroles.html", guild_id, "stickyroles",
        enabled=cfg["sticky_roles_enabled"],
        roles=roles,
    )


@app.post("/guild/{guild_id}/stickyroles/toggle")
async def stickyroles_toggle(request: Request, guild_id: int, enabled: Optional[str] = Form(None)):
    if (r := await require_auth(request)):
        return r
    db.set_sticky_roles_enabled(guild_id, enabled == "on")
    return RedirectResponse(f"/guild/{guild_id}/stickyroles", status_code=303)


@app.post("/guild/{guild_id}/stickyroles/exclude")
async def stickyroles_exclude(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    valid_roles = {rid for rid, _, _ in db.list_bot_roles(guild_id)}
    if role_id in valid_roles:
        db.add_sticky_role_exclusion(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/stickyroles", status_code=303)


@app.post("/guild/{guild_id}/stickyroles/include")
async def stickyroles_include(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_sticky_role_exclusion(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/stickyroles", status_code=303)


@app.get("/guild/{guild_id}/invites")
async def invites_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    leaderboard = [
        {"inviter_id": inviter_id, "inviter_name": member_label(guild_id, inviter_id), "count": count}
        for inviter_id, count in db.list_invite_leaderboard(guild_id, 25)
    ]
    recent = [
        {
            "user_name": member_label(guild_id, user_id),
            "inviter_name": member_label(guild_id, inviter_id) if inviter_id else "Unknown (vanity URL, deleted invite, or no permission to list invites)",
            "invite_code": invite_code or "-",
            "date_str": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M"),
        }
        for user_id, inviter_id, invite_code, created_at in db.list_recent_invite_joins(guild_id, 30)
    ]
    milestones = [
        {"invite_count": count, "role_id": role_id, "role_name": role_label(guild_id, role_id)}
        for count, role_id in db.list_invite_milestones(guild_id)
    ]
    return render(
        request, "invites.html", guild_id, "invites",
        leaderboard=leaderboard, recent=recent, milestones=milestones,
        role_choices=db.list_bot_roles(guild_id),
        quick_leaves=db.count_quick_leaves(guild_id, 24),
    )


@app.post("/guild/{guild_id}/invites/milestones/add")
async def add_invite_milestone(request: Request, guild_id: int, invite_count: int = Form(...), role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if invite_count < 1 or not validate_role(guild_id, role_id):
        return RedirectResponse(f"/guild/{guild_id}/invites?error=milestone", status_code=303)
    db.set_invite_milestone(guild_id, invite_count, role_id)
    return RedirectResponse(f"/guild/{guild_id}/invites", status_code=303)


@app.post("/guild/{guild_id}/invites/milestones/delete")
async def delete_invite_milestone(request: Request, guild_id: int, invite_count: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_invite_milestone(guild_id, invite_count)
    return RedirectResponse(f"/guild/{guild_id}/invites", status_code=303)
