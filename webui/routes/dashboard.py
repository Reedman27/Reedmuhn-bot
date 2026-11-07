"""Routes for the dashboard section of the dashboard (mirrors cogs/dashboard/).

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

@app.get("/guild/{guild_id}/analytics")
async def analytics_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    request.session["guild_id"] = guild_id

    days = _parse_days(request)
    since = int(time.time()) - days * 86400
    server = db.get_server_counts(guild_id)
    activity = db.get_activity_stats(guild_id, since)
    return render(
        request,
        "analytics.html",
        guild_id,
        "analytics",
        stats={**server, **activity},
        days=days,
        analytics_settings=db.get_analytics_settings(guild_id),
    )


@app.post("/guild/{guild_id}/analytics/settings")
async def analytics_settings(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    allowed = set(db.get_analytics_settings(guild_id))
    form = await request.form()
    # Checkboxes are intentionally explicit: unchecked means disabled.
    for setting in allowed:
        db.set_analytics_setting(guild_id, setting, setting in form)
    return RedirectResponse(f"/guild/{guild_id}/analytics", status_code=303)


@app.get("/guild/{guild_id}/analytics/{metric}")
async def analytics_detail(request: Request, guild_id: int, metric: str):
    if (r := await require_auth(request)):
        return r
    request.session["guild_id"] = guild_id

    meta = ANALYTICS_METRICS.get(metric)
    if meta is None:
        return RedirectResponse(f"/guild/{guild_id}/analytics", status_code=303)

    days = _parse_days(request)
    since = int(time.time()) - days * 86400

    breakdown = None
    events = None
    members = None

    if meta["kind"] == "timeseries":
        event_type = meta["event_type"]
        counts = dict(db.get_daily_activity_counts(guild_id, since, event_type))
        breakdown = []
        for offset in range(days - 1, -1, -1):
            day = datetime.fromtimestamp(int(time.time()) - offset * 86400).strftime("%Y-%m-%d")
            breakdown.append({"day": day, "count": counts.get(day, 0)})
        max_count = max((row["count"] for row in breakdown), default=0)
        for row in breakdown:
            row["pct"] = round(100 * row["count"] / max_count) if max_count else 0

        # Which timeseries metrics get a "who/what recently" list below the
        # chart - skipped for messages/commands since message content isn't
        # stored, so there's nothing more informative to show than the chart
        # already gives.
        if event_type in ("member.join", "member.leave", "message.edited", "message.deleted",
                           "reaction.added", "voice.join", "voice.leave"):
            raw_events = db.list_recent_events(guild_id, since, event_type, limit=25)
            events = []
            for created_at, actor_id, target_id, details in raw_events:
                detail_data = {}
                if details:
                    try:
                        detail_data = json.loads(details)
                    except (json.JSONDecodeError, TypeError):
                        detail_data = {}

                if event_type in ("member.join", "member.leave"):
                    name = detail_data.get("member") or (member_label(guild_id, target_id) if target_id else "Unknown member")
                    context = None
                else:
                    name = member_label(guild_id, actor_id) if actor_id else "Unknown member"
                    channel_id = detail_data.get("channel_id")
                    context = channel_label(guild_id, channel_id) if channel_id else None

                events.append({
                    "when": datetime.fromtimestamp(created_at).strftime("%b %d, %Y %H:%M"),
                    "name": name,
                    "context": context,
                })
    elif metric in ("members", "online"):
        members = db.list_bot_members_with_status(guild_id)
        if metric == "online":
            members = [m for m in members if m[3] in ("online", "idle", "dnd")]

    channels = db.list_bot_channels(guild_id) if metric == "channels" else None
    roles = db.list_bot_roles(guild_id) if metric == "roles" else None

    return render(
        request,
        "analytics_detail.html",
        guild_id,
        "analytics",
        metric=metric,
        meta=meta,
        days=days,
        breakdown=breakdown,
        events=events,
        members=members,
        channels=channels,
        roles=roles,
    )


@app.get("/guild/{guild_id}/access")
async def access_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    _require_can_grant(request)
    people = [
        {
            "user_id": uid, "level": level,
            "name": member_label(guild_id, int(uid)) if uid.isdigit() else uid,
            "added_by": added_by, "added_at": datetime.fromtimestamp(added_at).strftime("%Y-%m-%d"),
        }
        for uid, level, added_by, added_at in db.list_dashboard_access(guild_id)
    ]
    return render(
        request, "access.html", guild_id, "access",
        people=people, managers_allowed=db.get_dashboard_managers_allowed(guild_id),
        error=request.query_params.get("error"),
        staff_pages=sorted(p for p in STAFF_PAGES if p),
    )


@app.post("/guild/{guild_id}/access/add")
async def access_add(request: Request, guild_id: int, member: str = Form(...), level: str = Form("staff")):
    if (r := await require_auth(request)):
        return r
    _require_can_grant(request)
    if level not in ("full", "staff"):
        return RedirectResponse(f"/guild/{guild_id}/access?error=level", status_code=303)
    user_id = db.find_member_id(guild_id, member)
    if user_id is None:
        return RedirectResponse(f"/guild/{guild_id}/access?error=member", status_code=303)
    actor_id, actor_name = _current_identity(request)
    db.set_dashboard_access(guild_id, str(user_id), level, actor_name or actor_id)
    _guild_cache.clear()  # take effect now, not after the 60s cache window
    return RedirectResponse(f"/guild/{guild_id}/access", status_code=303)


@app.post("/guild/{guild_id}/access/remove")
async def access_remove(request: Request, guild_id: int, user_id: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    _require_can_grant(request)
    db.remove_dashboard_access(guild_id, user_id)
    _guild_cache.clear()
    return RedirectResponse(f"/guild/{guild_id}/access", status_code=303)


@app.post("/guild/{guild_id}/access/managers")
async def access_managers(request: Request, guild_id: int, allowed: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    _require_can_grant(request)
    db.set_dashboard_managers_allowed(guild_id, allowed == "1")
    _guild_cache.clear()
    return RedirectResponse(f"/guild/{guild_id}/access", status_code=303)


@app.get("/guild/{guild_id}/quickstart")
async def quickstart_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    return render(request, "quickstart.html", guild_id, "quickstart")


@app.get("/guild/{guild_id}/feed")
async def feed_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    return render(
        request, "feed.html", guild_id, "feed",
        enabled=db.get_feed_enabled(guild_id),
        channels=db.list_feed_channels(guild_id),
    )


@app.post("/guild/{guild_id}/feed/toggle")
async def toggle_feed(request: Request, guild_id: int, enabled: str = Form(None)):
    if (r := await require_auth(request)):
        return r
    db.set_feed_enabled(guild_id, enabled == "on")
    return RedirectResponse(f"/guild/{guild_id}/feed", status_code=303)


@app.get("/guild/{guild_id}/feed/{channel_id}")
async def feed_channel_page(request: Request, guild_id: int, channel_id: int):
    if (r := await require_auth(request)):
        return r
    messages = db.get_feed_messages(guild_id, channel_id)
    return render(
        request, "feed_channel.html", guild_id, "feed",
        channel_id=channel_id,
        channel_name=db.get_channel_name(guild_id, channel_id) or f"deleted-channel-{channel_id}",
        messages=messages,
        last_id=messages[-1]["id"] if messages else 0,
        enabled=db.get_feed_enabled(guild_id),
    )


@app.get("/guild/{guild_id}/feed/{channel_id}/poll")
async def feed_channel_poll(request: Request, guild_id: int, channel_id: int, after: int = 0):
    if (r := await require_auth(request)):
        return r
    return {"messages": db.get_feed_messages_after(guild_id, channel_id, after)}


@app.get("/guild/{guild_id}/talk")
async def talk_page(request: Request, guild_id: int, show_deleted: str = ""):
    if (r := await require_auth(request)):
        return r
    show_deleted_bool = show_deleted == "1"
    recent_rows = db.recent_outbound_messages(guild_id, 20, include_deleted=show_deleted_bool)
    recent_messages = [
        {
            "id": row[0], "channel_id": row[1],
            "channel_name": db.get_channel_name(guild_id, row[1]) or f"Deleted channel ({row[1]})",
            "content": row[2], "status": row[3], "attempts": row[4],
            "created_at": row[5], "sent_at": row[6], "failed_at": row[7],
            "last_error": row[8], "discord_message_id": row[9], "deleted_at": row[10],
        }
        for row in recent_rows
    ]
    return render(request, "talk.html", guild_id, "talk",
                  text_channels=db.list_bot_channels(guild_id, "text"),
                  recent_messages=recent_messages, show_deleted=show_deleted_bool)


@app.post("/guild/{guild_id}/talk/retry")
async def retry_talk_message(request: Request, guild_id: int, message_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    row = db.get_outbound_message(message_id)
    if row is None or row[1] != guild_id:
        return RedirectResponse(f"/guild/{guild_id}/talk?error=notfound", status_code=303)
    if db.retry_outbound_message(message_id):
        try:
            db.record_bot_event("dashboard.talk.retried", guild_id, None, row[2], f"message_id={message_id}", source="dashboard_talk")
        except Exception:
            pass
        return RedirectResponse(f"/guild/{guild_id}/talk?retried={message_id}", status_code=303)
    return RedirectResponse(f"/guild/{guild_id}/talk?error=notretryable", status_code=303)


@app.post("/guild/{guild_id}/talk/delete")
async def delete_talk_message(request: Request, guild_id: int, message_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    row = db.get_outbound_message(message_id)
    if row is None or row[1] != guild_id:
        return RedirectResponse(f"/guild/{guild_id}/talk?error=notfound", status_code=303)
    if db.request_message_delete(guild_id, message_id):
        await _wake("message_delete")
        try:
            db.record_bot_event("dashboard.talk.delete_requested", guild_id, None, row[2], f"message_id={message_id}", source="dashboard_talk")
        except Exception:
            pass
        return RedirectResponse(f"/guild/{guild_id}/talk?delete_queued={message_id}", status_code=303)
    return RedirectResponse(f"/guild/{guild_id}/talk?error=notdeletable", status_code=303)


@app.post("/guild/{guild_id}/talk")
async def send_talk_message(request: Request, guild_id: int, channel_id: int = Form(...), content: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    content = content.strip()
    if not content:
        return RedirectResponse(f"/guild/{guild_id}/talk?error=empty", status_code=303)
    if len(content) > 2000:
        return RedirectResponse(f"/guild/{guild_id}/talk?error=length", status_code=303)
    if not validate_channel(guild_id, channel_id, ("text",)):
        return RedirectResponse(f"/guild/{guild_id}/talk?error=channel", status_code=303)
    try:
        message_id = db.queue_outbound_message(guild_id, channel_id, content)
        await _wake("outbound_message")
        try:
            db.record_bot_event("dashboard.talk.queued", guild_id, None, channel_id,
                                f"message_id={message_id} content_length={len(content)}", source="dashboard_talk")
        except Exception:
            pass
    except ValueError as exc:
        error = "length" if "2000" in str(exc) else "empty"
        return RedirectResponse(f"/guild/{guild_id}/talk?error={error}", status_code=303)
    return RedirectResponse(f"/guild/{guild_id}/talk?queued={message_id}", status_code=303)


@app.get("/guild/{guild_id}/staffstats")
async def staffstats_page(request: Request, guild_id: int, window: str = "30"):
    if (r := await require_auth(request)):
        return r
    since = None
    if window != "all":
        try:
            days = int(window)
            since = int(time.time()) - days * 86400
        except ValueError:
            window = "30"
            since = int(time.time()) - 30 * 86400
    counts = db.staff_action_counts(guild_id, since=since)
    staff = []
    for moderator_id, c in counts.items():
        total = c["warns"] + c["tickets_closed"] + c["reports_resolved"]
        if total == 0:
            continue
        staff.append({
            "moderator_id": moderator_id,
            "moderator_name": "Dashboard" if moderator_id == 0 else member_label(guild_id, moderator_id),
            "warns": c["warns"], "tickets_closed": c["tickets_closed"], "reports_resolved": c["reports_resolved"],
            "total": total,
        })
    staff.sort(key=lambda s: s["total"], reverse=True)
    return render(request, "staffstats.html", guild_id, "staffstats", staff=staff, window=window)


@app.get("/guild/{guild_id}/search")
async def search_page(request: Request, guild_id: int, q: str = ""):
    if (r := await require_auth(request)):
        return r
    q = q.strip()
    results = None
    if q:
        raw = db.search_all(guild_id, q)
        results = {
            "warns": [
                {"id": row[0], "user_name": member_label(guild_id, row[1]), "moderator_name": ("Dashboard" if row[2] == 0 else member_label(guild_id, row[2])), "reason": row[3], "created_at": datetime.fromtimestamp(row[4]).strftime("%Y-%m-%d %H:%M")}
                for row in raw["warns"]
            ],
            "reports": [
                {"id": row[0], "reporter_name": member_label(guild_id, row[1]), "target_name": member_label(guild_id, row[2]), "reason": row[3], "status": row[4], "created_at": datetime.fromtimestamp(row[5]).strftime("%Y-%m-%d %H:%M")}
                for row in raw["reports"]
            ],
            "rules": [
                {"number": db.rule_number_for_id(guild_id, row[0]), "text": row[1]}
                for row in raw["rules"]
            ],
            "tickets": [
                {"id": row[0], "channel_label": channel_label(guild_id, row[1]), "opener_name": member_label(guild_id, row[2]), "subject": row[3], "status": row[4], "created_at": datetime.fromtimestamp(row[5]).strftime("%Y-%m-%d %H:%M")}
                for row in raw["tickets"]
            ],
            "polls": [
                {"id": row[0], "channel_label": channel_label(guild_id, row[1]), "question": row[2], "closed": bool(row[3]), "created_at": datetime.fromtimestamp(row[4]).strftime("%Y-%m-%d %H:%M")}
                for row in raw["polls"]
            ],
            "automod_queue": [
                {"id": row[0], "channel_label": channel_label(guild_id, row[1]), "user_name": member_label(guild_id, row[2]), "rule_label": row[3], "content_snapshot": row[4], "created_at": datetime.fromtimestamp(row[5]).strftime("%Y-%m-%d %H:%M"), "status": row[6]}
                for row in raw["automod_queue"]
            ],
        }
        results["total"] = sum(len(v) for v in results.values())
    return render(request, "search.html", guild_id, "search", query=q, results=results)


@app.get("/guild/{guild_id}/snapshots")
async def snapshots_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    snapshots = [
        {
            "id": s["id"], "name": s["name"],
            "created_at": datetime.fromtimestamp(s["created_at"]).strftime("%Y-%m-%d %H:%M"),
        }
        for s in db.list_config_snapshots(guild_id, 50)
    ]

    compare_a = request.query_params.get("a")
    compare_b = request.query_params.get("b")
    compare_result = None
    if compare_a and compare_b:
        def _resolve(ref: str):
            if ref == "current":
                return "Current live settings", db.capture_config_snapshot_data(guild_id)
            snap = db.get_config_snapshot(guild_id, int(ref))
            return (snap["name"], snap["data"]) if snap else (None, None)

        name_a, data_a = _resolve(compare_a)
        name_b, data_b = _resolve(compare_b)
        if data_a is not None and data_b is not None:
            compare_result = {
                "name_a": name_a, "name_b": name_b,
                "changes": diff_snapshot_data(data_a, data_b),
            }

    return render(
        request, "snapshots.html", guild_id, "snapshots", snapshots=snapshots,
        compare_result=compare_result, compare_a=compare_a, compare_b=compare_b,
    )


@app.post("/guild/{guild_id}/snapshots/create")
async def create_snapshot_route(request: Request, guild_id: int, name: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    name = name.strip()[:80]
    if not name:
        return RedirectResponse(f"/guild/{guild_id}/snapshots?error=name", status_code=303)
    data = db.capture_config_snapshot_data(guild_id)
    # actor 0: same "no per-admin login" sentinel used everywhere else here.
    db.create_config_snapshot(guild_id, name, data, 0)
    return RedirectResponse(f"/guild/{guild_id}/snapshots?created=1", status_code=303)


@app.post("/guild/{guild_id}/snapshots/restore")
async def restore_snapshot_route(request: Request, guild_id: int, snapshot_id: int = Form(...), confirm_text: str = Form("")):
    if (r := await require_auth(request)):
        return r
    if confirm_text.strip().upper() != "RESTORE":
        return RedirectResponse(f"/guild/{guild_id}/snapshots?error=confirm", status_code=303)
    snapshot = db.get_config_snapshot(guild_id, snapshot_id)
    if snapshot is None:
        return RedirectResponse(f"/guild/{guild_id}/snapshots?error=notfound", status_code=303)
    db.restore_config_snapshot_data(guild_id, snapshot["data"])
    return RedirectResponse(f"/guild/{guild_id}/snapshots?restored={snapshot_id}", status_code=303)


@app.post("/guild/{guild_id}/snapshots/delete")
async def delete_snapshot_route(request: Request, guild_id: int, snapshot_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.delete_config_snapshot(guild_id, snapshot_id)
    return RedirectResponse(f"/guild/{guild_id}/snapshots?deleted=1", status_code=303)
