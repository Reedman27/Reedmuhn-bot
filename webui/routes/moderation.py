"""Routes for the moderation section of the dashboard (mirrors cogs/moderation/).

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

@app.post("/guild/{guild_id}/moderation/votekick")
async def save_votekick_config(request: Request, guild_id: int, enabled: str = Form(None), required_votes: int = Form(5), duration_minutes: int = Form(10)):
    if (r := await require_auth(request)): return r
    if not 1 <= required_votes <= 100 or not 1 <= duration_minutes <= 1440:
        return RedirectResponse(f"/guild/{guild_id}/moderation?error=votekick", status_code=303)
    db.set_votekick_config(guild_id, enabled == "on", required_votes, duration_minutes * 60)
    return RedirectResponse(f"/guild/{guild_id}/moderation", status_code=303)


@app.get("/guild/{guild_id}/moderation")
async def moderation_page(request: Request, guild_id: int, user_id: Optional[int] = None, tab: str = "overview"):
    if (r := await require_auth(request)):
        return r

    if tab not in {"overview", "warnings", "cases", "tempbans"}:
        tab = "overview"

    warns = []
    if user_id:
        rows = db.list_warns(guild_id, user_id)
        warns = []
        for warn_id, moderator_id, reason, created_at, rule_id, notes in rows:
            rule_label = None
            if rule_id is not None:
                rule_number = db.rule_number_for_id(guild_id, rule_id)
                rule_text = db.get_rule_by_id(guild_id, rule_id)
                if rule_number is not None and rule_text is not None:
                    rule_label = f"Rule #{rule_number}: {rule_text}"
            warns.append({
                "id": warn_id,
                "moderator_id": moderator_id,
                "moderator_name": "Dashboard" if moderator_id == 0 else member_label(guild_id, moderator_id),
                "reason": reason,
                "date_str": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M"),
                "rule_label": rule_label,
                "notes": notes,
            })

    warned_users = [
        (uid, member_label(guild_id, uid), count, datetime.fromtimestamp(last_at).strftime("%Y-%m-%d %H:%M"))
        for uid, count, last_at in db.list_warned_users(guild_id)
    ]

    def _format_case(row, with_member=False):
        if with_member:
            case_number, uid, event_type, actor_id, reason, created_at, voided = row
        else:
            case_number, event_type, actor_id, reason, details, created_at, voided = row
        item = {
            "case_number": case_number,
            "action": event_type,
            "action_label": CASE_ACTION_LABELS.get(event_type, event_type.replace("_", " ").title()),
            "moderator_name": "System" if not actor_id else member_label(guild_id, actor_id),
            "reason": reason,
            "date_str": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M"),
            "voided": bool(voided),
        }
        if with_member:
            item["user_id"] = uid
            item["user_name"] = member_label(guild_id, uid)
        return item

    recent_cases = [_format_case(row, with_member=True) for row in db.list_recent_cases(guild_id, 20)]
    user_cases = []
    if user_id and tab == "cases":
        user_cases = [_format_case(row) for row in db.list_cases_for_user(guild_id, user_id)]

    tempbans = []
    for event_id, _, run_at, data in db.list_scheduled_events(guild_id, "unban"):
        parsed = json.loads(data)
        tempbans.append({
            "id": event_id,
            "user_id": parsed["user_id"],
            "user_name": member_label(guild_id, parsed["user_id"]),
            "unban_at": datetime.fromtimestamp(run_at).strftime("%Y-%m-%d %H:%M"),
        })

    tempnicks_count = len(db.list_scheduled_events(guild_id, "revert_nick"))

    cfg = db.get_guild_config(guild_id)
    members = db.list_bot_members(guild_id)
    purge_requests = []
    for row in db.recent_purge_requests(guild_id, 15):
        (pid, cid, uid, amount, reason, status, created_at, completed_at, error,
         deleted_count, breakdown_json) = row
        breakdown = None
        if breakdown_json:
            try:
                # Sorted by volume so the biggest contributor to the purge
                # shows first, same as /purge's ephemeral reply.
                breakdown = sorted(json.loads(breakdown_json).items(), key=lambda pair: pair[1], reverse=True)
            except (json.JSONDecodeError, TypeError, AttributeError):
                breakdown = None
        purge_requests.append({
            "id": pid, "channel_id": cid, "user_id": uid, "amount": amount, "reason": reason,
            "status": status, "error": error, "deleted_count": deleted_count, "breakdown": breakdown,
        })
    mod_actions = [
        {
            "id": row[0], "user_id": row[1], "user_name": member_label(guild_id, row[1]),
            "action": row[2], "action_label": MOD_ACTION_LABELS.get(row[2], row[2]),
            "duration": format_duration(row[3]) if row[3] else None,
            "reason": row[4], "status": row[5],
            "created_at": datetime.fromtimestamp(row[6]).strftime("%Y-%m-%d %H:%M"),
            "error": row[8],
        }
        for row in db.recent_mod_actions(guild_id, 15)
    ]
    mute_sync = None
    sync_row = db.latest_mute_role_sync(guild_id)
    if sync_row:
        status, created_at, error, changed, failed = sync_row
        mute_sync = {
            "status": status, "error": error, "changed": changed, "failed": failed,
            "created_at": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M"),
        }
    return render(
        request, "moderation.html", guild_id, "moderation",
        tab=tab, looked_up_user_id=user_id,
        looked_up_user_name=(member_label(guild_id, user_id) if user_id else None),
        warns=warns, warned_users=warned_users, members=members, tempbans=tempbans,
        active_temp_nicknames=tempnicks_count,
        text_channels=db.list_bot_channels(guild_id, "text"), purge_requests=purge_requests,
        purge_member_names={uid: display for uid, display, _name in members},
        muted_role_id=cfg["muted_role_id"], roles=db.list_bot_roles(guild_id), muted_config=cfg,
        mod_action_requests=mod_actions, mod_action_choices=list(MOD_ACTION_LABELS.items()),
        mod_timed_actions=list(MOD_TIMED_ACTIONS), mute_sync=mute_sync,
        votekick_cfg=db.get_votekick_config(guild_id),
        recent_cases=recent_cases, user_cases=user_cases,
    )


@app.post("/guild/{guild_id}/moderation/purge")
async def queue_dashboard_purge(
    request: Request, guild_id: int, channel_id: int = Form(...),
    amount: int = Form(...), user_id: str = Form(""), reason: str = Form("WebUI message purge"),
):
    if (r := await require_auth(request)):
        return r
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 0
    if amount < 1 or amount > 1000 or not validate_channel(guild_id, channel_id, ("text",)):
        return RedirectResponse(f"/guild/{guild_id}/moderation?error=purge", status_code=303)
    target = int(user_id) if user_id.strip().isdigit() else None
    if target is not None and not any(uid == target for uid, _display, _name in db.list_bot_members(guild_id)):
        return RedirectResponse(f"/guild/{guild_id}/moderation?error=purge", status_code=303)
    reason = reason.strip()[:500] or "WebUI message purge"
    db.queue_purge_request(guild_id, channel_id, target, amount, reason)
    await _wake("purge")
    return RedirectResponse(f"/guild/{guild_id}/moderation?purge=queued", status_code=303)


@app.post("/guild/{guild_id}/moderation/clear-warns")
async def clear_warns_route(request: Request, guild_id: int, user_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.clear_warns(guild_id, user_id)
    return RedirectResponse(f"/guild/{guild_id}/moderation?tab=warnings&user_id={user_id}", status_code=303)


@app.post("/guild/{guild_id}/moderation/add-warn")
async def add_warn_route(request: Request, guild_id: int, user_id: int = Form(...), reason: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    reason = reason.strip() or "No reason given"
    # moderator_id 0 is a sentinel meaning "issued from the dashboard" -
    # there's no per-admin dashboard login to attribute it to a specific
    # Discord user, unlike /warn issued in Discord itself.
    db.add_warn(guild_id, user_id, 0, reason, int(time.time()))
    queue_warn_escalation_if_due(guild_id, user_id, reason)
    _wake_fire_and_forget("mod_action")
    return RedirectResponse(f"/guild/{guild_id}/moderation?tab=warnings&user_id={user_id}", status_code=303)


@app.post("/guild/{guild_id}/moderation/remove-warn")
async def remove_warn_route(request: Request, guild_id: int, warn_id: int = Form(...), user_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_warn(guild_id, warn_id)
    return RedirectResponse(f"/guild/{guild_id}/moderation?tab=warnings&user_id={user_id}", status_code=303)


@app.post("/guild/{guild_id}/moderation/warn-notes")
async def set_warn_notes_route(request: Request, guild_id: int, warn_id: int = Form(...), user_id: int = Form(...), notes: str = Form("")):
    if (r := await require_auth(request)):
        return r
    notes = notes.strip()[:1000]
    db.set_warn_notes(guild_id, warn_id, notes or None)
    return RedirectResponse(f"/guild/{guild_id}/moderation?tab=warnings&user_id={user_id}", status_code=303)


@app.post("/guild/{guild_id}/moderation/case-edit")
async def edit_case_route(request: Request, guild_id: int, case_number: int = Form(...), user_id: int = Form(...), reason: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.edit_case_reason(guild_id, case_number, reason.strip()[:500])
    return RedirectResponse(f"/guild/{guild_id}/moderation?tab=cases&user_id={user_id}", status_code=303)


@app.post("/guild/{guild_id}/moderation/case-void")
async def void_case_route(request: Request, guild_id: int, case_number: int = Form(...), user_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.void_case(guild_id, case_number, True)
    return RedirectResponse(f"/guild/{guild_id}/moderation?tab=cases&user_id={user_id}", status_code=303)


@app.post("/guild/{guild_id}/moderation/muted-role")
async def save_muted_role(request: Request, guild_id: int, role_id: str = Form("auto")):
    if (r := await require_auth(request)):
        return r
    if role_id == "auto":
        db.set_muted_role(guild_id, None)
    elif validate_role(guild_id, int(role_id)):
        db.set_muted_role(guild_id, int(role_id))
    else:
        return RedirectResponse(f"/guild/{guild_id}/moderation", status_code=303)
    db.queue_mute_role_sync(guild_id, "WebUI: Muted role changed")
    await _wake("mute_role_sync")
    return RedirectResponse(f"/guild/{guild_id}/moderation", status_code=303)


@app.post("/guild/{guild_id}/moderation/muted-role-settings")
async def save_muted_role_settings(
    request: Request, guild_id: int,
    messages: Optional[str] = Form(None), reactions: Optional[str] = Form(None),
    threads: Optional[str] = Form(None), connect: Optional[str] = Form(None),
    speak: Optional[str] = Form(None), stream: Optional[str] = Form(None),
    view_channel: Optional[str] = Form(None), strip_roles: Optional[str] = Form(None),
):
    if (r := await require_auth(request)):
        return r
    db.set_muted_settings(
        guild_id,
        deny_send_messages=messages == "on",
        deny_reactions=reactions == "on",
        deny_threads=threads == "on",
        deny_connect=connect == "on",
        deny_speak=speak == "on",
        deny_stream=stream == "on",
        deny_view_channel=view_channel == "on",
    )
    db.set_muted_strip_roles(guild_id, strip_roles == "on")
    db.queue_mute_role_sync(guild_id, "WebUI: Muted role settings changed")
    await _wake("mute_role_sync")
    return RedirectResponse(f"/guild/{guild_id}/moderation", status_code=303)


@app.post("/guild/{guild_id}/moderation/muted-role-preset")
async def save_muted_role_preset(request: Request, guild_id: int, preset: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    if preset not in MUTE_PRESETS:
        return RedirectResponse(f"/guild/{guild_id}/moderation?error=invalid", status_code=303)
    db.set_muted_settings(guild_id, **MUTE_PRESETS[preset])
    db.queue_mute_role_sync(guild_id, f"WebUI: Muted role preset applied ({preset})")
    await _wake("mute_role_sync")
    return RedirectResponse(f"/guild/{guild_id}/moderation", status_code=303)


@app.post("/guild/{guild_id}/moderation/tempbans/unban")
async def unban_tempban_now(request: Request, guild_id: int, event_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    event = db.get_scheduled_event(event_id, guild_id, "unban")
    if event:
        _event_id, _name, _run_at, data = event
        db.delete_scheduled_event(event_id, guild_id)
        parsed = json.loads(data)
        db.insert_scheduled_event("unban", guild_id, int(time.time()), {"user_id": parsed["user_id"]})
    return RedirectResponse(f"/guild/{guild_id}/moderation?tab=tempbans", status_code=303)


@app.post("/guild/{guild_id}/moderation/action")
async def queue_dashboard_mod_action(
    request: Request, guild_id: int, action: str = Form(...),
    user_id: str = Form(""), user_id_manual: str = Form(""),
    duration_value: str = Form(""), duration_unit: str = Form("m"),
    reason: str = Form("WebUI moderation action"),
):
    if (r := await require_auth(request)):
        return r
    if action not in MOD_ACTION_LABELS:
        return RedirectResponse(f"/guild/{guild_id}/moderation?error=action", status_code=303)

    manual = user_id_manual.strip()
    if manual:
        if not manual.isdigit():
            return RedirectResponse(f"/guild/{guild_id}/moderation?error=action", status_code=303)
        target_id = int(manual)
    elif user_id.strip().isdigit():
        target_id = int(user_id)
        # For every action except unban, the target needs to be a cached
        # (currently-present) member - the manual ID field exists
        # specifically so a mod can unban someone no longer in the server.
        if action != "unban" and not any(uid == target_id for uid, _display, _name in db.list_bot_members(guild_id)):
            return RedirectResponse(f"/guild/{guild_id}/moderation?error=action", status_code=303)
    else:
        return RedirectResponse(f"/guild/{guild_id}/moderation?error=action", status_code=303)

    duration_seconds = None
    if action in MOD_TIMED_ACTIONS:
        unit_seconds = {
            "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
            "mo": 30 * 86400, "y": 365 * 86400,
        }.get(duration_unit)
        if not duration_value.strip().isdigit() or unit_seconds is None:
            return RedirectResponse(f"/guild/{guild_id}/moderation?error=action", status_code=303)
        duration_seconds = int(duration_value) * unit_seconds
        max_seconds = MOD_ACTION_TIMEOUT_MAX_SECONDS if action == "timeout" else 365 * 86400
        if not 1 <= duration_seconds <= max_seconds:
            return RedirectResponse(f"/guild/{guild_id}/moderation?error=action", status_code=303)

    reason = reason.strip()[:500] or "WebUI moderation action"
    db.queue_mod_action(guild_id, target_id, action, duration_seconds, reason)
    await _wake("mod_action")
    return RedirectResponse(f"/guild/{guild_id}/moderation?action=queued", status_code=303)


@app.get("/guild/{guild_id}/antinuke")
async def antinuke_page(request: Request,guild_id:int):
    if (r := await require_auth(request)): return r
    cfg=db.get_antinuke_config(guild_id)
    return render(request,"antinuke.html",guild_id,"antinuke",cfg=cfg,channels=db.list_bot_channels(guild_id,"text")+db.list_bot_channels(guild_id,"news"),incidents=db.list_antinuke_incidents(guild_id,20))


@app.post("/guild/{guild_id}/antinuke/save")
async def antinuke_save(request: Request,guild_id:int,enabled:str=Form(""),auto_recovery:str=Form(""),punishment:str=Form("BAN"),threshold:int=Form(3),window_seconds:int=Form(10),log_channel_id:str=Form("")):
    if (r := await require_auth(request)): return r
    punishment=punishment.upper() if punishment.upper() in {"BAN","KICK"} else "BAN"
    db.set_antinuke_enabled(guild_id,enabled=="on"); db.set_antinuke_auto_recovery(guild_id,auto_recovery=="on"); db.set_antinuke_punishment(guild_id,punishment)
    db.set_antinuke_threshold(guild_id,max(1,min(50,threshold)),max(1,min(300,window_seconds)))
    cid=int(log_channel_id) if log_channel_id else None
    if cid and not validate_channel(guild_id,cid,("text","news")): cid=None
    db.set_antinuke_log_channel(guild_id,cid)
    return RedirectResponse(f"/guild/{guild_id}/antinuke",status_code=303)


@app.get("/guild/{guild_id}/raid")
async def raid_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    cfg = db.get_raid_config(guild_id)
    incidents = [
        {
            "join_count": row[1], "window_seconds": row[2],
            "action_label": RAID_ACTIONS.get(row[3], row[3]),
            "kicked_count": row[4],
            "date_str": datetime.fromtimestamp(row[5]).strftime("%Y-%m-%d %H:%M"),
        }
        for row in db.list_raid_incidents(guild_id, 20)
    ]
    return render(
        request, "raid.html", guild_id, "raid",
        cfg=cfg, action_choices=list(RAID_ACTIONS.items()),
        channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"),
        incidents=incidents,
    )


@app.post("/guild/{guild_id}/raid/save")
async def raid_save(
    request: Request, guild_id: int, enabled: str = Form(""), join_threshold: int = Form(10),
    window_seconds: int = Form(60), action: str = Form("alert"), new_account_hours: int = Form(168),
    cooldown_seconds: int = Form(300), log_channel_id: str = Form(""),
):
    if (r := await require_auth(request)):
        return r
    if action not in RAID_ACTIONS:
        action = "alert"
    cid = int(log_channel_id) if log_channel_id else None
    if cid and not validate_channel(guild_id, cid, ("text", "news")):
        cid = None
    db.set_raid_config(
        guild_id, enabled=enabled == "on", join_threshold=max(2, min(200, join_threshold)),
        window_seconds=max(5, min(600, window_seconds)), action=action,
        new_account_hours=max(1, min(8760, new_account_hours)),
        cooldown_seconds=max(30, min(3600, cooldown_seconds)), log_channel_id=cid,
    )
    return RedirectResponse(f"/guild/{guild_id}/raid?saved=1", status_code=303)


@app.get("/guild/{guild_id}/logging")
async def logging_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    configured = db.get_all_log_channels(guild_id)
    ignored_ids = db.list_ignored_log_channels(guild_id)
    channel_sync = None
    sync_row = db.latest_log_channel_create(guild_id)
    if sync_row:
        status, created_at, error, channel_id, category = sync_row
        channel_sync = {
            "status": status, "error": error, "channel_id": channel_id,
            "category": dict((key, name) for key, name, _desc in LOG_CATEGORIES).get(category, category),
            "channel_name": channel_label(guild_id, channel_id) if channel_id else None,
            "created_at": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M"),
        }
    return render(
        request, "logging.html", guild_id, "logging",
        categories=LOG_CATEGORIES,
        category_channel_names=LOG_CATEGORY_CHANNEL_NAMES,
        configured=configured,
        ignored_channels=[(cid, channel_label(guild_id, cid)) for cid in ignored_ids],
        channel_choices=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"),
        channel_sync=channel_sync,
    )


@app.post("/guild/{guild_id}/logging/auto-create")
async def auto_create_log_channels(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    # The WebUI is intentionally token-less. Queue one Discord-side request per
    # category; LoggingCog claims these requests and performs the real channel
    # creation with the bot token, just like the muted-role sync workflow.
    for key, _name, _description in LOG_CATEGORIES:
        if not db.get_all_log_channels(guild_id).get(key):
            db.queue_log_channel_create(guild_id, key, "WebUI: create/reuse all log channels")
    await _wake("log_channel_create")
    return RedirectResponse(f"/guild/{guild_id}/logging", status_code=303)


@app.post("/guild/{guild_id}/logging/channel")
async def save_logging_channel(request: Request, guild_id: int, category: str = Form(...), channel_id: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    allowed = {key for key, _name, _desc in LOG_CATEGORIES}
    if category not in allowed:
        return RedirectResponse(f"/guild/{guild_id}/logging?error=category", status_code=303)
    if channel_id == "off":
        db.disable_log_category(guild_id, category)
    elif channel_id == "auto":
        category_name = dict((key, name) for key, name, _desc in LOG_CATEGORIES).get(category, category)
        db.queue_log_channel_create(guild_id, category, f"WebUI: create channel for {category_name} logs")
        await _wake("log_channel_create")
    else:
        try:
            cid = int(channel_id)
        except ValueError:
            return RedirectResponse(f"/guild/{guild_id}/logging?error=channel", status_code=303)
        if not validate_channel(guild_id, cid, ("text", "news")):
            return RedirectResponse(f"/guild/{guild_id}/logging?error=channel", status_code=303)
        db.set_log_channel(guild_id, category, cid)
    return RedirectResponse(f"/guild/{guild_id}/logging", status_code=303)


@app.post("/guild/{guild_id}/logging/ignore/add")
async def add_logging_ignore(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/logging?error=channel", status_code=303)
    db.add_ignored_log_channel(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/logging", status_code=303)


@app.post("/guild/{guild_id}/logging/ignore/delete")
async def delete_logging_ignore(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_ignored_log_channel(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/logging", status_code=303)


@app.get("/guild/{guild_id}/automod")
async def automod_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    cfg = db.get_automod_config(guild_id)
    tiers = db.list_automod_escalation_tiers(guild_id)
    for tier in tiers:
        tier["action_label"] = AUTOMOD_ACTION_LABELS.get(tier["action"], tier["action"])
        tier["duration_label"] = format_duration(tier["duration_seconds"]) if tier["duration_seconds"] else None
    return render(
        request, "automod.html", guild_id, "automod",
        cfg=cfg,
        tiers=tiers,
        action_choices=list(AUTOMOD_ACTION_LABELS.items()),
        exempt_roles=[(rid, role_label(guild_id, rid)) for rid in db.list_automod_exempt_roles(guild_id)],
        invite_allow=db.list_automod_invite_allow(guild_id),
        invite_exempt_channels=[(cid, channel_label(guild_id, cid)) for cid in db.list_automod_invite_exempt_channels(guild_id)],
        invite_modes=[
            ("strike", "Delete + count as a strike (punishment ladder)"),
            ("delete", "Delete only (no strike)"),
            ("log", "Log only (leave the message up)"),
        ],
        text_channels=[c for c in db.list_bot_channels(guild_id) if c[2] in ("text", "news")],
        role_choices=db.list_bot_roles(guild_id),
        gif_allowlist=db.list_automod_gif_allowlist(guild_id),
        gif_blocklist=db.list_automod_gif_blocklist(guild_id),
    )


@app.post("/guild/{guild_id}/automod/enabled")
async def save_automod_enabled(request: Request, guild_id: int, enabled: str = Form(None)):
    if (r := await require_auth(request)):
        return r
    db.set_automod_enabled(guild_id, enabled == "on")
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/invites")
async def save_automod_invites(request: Request, guild_id: int, block_invites: str = Form(None)):
    if (r := await require_auth(request)):
        return r
    db.set_automod_invites(guild_id, block_invites == "on")
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/invites/settings")
async def save_automod_invite_settings(request: Request, guild_id: int, invite_mode: str = Form(...), allow_own: str = Form(None)):
    if (r := await require_auth(request)):
        return r
    try:
        db.set_automod_invite_mode(guild_id, invite_mode)
    except ValueError:
        return RedirectResponse(f"/guild/{guild_id}/automod?error=invitemode", status_code=303)
    db.set_automod_invite_allow_own(guild_id, allow_own == "on")
    db.record_bot_event("automod.invite_settings", guild_id, None, None,
                         f"mode={invite_mode} allow_own={allow_own == 'on'}", source="dashboard_automod")
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/invites/allow/add")
async def add_automod_invite_allow(request: Request, guild_id: int, entry: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    try:
        kind, value = db.add_automod_invite_allow(guild_id, entry)
    except ValueError:
        return RedirectResponse(f"/guild/{guild_id}/automod?error=inviteallow", status_code=303)
    db.record_bot_event("automod.invite_allow_add", guild_id, None, None,
                         f"{kind}={value}", source="dashboard_automod")
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/invites/allow/delete")
async def delete_automod_invite_allow(request: Request, guild_id: int, kind: str = Form(...), value: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    if kind in ("code", "guild"):
        db.remove_automod_invite_allow(guild_id, kind, value)
        db.record_bot_event("automod.invite_allow_remove", guild_id, None, None,
                             f"{kind}={value}", source="dashboard_automod")
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/invites/exempt-channels/add")
async def add_automod_invite_exempt_channel(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/automod?error=channel", status_code=303)
    db.add_automod_invite_exempt_channel(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/invites/exempt-channels/delete")
async def delete_automod_invite_exempt_channel(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_automod_invite_exempt_channel(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/gifs")
async def save_automod_gifs(request: Request, guild_id: int, block_gifs: str = Form(None)):
    if (r := await require_auth(request)):
        return r
    db.set_automod_block_gifs(guild_id, block_gifs == "on")
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/gif-allowlist/add")
async def add_automod_gif_allowlist(request: Request, guild_id: int, identifier: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    try: db.add_automod_gif_allowlist(guild_id, identifier)
    except ValueError: return RedirectResponse(f"/guild/{guild_id}/automod?error=gif", status_code=303)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/gif-allowlist/delete")
async def delete_automod_gif_allowlist(request: Request, guild_id: int, identifier: str = Form(...)):
    if (r := await require_auth(request)): return r
    try: db.remove_automod_gif_allowlist(guild_id, identifier)
    except ValueError: pass
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/gif-blocklist/add")
async def add_automod_gif_blocklist(request: Request, guild_id: int, identifier: str = Form(...)):
    if (r := await require_auth(request)): return r
    try: db.add_automod_gif_blocklist(guild_id, identifier)
    except ValueError: return RedirectResponse(f"/guild/{guild_id}/automod?error=gif", status_code=303)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/gif-blocklist/delete")
async def delete_automod_gif_blocklist(request: Request, guild_id: int, identifier: str = Form(...)):
    if (r := await require_auth(request)): return r
    try: db.remove_automod_gif_blocklist(guild_id, identifier)
    except ValueError: pass
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/words")
async def save_automod_words(request: Request, guild_id: int, words: str = Form("")):
    if (r := await require_auth(request)):
        return r
    db.set_automod_words(guild_id, words.split(","))
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/fuzzy-words")
async def save_automod_fuzzy_words(request: Request, guild_id: int, fuzzy_words: str = Form(None)):
    if (r := await require_auth(request)):
        return r
    db.set_automod_fuzzy_words(guild_id, fuzzy_words == "on")
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/queue-fuzzy")
async def save_automod_queue_fuzzy(request: Request, guild_id: int, queue_fuzzy_matches: str = Form(None)):
    if (r := await require_auth(request)):
        return r
    db.set_automod_queue_fuzzy(guild_id, queue_fuzzy_matches == "on")
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/caps")
async def save_automod_caps(request: Request, guild_id: int, percent: int = Form(...), min_len: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not 1 <= percent <= 100 or not 1 <= min_len <= 10_000:
        return RedirectResponse(f"/guild/{guild_id}/automod?error=invalid", status_code=303)
    db.set_automod_caps(guild_id, percent, min_len)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/mentions")
async def save_automod_mentions(request: Request, guild_id: int, threshold: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not 0 <= threshold <= 100:
        return RedirectResponse(f"/guild/{guild_id}/automod?error=invalid", status_code=303)
    db.set_automod_mentions(guild_id, threshold)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/spam")
async def save_automod_spam(request: Request, guild_id: int, count: int = Form(...), window_seconds: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not 0 <= count <= 1000 or not 1 <= window_seconds <= 86400:
        return RedirectResponse(f"/guild/{guild_id}/automod?error=invalid", status_code=303)
    db.set_automod_spam(guild_id, count, window_seconds)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/duplicates")
async def save_automod_duplicates(request: Request, guild_id: int, count: int = Form(...), window_seconds: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not 0 <= count <= 1000 or not 1 <= window_seconds <= 86400:
        return RedirectResponse(f"/guild/{guild_id}/automod?error=invalid", status_code=303)
    db.set_automod_duplicates(guild_id, count, window_seconds)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/escalation")
async def save_automod_escalation(request: Request, guild_id: int, window_seconds: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not 1 <= window_seconds <= 604800:
        return RedirectResponse(f"/guild/{guild_id}/automod?error=invalid", status_code=303)
    db.set_automod_violation_window(guild_id, window_seconds)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/escalation/add")
async def add_automod_escalation_tier(
    request: Request, guild_id: int, threshold: int = Form(...), action: str = Form(...),
    duration_value: str = Form(""), duration_unit: str = Form("m"),
):
    if (r := await require_auth(request)):
        return r
    if not 1 <= threshold <= 1000 or action not in AUTOMOD_ACTION_LABELS:
        return RedirectResponse(f"/guild/{guild_id}/automod?error=invalid", status_code=303)

    duration_seconds = None
    if action in AUTOMOD_TIMED_ACTIONS:
        unit_seconds = {
            "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800,
            "mo": 30 * 86400, "y": 365 * 86400,
        }.get(duration_unit)
        if not duration_value.strip().isdigit() or unit_seconds is None:
            return RedirectResponse(f"/guild/{guild_id}/automod?error=invalid", status_code=303)
        duration_seconds = int(duration_value) * unit_seconds
        max_seconds = AUTOMOD_MAX_TIMEOUT_SECONDS if action == "timeout" else 365 * 86400
        if not 1 <= duration_seconds <= max_seconds:
            return RedirectResponse(f"/guild/{guild_id}/automod?error=invalid", status_code=303)

    db.set_automod_escalation_tier(guild_id, threshold, action, duration_seconds)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/escalation/delete")
async def delete_automod_escalation_tier(request: Request, guild_id: int, tier_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_automod_escalation_tier(guild_id, tier_id)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/exempt-roles/add")
async def add_automod_exempt_role(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_role(guild_id, role_id):
        return RedirectResponse(f"/guild/{guild_id}/automod?error=role", status_code=303)
    db.add_automod_exempt_role(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.post("/guild/{guild_id}/automod/exempt-roles/delete")
async def delete_automod_exempt_role(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_automod_exempt_role(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/automod", status_code=303)


@app.get("/guild/{guild_id}/verification")
async def verification_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    cfg = db.get_verification_config(guild_id)
    recent = [
        {"user": member_label(guild_id, uid), "created_at": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M")}
        for uid, _actor_id, _reason, created_at in db.list_member_history_by_type(guild_id, "verify", 20)
    ]
    return render(
        request, "verification.html", guild_id, "verification",
        cfg=cfg,
        channel_label=channel_label(guild_id, cfg["channel_id"]),
        role_label=role_label(guild_id, cfg["role_id"]),
        text_channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"),
        roles=db.list_bot_roles(guild_id),
        recent=recent,
    )


@app.post("/guild/{guild_id}/verification")
async def save_verification(
    request: Request, guild_id: int, enabled: Optional[str] = Form(None),
    channel_id: int = Form(...), role_id: int = Form(...), message: str = Form(...),
):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/verification?error=channel", status_code=303)
    if not validate_role(guild_id, role_id):
        return RedirectResponse(f"/guild/{guild_id}/verification?error=role", status_code=303)
    message = message.strip()[:1000] or "Click the button below to verify and unlock the rest of the server."
    db.set_verification_config(guild_id, enabled == "on", channel_id, role_id, message)
    if enabled == "on":
        db.queue_verify_post(guild_id)
        await _wake("verify_post")
    return RedirectResponse(f"/guild/{guild_id}/verification?saved=1", status_code=303)


@app.get("/guild/{guild_id}/modmail")
async def modmail_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    cfg = db.get_modmail_config(guild_id)
    threads = [
        {
            "id": row[0], "channel_label": channel_label(guild_id, row[1]),
            "user_name": member_label(guild_id, row[2]), "status": row[3],
            "created_at": datetime.fromtimestamp(row[4]).strftime("%Y-%m-%d %H:%M"),
            "closed_at": datetime.fromtimestamp(row[5]).strftime("%Y-%m-%d %H:%M") if row[5] else None,
            "closed_by": member_label(guild_id, row[6]) if row[6] else None,
        }
        for row in db.list_modmail_threads(guild_id, limit=50)
    ]
    blocked = [
        {
            "user_id": user_id, "user_name": member_label(guild_id, user_id),
            "blocked_by": member_label(guild_id, blocked_by) if blocked_by else "Dashboard",
            "date_str": datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M"),
        }
        for user_id, blocked_by, created_at in db.list_modmail_blocks(guild_id)
    ]
    return render(
        request, "modmail.html", guild_id, "modmail",
        cfg=cfg, threads=threads, blocked=blocked,
        categories=db.list_bot_channels(guild_id, "category"),
        channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"),
    )


@app.post("/guild/{guild_id}/modmail/config")
async def modmail_config_route(
    request: Request, guild_id: int, enabled: str = Form(""), category_id: str = Form(""),
    log_channel_id: str = Form(""), anonymous_staff: str = Form(""),
):
    if (r := await require_auth(request)):
        return r
    cat_id = int(category_id) if category_id else None
    if cat_id and not validate_channel(guild_id, cat_id, ("category",)):
        cat_id = None
    log_id = int(log_channel_id) if log_channel_id else None
    if log_id and not validate_channel(guild_id, log_id, ("text", "news")):
        log_id = None
    db.set_modmail_config(guild_id, enabled=enabled == "on", category_id=cat_id, log_channel_id=log_id, anonymous_staff=anonymous_staff == "on")
    return RedirectResponse(f"/guild/{guild_id}/modmail?saved=1", status_code=303)


@app.post("/guild/{guild_id}/modmail/unblock")
async def modmail_unblock_route(request: Request, guild_id: int, user_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.unblock_modmail_user(guild_id, user_id)
    return RedirectResponse(f"/guild/{guild_id}/modmail", status_code=303)


@app.get("/guild/{guild_id}/rules")
async def rules_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    rules = [
        {"number": idx, "id": rule_id, "text": text}
        for idx, (rule_id, text) in enumerate(db.list_rules(guild_id), start=1)
    ]
    return render(request, "rules.html", guild_id, "rules", rules=rules)


@app.post("/guild/{guild_id}/rules/add")
async def add_rule_route(request: Request, guild_id: int, text: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    text = text.strip()
    if not text or len(text) > 500:
        return RedirectResponse(f"/guild/{guild_id}/rules?error=invalid", status_code=303)
    db.add_rule(guild_id, text)
    return RedirectResponse(f"/guild/{guild_id}/rules", status_code=303)


@app.post("/guild/{guild_id}/rules/delete")
async def delete_rule_route(request: Request, guild_id: int, rule_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.delete_rule(guild_id, rule_id)
    return RedirectResponse(f"/guild/{guild_id}/rules", status_code=303)


@app.get("/guild/{guild_id}/reports")
async def reports_page(request: Request, guild_id: int, status: str = "open"):
    if (r := await require_auth(request)):
        return r
    if status not in {"open", "reviewing", "resolved", "dismissed", "all"}:
        status = "open"
    rows = db.list_reports(guild_id, status=None if status == "all" else status, limit=100)
    reports = []
    for r in rows:
        reports.append({
            "id": r["id"],
            "reporter_name": member_label(guild_id, r["reporter_id"]),
            "target_name": member_label(guild_id, r["target_user_id"]),
            "target_user_id": r["target_user_id"],
            "reason": r["reason"],
            "status": r["status"],
            "status_label": REPORT_STATUS_LABELS.get(r["status"], r["status"]),
            "created_at": datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M"),
            "resolved_by_name": ("Dashboard" if r["resolved_by"] == 0 else member_label(guild_id, r["resolved_by"])) if r["resolved_by"] is not None else None,
            "resolution_note": r["resolution_note"],
            "linked_warn_id": r["linked_warn_id"],
        })
    cfg = db.get_report_config(guild_id)
    return render(
        request, "reports.html", guild_id, "reports",
        reports=reports, status_filter=status, status_choices=list(REPORT_STATUS_LABELS.items()),
        report_channel_label=channel_label(guild_id, cfg["channel_id"]) if cfg["channel_id"] else None,
        text_channels=db.list_bot_channels(guild_id, "text"), report_channel_id=cfg["channel_id"],
    )


@app.post("/guild/{guild_id}/reports/status")
async def set_report_status_route(request: Request, guild_id: int, report_id: int = Form(...), status: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    if status != "reviewing":
        return RedirectResponse(f"/guild/{guild_id}/reports?error=invalid", status_code=303)
    db.set_report_status(guild_id, report_id, status)
    return RedirectResponse(f"/guild/{guild_id}/reports", status_code=303)


@app.post("/guild/{guild_id}/reports/dismiss")
async def dismiss_report_route(request: Request, guild_id: int, report_id: int = Form(...), note: str = Form("")):
    if (r := await require_auth(request)):
        return r
    db.close_report(guild_id, report_id, "dismissed", 0, note.strip()[:500] or None)
    return RedirectResponse(f"/guild/{guild_id}/reports", status_code=303)


@app.post("/guild/{guild_id}/reports/resolve")
async def resolve_report_route(
    request: Request, guild_id: int, report_id: int = Form(...), note: str = Form(""),
    issue_warning: str = Form(None),
):
    if (r := await require_auth(request)):
        return r
    report = db.get_report(guild_id, report_id)
    if report is None:
        return RedirectResponse(f"/guild/{guild_id}/reports?error=notfound", status_code=303)
    note = note.strip()[:500]
    linked_warn_id = None
    if issue_warning == "on":
        # Resolving a report by issuing a warning uses the exact same
        # add_warn path /warn and the dashboard's own "add warning" form
        # use, so it shows up in that member's warning history and in
        # staff_action_counts like any other warning - not a parallel
        # record that could drift out of sync with the real one.
        reason = note or report["reason"]
        linked_warn_id = db.add_warn(guild_id, report["target_user_id"], 0, reason, int(time.time()))
        queue_warn_escalation_if_due(guild_id, report["target_user_id"], reason)
        # queue_warn_escalation_if_due may have queued a mod action (mute/
        # timeout/kick/ban/tempban) onto dashboard_mod_actions - wake the bot
        # the same way add_warn_route does, so it isn't left to the next poll.
        await _wake("mod_action")
    db.close_report(guild_id, report_id, "resolved", 0, note or None, linked_warn_id)
    return RedirectResponse(f"/guild/{guild_id}/reports", status_code=303)


@app.post("/guild/{guild_id}/reports/channel")
async def set_report_channel_route(request: Request, guild_id: int, channel_id: str = Form("")):
    if (r := await require_auth(request)):
        return r
    if not channel_id.strip():
        db.set_report_channel(guild_id, None)
        return RedirectResponse(f"/guild/{guild_id}/reports", status_code=303)
    cid = int(channel_id)
    if not validate_channel(guild_id, cid, ("text",)):
        return RedirectResponse(f"/guild/{guild_id}/reports?error=channel", status_code=303)
    db.set_report_channel(guild_id, cid)
    return RedirectResponse(f"/guild/{guild_id}/reports", status_code=303)


@app.get("/guild/{guild_id}/moderationqueue")
async def moderationqueue_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    rows = db.list_automod_queue(guild_id, status="pending", limit=100)
    queue = [
        {
            "id": row["id"],
            "channel_label": channel_label(guild_id, row["channel_id"]),
            "user_name": member_label(guild_id, row["user_id"]),
            "rule_label": row["rule_label"],
            "content_snapshot": row["content_snapshot"],
            "created_at": datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d %H:%M"),
        }
        for row in rows
    ]
    return render(request, "moderationqueue.html", guild_id, "moderationqueue", queue=queue)


@app.post("/guild/{guild_id}/moderationqueue/decide")
async def moderationqueue_decide_route(request: Request, guild_id: int, review_id: int = Form(...), decision: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    if decision not in {"confirm", "dismiss"}:
        return RedirectResponse(f"/guild/{guild_id}/moderationqueue?error=invalid", status_code=303)
    review = db.get_automod_review(guild_id, review_id)
    if review is None or review["status"] != "pending":
        return RedirectResponse(f"/guild/{guild_id}/moderationqueue?error=notfound", status_code=303)
    # moderator_id 0 is the dashboard sentinel, same convention as every
    # other dashboard-attributed action in this codebase.
    if decision == "dismiss":
        db.queue_automod_decision(guild_id, review_id, "dismiss", 0)
        await _wake("automod")
        return RedirectResponse(f"/guild/{guild_id}/moderationqueue?dismissqueued={review_id}", status_code=303)
    # Confirming needs to actually apply the escalation ladder, which needs
    # a live Discord connection the dashboard process doesn't have - so
    # it's queued and the bot's poller applies it (see automod.py's
    # _poll_queue_decisions), same bridge pattern as every other
    # WebUI->Discord action.
    db.queue_automod_decision(guild_id, review_id, "confirm", 0)
    await _wake("automod")
    return RedirectResponse(f"/guild/{guild_id}/moderationqueue?confirmqueued={review_id}", status_code=303)


@app.get("/guild/{guild_id}/emergency")
async def emergency_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    lockdown_state = db.get_lockdown_state(guild_id)
    recent = []
    for row in db.recent_emergency_requests(guild_id, 20):
        recent.append({
            "id": row["id"],
            "action": row["action"],
            "status": row["status"],
            "created_at": datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d %H:%M"),
            "error": row["error"],
            "result": row["result"],
        })
    roles = [{"id": rid, "name": name} for rid, name, _pos in db.list_bot_roles(guild_id)]
    return render(
        request, "emergency.html", guild_id, "emergency",
        locked=lockdown_state is not None,
        locked_channel_count=len(lockdown_state["channel_overwrites"]) if lockdown_state else 0,
        locked_since=datetime.fromtimestamp(lockdown_state["started_at"]).strftime("%Y-%m-%d %H:%M") if lockdown_state else None,
        roles=roles,
        recent=recent,
    )


@app.post("/guild/{guild_id}/emergency/lockdown")
async def emergency_lockdown_route(request: Request, guild_id: int, confirm_text: str = Form("")):
    if (r := await require_auth(request)):
        return r
    if not _check_confirm_phrase("lockdown", confirm_text):
        return RedirectResponse(f"/guild/{guild_id}/emergency?error=confirm", status_code=303)
    # actor 0: no per-admin dashboard login to attribute this to, same
    # sentinel used by every other dashboard-issued action in this codebase.
    db.queue_emergency_request(guild_id, "lockdown", {"started_by": 0})
    await _wake("emergency")
    return RedirectResponse(f"/guild/{guild_id}/emergency?queued=lockdown", status_code=303)


@app.post("/guild/{guild_id}/emergency/unlock")
async def emergency_unlock_route(request: Request, guild_id: int, confirm_text: str = Form("")):
    if (r := await require_auth(request)):
        return r
    if not _check_confirm_phrase("unlock", confirm_text):
        return RedirectResponse(f"/guild/{guild_id}/emergency?error=confirm", status_code=303)
    db.queue_emergency_request(guild_id, "unlock", {})
    await _wake("emergency")
    return RedirectResponse(f"/guild/{guild_id}/emergency?queued=unlock", status_code=303)


@app.post("/guild/{guild_id}/emergency/revoke-invites")
async def emergency_revoke_invites_route(request: Request, guild_id: int, confirm_text: str = Form("")):
    if (r := await require_auth(request)):
        return r
    if not _check_confirm_phrase("revoke_invites", confirm_text):
        return RedirectResponse(f"/guild/{guild_id}/emergency?error=confirm", status_code=303)
    db.queue_emergency_request(guild_id, "revoke_invites", {})
    await _wake("emergency")
    return RedirectResponse(f"/guild/{guild_id}/emergency?queued=revoke_invites", status_code=303)


@app.post("/guild/{guild_id}/emergency/mass-timeout")
async def emergency_mass_timeout_route(
    request: Request, guild_id: int, role_id: int = Form(...), duration_minutes: int = Form(...),
    reason: str = Form(""), confirm_text: str = Form(""),
):
    if (r := await require_auth(request)):
        return r
    if not _check_confirm_phrase("mass_timeout", confirm_text):
        return RedirectResponse(f"/guild/{guild_id}/emergency?error=confirm", status_code=303)
    if not validate_role(guild_id, role_id) or duration_minutes < 1 or duration_minutes > 40320:  # 28 days
        return RedirectResponse(f"/guild/{guild_id}/emergency?error=masstimeout", status_code=303)
    db.queue_emergency_request(guild_id, "mass_timeout", {
        "role_id": role_id, "duration_seconds": duration_minutes * 60, "reason": reason.strip()[:500],
    })
    await _wake("emergency")
    return RedirectResponse(f"/guild/{guild_id}/emergency?queued=mass_timeout", status_code=303)
