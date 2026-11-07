"""Routes for the utility section of the dashboard (mirrors cogs/utility/).

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

@app.get("/guild/{guild_id}/commands")
async def commands_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    return render(
        request, "commands.html", guild_id, "commands",
        custom_commands=db.list_custom_commands(guild_id),
    )


@app.post("/guild/{guild_id}/custom-commands/add")
async def add_custom_command(request: Request, guild_id: int, trigger: str = Form(...), response: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    trigger = trigger.strip()
    response = response.strip()
    if not trigger or len(trigger) > 100 or len(response) > 2000:
        return RedirectResponse(f"/guild/{guild_id}/commands?error=invalid", status_code=303)
    db.add_custom_command(guild_id, trigger, response)
    return RedirectResponse(f"/guild/{guild_id}/commands", status_code=303)


@app.post("/guild/{guild_id}/custom-commands/delete")
async def delete_custom_command(request: Request, guild_id: int, trigger: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_custom_command(guild_id, trigger)
    return RedirectResponse(f"/guild/{guild_id}/commands", status_code=303)


@app.get("/guild/{guild_id}/scheduled")
async def scheduled_page(request: Request, guild_id: int, nick_q: str = ""):
    if (r := await require_auth(request)):
        return r

    reminders = []
    for event_id, _, run_at, data in db.list_scheduled_events(guild_id, "reminder"):
        parsed = json.loads(data)
        reminders.append({
            "id": event_id,
            "user_id": parsed["user_id"],
            "user_name": member_label(guild_id, parsed["user_id"]),
            "message": parsed["message"],
            "run_at": datetime.fromtimestamp(run_at).strftime("%Y-%m-%d %H:%M"),
        })

    nick_q = nick_q.strip()
    nick_reverts = []
    for event_id, _, run_at, data in db.list_scheduled_events(guild_id, "revert_nick"):
        parsed = json.loads(data)
        user_id = parsed["user_id"]
        user_name = member_label(guild_id, user_id)
        if nick_q and nick_q != str(user_id) and nick_q.lower() not in user_name.lower():
            continue
        nick_reverts.append({
            "id": event_id,
            "user_id": user_id,
            "user_name": user_name,
            "original_nick": parsed.get("original_nick"),
            "reverts_at": datetime.fromtimestamp(run_at).strftime("%Y-%m-%d %H:%M"),
        })

    return render(
        request, "scheduled.html", guild_id, "scheduled",
        reminders=reminders, nick_reverts=nick_reverts, nick_q=nick_q,
    )


@app.post("/guild/{guild_id}/scheduled/cancel-reminder")
async def cancel_reminder(request: Request, guild_id: int, event_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.delete_scheduled_event(event_id, guild_id)
    return RedirectResponse(f"/guild/{guild_id}/scheduled", status_code=303)


@app.post("/guild/{guild_id}/scheduled/revert-nick-now")
async def revert_nick_now(request: Request, guild_id: int, event_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    event = db.get_scheduled_event(event_id, guild_id, "revert_nick")
    if event:
        _event_id, _name, _run_at, data = event
        db.delete_scheduled_event(event_id, guild_id)
        parsed = json.loads(data)
        # Same pattern as the tempbans tab's "unban now" - reinsert the
        # same event with an immediate run_at rather than performing the
        # Discord edit here, since the WebUI process has no Discord
        # connection of its own. The scheduler loop picks it up on its
        # next tick (every 30s).
        db.insert_scheduled_event(
            "revert_nick", guild_id, int(time.time()),
            {"user_id": parsed["user_id"], "original_nick": parsed.get("original_nick")},
        )
    return RedirectResponse(f"/guild/{guild_id}/scheduled?reverted={event_id}", status_code=303)


@app.get("/guild/{guild_id}/permissions")
async def permissions_page(request: Request, guild_id: int, tab: str = "access"):
    if (r := await require_auth(request)):
        return r
    if tab not in {"access", "scanner"}:
        tab = "access"
    return render(
        request, "permissions.html", guild_id, "permissions",
        tab=tab,
        mode=db.get_tempnick_mode(guild_id),
        roles=[(rid, role_label(guild_id, rid)) for rid in db.list_tempnick_roles(guild_id)],
        role_choices=db.list_bot_roles(guild_id),
        bot_manager_roles=[(rid, role_label(guild_id, rid)) for rid in db.list_bot_manager_roles(guild_id)],
        scan_findings=scan_guild_permissions(guild_id) if tab == "scanner" else [],
    )


@app.post("/guild/{guild_id}/permissions/bot-manager-roles/add")
async def add_bot_manager_role(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_role(guild_id, role_id):
        return RedirectResponse(f"/guild/{guild_id}/permissions?error=role", status_code=303)
    db.add_bot_manager_role(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/permissions", status_code=303)


@app.post("/guild/{guild_id}/permissions/bot-manager-roles/delete")
async def delete_bot_manager_role(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_bot_manager_role(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/permissions", status_code=303)


@app.post("/guild/{guild_id}/permissions/tempnick-mode")
async def save_tempnick_mode(request: Request, guild_id: int, mode: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    if mode in ("everyone", "allowlist", "denylist"):
        db.set_tempnick_mode(guild_id, mode)
    return RedirectResponse(f"/guild/{guild_id}/permissions", status_code=303)


@app.post("/guild/{guild_id}/permissions/tempnick-roles/add")
async def add_tempnick_role(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_role(guild_id, role_id):
        return RedirectResponse(f"/guild/{guild_id}/permissions?error=role", status_code=303)
    db.add_tempnick_role(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/permissions", status_code=303)


@app.post("/guild/{guild_id}/permissions/tempnick-roles/delete")
async def delete_tempnick_role(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_tempnick_role(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/permissions", status_code=303)


@app.get("/guild/{guild_id}/ai")
async def ai_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    cfg = db.get_ai_config(guild_id)
    # Never put the real credential into a template. The form uses a masked
    # placeholder and only replaces the stored key when the admin supplies a
    # new one.
    view = dict(cfg)
    view["api_key_set"] = bool(cfg["api_key"])
    view["api_key"] = ""
    return render(request, "ai.html", guild_id, "ai", cfg=view)


@app.post("/guild/{guild_id}/ai/save")
async def ai_save(
    request: Request, guild_id: int,
    enabled: str = Form(""), provider: str = Form("openai_compatible"),
    base_url: str = Form("https://api.openai.com/v1"), api_key: str = Form(""),
    model: str = Form("gpt-4o-mini"), system_prompt: str = Form(""),
    max_tokens: int = Form(800), temperature: float = Form(0.7),
    use_channel_context: str = Form(""), context_message_limit: int = Form(10),
    index_channels: str = Form(""), index_message_limit: int = Form(500),
):
    if (r := await require_auth(request)):
        return r
    provider = provider.strip() or "openai_compatible"
    allowed_providers = {"openai_compatible", "openai", "groq", "ollama", "custom"}
    if provider not in allowed_providers:
        provider = "openai_compatible"
    base_url = base_url.strip().rstrip("/") or "https://api.openai.com/v1"
    model = model.strip()[:200] or "gpt-4o-mini"
    system_prompt = system_prompt.strip()[:2000] or "You are ReedMuhn, a helpful Discord assistant. Be concise and follow the server context when provided."
    old = db.get_ai_config(guild_id)
    if not api_key.strip():
        api_key = old["api_key"]
    db.set_ai_config(
        guild_id,
        enabled=enabled == "on",
        provider=provider,
        base_url=base_url,
        api_key=api_key.strip()[:500],
        model=model,
        system_prompt=system_prompt,
        max_tokens=max(64, min(4000, max_tokens)),
        temperature=max(0.0, min(2.0, temperature)),
        use_channel_context=use_channel_context == "on",
        context_message_limit=max(1, min(30, context_message_limit)),
        index_channels=index_channels == "on",
        index_message_limit=max(50, min(5000, index_message_limit)),
    )
    return RedirectResponse(f"/guild/{guild_id}/ai?saved=1", status_code=303)


@app.get("/guild/{guild_id}/tempvoice")
async def tempvoice_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    hubs = db.list_voice_hubs(guild_id)
    return render(
        request, "tempvoice.html", guild_id, "tempvoice",
        hubs=[(cid, channel_label(guild_id, cid), user_limit) for cid, user_limit in hubs],
        active_channels=[
            (cid, member_label(guild_id, owner_id), channel_label(guild_id, cid), user_limit)
            for cid, owner_id, user_limit in db.list_temp_voice_channels(guild_id)
        ],
        voice_channels=db.list_bot_channels(guild_id, "voice"),
        settings=db.get_temp_voice_settings(guild_id),
        owner_perm_options=[
            (name, label, bool(db.get_temp_voice_settings(guild_id)["owner_perms"] & bit))
            for name, (bit, label) in TEMP_VOICE_OWNER_PERMS.items()
        ],
        text_channels=[c for c in db.list_bot_channels(guild_id) if c[2] in ("text", "news")],
    )


@app.post("/guild/{guild_id}/tempvoice/settings")
async def save_tempvoice_settings(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    form = await request.form()
    owner_perms = 0
    for name, (bit, _label) in TEMP_VOICE_OWNER_PERMS.items():
        if form.get(f"perm_{name}") == "on":
            owner_perms |= bit
    log_raw = str(form.get("log_channel_id", "") or "").strip()
    log_channel_id = 0
    if log_raw:
        if not log_raw.isdigit() or not validate_channel(guild_id, int(log_raw), ("text", "news")):
            return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=channel", status_code=303)
        log_channel_id = int(log_raw)
    try:
        max_channels = int(str(form.get("max_channels", "25")))
    except ValueError:
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=invalid", status_code=303)
    if not 1 <= max_channels <= 100:
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=invalid", status_code=303)
    db.set_temp_voice_settings(
        guild_id, owner_perms=owner_perms, create_role=form.get("create_role") == "on",
        role_hoist=form.get("role_hoist") == "on", log_channel_id=log_channel_id,
        name_template=str(form.get("name_template", "") or ""), max_channels=max_channels,
    )
    return RedirectResponse(f"/guild/{guild_id}/tempvoice?saved=1", status_code=303)


@app.post("/guild/{guild_id}/tempvoice/hub")
async def save_voice_hub(request: Request, guild_id: int, channel_id: int = Form(...), user_limit: int = Form(0)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("voice",)):
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=channel", status_code=303)
    if not 0 <= user_limit <= 99:
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=invalid", status_code=303)
    db.add_voice_hub(guild_id, channel_id, user_limit)
    return RedirectResponse(f"/guild/{guild_id}/tempvoice", status_code=303)


@app.post("/guild/{guild_id}/tempvoice/hub/limit")
async def update_voice_hub_limit_route(request: Request, guild_id: int, channel_id: int = Form(...), user_limit: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not 0 <= user_limit <= 99:
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=invalid", status_code=303)
    if not db.set_voice_hub_limit(guild_id, channel_id, user_limit):
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=not_found", status_code=303)
    return RedirectResponse(f"/guild/{guild_id}/tempvoice?hublimit=1", status_code=303)


@app.post("/guild/{guild_id}/tempvoice/delete")
async def delete_tempvoice_route(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not db.is_temp_voice_channel(channel_id, guild_id):
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=not_found", status_code=303)
    if not db.request_temp_voice_delete(guild_id, channel_id):
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=not_found", status_code=303)
    await _wake("tempvoice_delete")
    return RedirectResponse(f"/guild/{guild_id}/tempvoice?requested=1", status_code=303)


@app.post("/guild/{guild_id}/tempvoice/limit")
async def queue_tempvoice_limit_route(request: Request, guild_id: int, channel_id: int = Form(...), user_limit: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not 0 <= user_limit <= 99:
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=invalid", status_code=303)
    if not db.is_temp_voice_channel(channel_id, guild_id):
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=not_found", status_code=303)
    if not db.request_temp_voice_limit(guild_id, channel_id, user_limit):
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=not_found", status_code=303)
    await _wake("tempvoice_limit")
    return RedirectResponse(f"/guild/{guild_id}/tempvoice?limitrequested=1", status_code=303)


@app.post("/guild/{guild_id}/tempvoice/remove")
async def remove_voice_hub_route(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_voice_hub(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/tempvoice", status_code=303)


@app.get("/guild/{guild_id}/tickets")
async def tickets_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    cfg = db.get_ticket_config(guild_id)
    tickets = [
        {
            "id": row[0], "channel_id": row[1],
            "channel_label": channel_label(guild_id, row[1]),
            "opener": member_label(guild_id, row[2]),
            "subject": row[3], "status": row[4],
            "created_at": datetime.fromtimestamp(row[5]).strftime("%Y-%m-%d %H:%M"),
            "closed_at": datetime.fromtimestamp(row[6]).strftime("%Y-%m-%d %H:%M") if row[6] else None,
            "closed_by": member_label(guild_id, row[7]) if row[7] else None,
            "close_reason": row[8],
        }
        for row in db.list_tickets(guild_id, 50)
    ]
    categories = [c for c in db.list_bot_channels(guild_id, "category")]
    text_channels = db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news")
    return render(
        request, "tickets.html", guild_id, "tickets",
        cfg=cfg,
        category_label=channel_label(guild_id, cfg["category_id"]),
        support_role_label=role_label(guild_id, cfg["support_role_id"]),
        panel_channel_label=channel_label(guild_id, cfg["panel_channel_id"]),
        categories=categories,
        text_channels=text_channels,
        roles=db.list_bot_roles(guild_id),
        tickets=tickets,
    )


@app.post("/guild/{guild_id}/tickets/config")
async def save_ticket_config(request: Request, guild_id: int, category_id: int = Form(...), support_role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, category_id, ("category",)):
        return RedirectResponse(f"/guild/{guild_id}/tickets?error=category", status_code=303)
    if not validate_role(guild_id, support_role_id):
        return RedirectResponse(f"/guild/{guild_id}/tickets?error=role", status_code=303)
    db.set_ticket_config(guild_id, category_id, support_role_id)
    return RedirectResponse(f"/guild/{guild_id}/tickets?saved=1", status_code=303)


@app.post("/guild/{guild_id}/tickets/delete-on-close")
async def save_ticket_delete_on_close(
    request: Request, guild_id: int, delete_on_close: str = Form(None), delete_delay_seconds: int = Form(10)
):
    if (r := await require_auth(request)):
        return r
    delay = max(3, min(300, delete_delay_seconds))
    db.set_ticket_delete_on_close(guild_id, delete_on_close is not None, delay)
    return RedirectResponse(f"/guild/{guild_id}/tickets?deletesaved=1", status_code=303)


@app.post("/guild/{guild_id}/tickets/panel")
async def save_ticket_panel_route(
    request: Request, guild_id: int, panel_channel_id: int = Form(...),
    panel_title: str = Form(...), panel_description: str = Form(...),
):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, panel_channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/tickets?error=panelchannel", status_code=303)
    panel_title = panel_title.strip()[:256] or "Support"
    panel_description = panel_description.strip()[:1000] or "Click the button below to open a private ticket with the support team."
    db.set_ticket_panel_config(guild_id, panel_channel_id, panel_title, panel_description)
    db.queue_ticket_panel_post(guild_id)
    await _wake("ticket_panel")
    return RedirectResponse(f"/guild/{guild_id}/tickets?panelsaved=1", status_code=303)


@app.post("/guild/{guild_id}/tickets/close")
async def close_ticket_route(request: Request, guild_id: int, ticket_id: int = Form(...), reason: str = Form("")):
    if (r := await require_auth(request)):
        return r
    row = db.get_ticket(ticket_id)
    if row is None or row[1] != guild_id:
        return RedirectResponse(f"/guild/{guild_id}/tickets?error=notfound", status_code=303)
    if row[5] != "open":
        return RedirectResponse(f"/guild/{guild_id}/tickets?error=alreadyclosed", status_code=303)
    db.queue_ticket_close(guild_id, ticket_id, reason.strip()[:500] or "Closed from the dashboard")
    await _wake("ticket_close")
    return RedirectResponse(f"/guild/{guild_id}/tickets?closequeued={ticket_id}", status_code=303)
