"""Routes for the fun section of the dashboard (mirrors cogs/fun/).

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

@app.get("/guild/{guild_id}/fun-commands")
async def fun_commands_page(request: Request, guild_id: int):
    if (r := await require_auth(request)): return r
    disabled=db.get_disabled_commands(guild_id)
    return render(request,"funcommands.html",guild_id,"funcommands",groups=[(name,[(cmd,desc,cmd not in disabled) for cmd,desc in items]) for name,items in FUN_COMMANDS])


@app.post("/guild/{guild_id}/fun-commands/toggle")
async def toggle_fun_command(request: Request, guild_id: int, command_name: str = Form(...), enabled: str = Form("")):
    if (r := await require_auth(request)): return r
    allowed={cmd for _,items in FUN_COMMANDS for cmd,_ in items}
    if command_name in allowed: db.set_command_enabled(guild_id,command_name,enabled=="on")
    return RedirectResponse(f"/guild/{guild_id}/fun-commands",status_code=303)


@app.get("/guild/{guild_id}/counters")
async def counters_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    counters = [
        {"kind": kind, "label": COUNTER_KINDS.get(kind, kind.title()), "channel_id": cid, "channel_name": channel_label(guild_id, cid)}
        for kind, cid in db.list_extras_counters(guild_id)
    ]
    return render(
        request, "counters.html", guild_id, "counters",
        counters=counters, counter_kinds=COUNTER_KINDS,
        available_counter_kinds=[k for k in COUNTER_KINDS if k not in {c["kind"] for c in counters}],
        voice_channels=db.list_bot_channels(guild_id, "voice"),
    )


@app.post("/guild/{guild_id}/counters/add")
async def add_extras_counter(request: Request, guild_id: int, kind: str = Form(...), channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if kind not in COUNTER_KINDS:
        return RedirectResponse(f"/guild/{guild_id}/counters?error=counterkind", status_code=303)
    if not validate_channel(guild_id, channel_id, ("voice",)):
        return RedirectResponse(f"/guild/{guild_id}/counters?error=counterchannel", status_code=303)
    db.upsert_extras_counter(guild_id, kind, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/counters?saved=1", status_code=303)


@app.post("/guild/{guild_id}/counters/delete")
async def delete_extras_counter(request: Request, guild_id: int, kind: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.delete_extras_counter(guild_id, kind)
    return RedirectResponse(f"/guild/{guild_id}/counters", status_code=303)


@app.get("/guild/{guild_id}/twitch")
async def twitch_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    twitch = [
        {"username": username, "channel_id": cid, "channel_name": channel_label(guild_id, cid), "live": bool(live)}
        for username, cid, live in db.list_extras_twitch(guild_id)
    ]
    return render(
        request, "twitch.html", guild_id, "twitch",
        twitch=twitch, text_channels=db.list_bot_channels(guild_id, "text"),
        twitch_configured=bool(os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET")),
    )


@app.post("/guild/{guild_id}/twitch/add")
async def add_extras_twitch(request: Request, guild_id: int, username: str = Form(...), channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    username = username.strip()
    if not username or not re.fullmatch(r"[A-Za-z0-9_]{1,25}", username):
        return RedirectResponse(f"/guild/{guild_id}/twitch?error=twitchuser", status_code=303)
    if not validate_channel(guild_id, channel_id, ("text",)):
        return RedirectResponse(f"/guild/{guild_id}/twitch?error=twitchchannel", status_code=303)
    db.upsert_extras_twitch(guild_id, username, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/twitch?saved=1", status_code=303)


@app.post("/guild/{guild_id}/twitch/delete")
async def delete_extras_twitch(request: Request, guild_id: int, username: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.delete_extras_twitch(guild_id, username)
    return RedirectResponse(f"/guild/{guild_id}/twitch", status_code=303)


@app.get("/guild/{guild_id}/feeds")
async def feeds_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    feeds = [
        {"url": url, "channel_id": cid, "channel_name": channel_label(guild_id, cid)}
        for url, cid in db.list_extras_feeds(guild_id)
    ]
    return render(
        request, "feeds.html", guild_id, "feeds",
        feeds=feeds, text_channels=db.list_bot_channels(guild_id, "text"),
    )


@app.post("/guild/{guild_id}/feeds/add")
async def add_extras_feed(request: Request, guild_id: int, url: str = Form(...), channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    try:
        url = validate_feed_url(url)
    except ValueError:
        return RedirectResponse(f"/guild/{guild_id}/feeds?error=feedurl", status_code=303)
    if not validate_channel(guild_id, channel_id, ("text",)):
        return RedirectResponse(f"/guild/{guild_id}/feeds?error=feedchannel", status_code=303)
    db.upsert_extras_feed(guild_id, url, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/feeds?saved=1", status_code=303)


@app.post("/guild/{guild_id}/feeds/delete")
async def delete_extras_feed(request: Request, guild_id: int, url: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.delete_extras_feed(guild_id, url)
    return RedirectResponse(f"/guild/{guild_id}/feeds", status_code=303)


@app.get("/guild/{guild_id}/giveaways")
async def giveaways_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    giveaways = [
        {
            "id": gid, "channel_id": cid, "channel_name": channel_label(guild_id, cid),
            "message_id": mid, "prize": prize, "winners": winners, "end_at": end_at, "ended": bool(ended),
        }
        for gid, cid, mid, prize, winners, end_at, ended in db.list_extras_giveaways(guild_id)
    ]
    return render(request, "giveaways.html", guild_id, "giveaways", giveaways=giveaways)


@app.post("/guild/{guild_id}/giveaways/end")
async def end_extras_giveaway(request: Request, guild_id: int, message_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not any(mid == message_id for _gid, _cid, mid, *_rest in db.list_extras_giveaways(guild_id)):
        return RedirectResponse(f"/guild/{guild_id}/giveaways?error=giveawaynotfound", status_code=303)
    # The dashboard has no live Discord connection - queue it for the bot
    # process to pick up on its next scheduler tick (same mechanism as
    # poll close / youtube watch add; see scheduler._handle_end_giveaway).
    db.insert_scheduled_event("end_giveaway", guild_id, int(time.time()), {"message_id": message_id})
    return RedirectResponse(f"/guild/{guild_id}/giveaways?queued=1", status_code=303)


@app.get("/guild/{guild_id}/leveling")
async def leveling_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    xp_leaderboard = [
        {"user_id": uid, "name": member_label(guild_id, uid), "xp": xp, "level": level}
        for uid, xp, level in db.list_extras_xp_leaderboard(guild_id)
    ]
    level_config = db.get_extras_level_config(guild_id)
    level_roles = [(level, role_id, role_label(guild_id, role_id)) for level, role_id in db.list_extras_level_roles(guild_id)]
    noxp_channels = [(cid, channel_label(guild_id, cid)) for cid in db.list_extras_noxp_channels(guild_id)]
    boost_roles = [(role_id, role_label(guild_id, role_id), mult) for role_id, mult in db.list_extras_xp_boost_roles(guild_id)]
    return render(
        request, "leveling.html", guild_id, "leveling",
        xp_leaderboard=xp_leaderboard, members=db.list_bot_members(guild_id),
        level_config=level_config, level_roles=level_roles, noxp_channels=noxp_channels, boost_roles=boost_roles,
        text_channels=db.list_bot_channels(guild_id, "text"), roles=db.list_bot_roles(guild_id),
    )


@app.post("/guild/{guild_id}/leveling/xp/set")
async def set_extras_xp_route(request: Request, guild_id: int, user_id: int = Form(...), amount: int = Form(...), op: str = Form("set")):
    if (r := await require_auth(request)):
        return r
    if amount < 0 or amount > EXTRAS_MAX_AMOUNT:
        return RedirectResponse(f"/guild/{guild_id}/leveling?error=xpamount", status_code=303)
    current = db.get_extras_xp(guild_id, user_id)
    if op == "add":
        new_xp = current + amount
    elif op == "subtract":
        new_xp = current - amount
    else:
        new_xp = amount
    result = db.set_extras_xp_detailed(guild_id, user_id, new_xp)
    # This process has no Discord connection, so it can't hand out the reward
    # roles the new level earns. Queue the work for the bot (same mechanism as
    # the reaction-role and giveaway actions above) - otherwise an admin
    # bumping someone from level 4 to level 10 here would leave the database
    # saying level 10 while the member never gets the level 5-10 roles.
    if result["new_level"] > result["old_level"]:
        db.insert_scheduled_event(
            "sync_level_rewards", guild_id, int(time.time()),
            {"user_id": user_id, "new_level": result["new_level"], "old_level": result["old_level"]},
        )
    return RedirectResponse(f"/guild/{guild_id}/leveling?saved=1", status_code=303)


@app.post("/guild/{guild_id}/leveling/config")
async def set_extras_level_config_route(
    request: Request, guild_id: int, enabled: str = Form(None), channel_id: str = Form(""), message: str = Form(...)
):
    if (r := await require_auth(request)):
        return r
    channel_id_int = int(channel_id) if channel_id and validate_channel(guild_id, int(channel_id), ("text",)) else None
    db.set_extras_level_config(guild_id, bool(enabled), channel_id_int, message.strip() or "🎉 {user} reached **level {level}**!")
    return RedirectResponse(f"/guild/{guild_id}/leveling?saved=1", status_code=303)


@app.post("/guild/{guild_id}/leveling/roles/add")
async def add_extras_level_role(request: Request, guild_id: int, level: int = Form(...), role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if level < 1:
        return RedirectResponse(f"/guild/{guild_id}/leveling?error=levelrolelevel", status_code=303)
    db.set_extras_level_role(guild_id, level, role_id)
    return RedirectResponse(f"/guild/{guild_id}/leveling?saved=1", status_code=303)


@app.post("/guild/{guild_id}/leveling/roles/delete")
async def delete_extras_level_role(request: Request, guild_id: int, level: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_extras_level_role(guild_id, level)
    return RedirectResponse(f"/guild/{guild_id}/leveling", status_code=303)


@app.post("/guild/{guild_id}/leveling/noxp/add")
async def add_extras_noxp_channel_route(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("text",)):
        return RedirectResponse(f"/guild/{guild_id}/leveling?error=noxpchannel", status_code=303)
    db.add_extras_noxp_channel(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/leveling?saved=1", status_code=303)


@app.post("/guild/{guild_id}/leveling/noxp/delete")
async def delete_extras_noxp_channel_route(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_extras_noxp_channel(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/leveling", status_code=303)


@app.post("/guild/{guild_id}/leveling/boost/add")
async def add_extras_boost_role_route(request: Request, guild_id: int, role_id: int = Form(...), multiplier: float = Form(...)):
    if (r := await require_auth(request)):
        return r
    if multiplier < 1.0 or multiplier > 10.0:
        return RedirectResponse(f"/guild/{guild_id}/leveling?error=boostmultiplier", status_code=303)
    db.set_extras_xp_boost_role(guild_id, role_id, multiplier)
    return RedirectResponse(f"/guild/{guild_id}/leveling?saved=1", status_code=303)


@app.post("/guild/{guild_id}/leveling/boost/delete")
async def delete_extras_boost_role_route(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_extras_xp_boost_role(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/leveling", status_code=303)


@app.get("/guild/{guild_id}/economy")
async def economy_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    economy_leaderboard = [
        {"user_id": uid, "name": member_label(guild_id, uid), "balance": bal, "streak": streak}
        for uid, bal, streak in db.list_extras_economy_leaderboard(guild_id)
    ]
    return render(
        request, "economy.html", guild_id, "economy",
        economy_leaderboard=economy_leaderboard, members=db.list_bot_members(guild_id),
    )


@app.post("/guild/{guild_id}/economy/set")
async def set_extras_balance_route(request: Request, guild_id: int, user_id: int = Form(...), amount: int = Form(...), op: str = Form("set")):
    if (r := await require_auth(request)):
        return r
    if amount < 0 or amount > EXTRAS_MAX_AMOUNT:
        return RedirectResponse(f"/guild/{guild_id}/economy?error=coinamount", status_code=303)
    current = db.get_extras_balance(guild_id, user_id)
    if op == "add":
        new_balance = current + amount
    elif op == "subtract":
        new_balance = current - amount
    else:
        new_balance = amount
    db.set_extras_balance(guild_id, user_id, new_balance)
    return RedirectResponse(f"/guild/{guild_id}/economy?saved=1", status_code=303)


@app.get("/guild/{guild_id}/polls")
async def polls_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    polls = []
    for poll in db.list_polls(guild_id, 20):
        counts = db.poll_results(poll["id"], len(poll["options"]))
        total = sum(counts)
        voter_ids = db.poll_voters(poll["id"], len(poll["options"]))
        results = [
            {
                "option": opt, "count": counts[i],
                "pct": round(counts[i] / total * 100) if total else 0,
                "voters": [member_label(guild_id, uid) for uid in voter_ids.get(i, [])],
            }
            for i, opt in enumerate(poll["options"])
        ]
        polls.append({
            "id": poll["id"],
            "question": poll["question"],
            "results": results,
            "total": total,
            "closed": poll["closed"],
            "created_by": member_label(guild_id, poll["created_by"]),
            "created_at": datetime.fromtimestamp(poll["created_at"]).strftime("%Y-%m-%d %H:%M"),
            "channel_label": channel_label(guild_id, poll["channel_id"]),
        })
    return render(request, "polls.html", guild_id, "polls", polls=polls)


@app.post("/guild/{guild_id}/polls/close")
async def close_poll_route(request: Request, guild_id: int, poll_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    poll = db.get_poll(poll_id)
    if poll is None or poll["guild_id"] != guild_id:
        return RedirectResponse(f"/guild/{guild_id}/polls?error=notfound", status_code=303)
    # Queue even when the DB already says closed. The previous attempt may
    # have committed the closed flag but failed to edit the Discord message;
    # re-running the idempotent close handler repairs the stale embed/buttons.
    db.queue_poll_close(guild_id, poll_id)
    await _wake("poll_close")
    return RedirectResponse(f"/guild/{guild_id}/polls?closequeued={poll_id}", status_code=303)
