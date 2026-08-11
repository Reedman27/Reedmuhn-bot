"""Web dashboard for the bot. Reads and writes the exact same bot.db file
the Discord bot uses (via a mounted volume, see docker-compose.yml).

The dashboard uses a local shared-password login. It does not require
Discord OAuth, a public domain, or a Discord redirect URL, so it works on
LAN-only/self-hosted deployments such as http://HOST:8490.

Pages live under /guild/{guild_id}/<page> as separate routes (not anchor
links on one long page) so each section gets its own URL, its own back
button behavior, and doesn't force loading/rendering every section's data
on every visit.
"""
import json
import os
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import hmac
from db import Db

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]

DB_PATH = os.environ.get("DB_PATH", "data/bot.db")



def load_or_create_secret_key() -> str:
    """The key used to sign login session cookies. Stored as a file next to
    bot.db (same mounted volume) so it survives container rebuilds - if it
    changed on every restart, everyone would get logged out constantly.
    Generated automatically on first run so there's nothing to configure.
    """
    secret_path = Path(DB_PATH).parent / "webui_secret_key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if secret_path.exists():
        return secret_path.read_text().strip()
    key = secrets.token_hex(32)
    secret_path.write_text(key)
    try:
        secret_path.chmod(0o600)
    except OSError:
        pass
    return key


SECRET_KEY = load_or_create_secret_key()
db = Db(DB_PATH)


async def _lifespan(app: FastAPI):
    yield


app = FastAPI(lifespan=_lifespan)


@app.middleware("http")
async def security_headers_and_origin_check(request: Request, call_next):
    if request.method == "POST" and request.url.path != "/login":
        origin = request.headers.get("origin")
        referer = request.headers.get("referer")
        expected = f"{request.url.scheme}://{request.headers.get('host', request.url.netloc)}"
        supplied = origin or (referer.rsplit("/", 3)[0] if referer else None)
        if origin and not origin.startswith(expected):
            return RedirectResponse("/login", status_code=303)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'")
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    max_age=12 * 60 * 60,
    https_only=os.environ.get("WEBUI_HTTPS_ONLY", "0") == "1",
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "")

if not WEBUI_PASSWORD:
    raise RuntimeError(
        "WEBUI_PASSWORD is not configured. Set WEBUI_PASSWORD in .env."
    )


def is_authed(request: Request) -> bool:
    return request.session.get("authenticated") is True


async def require_auth(request: Request):
    """Require the local WebUI password and verify guild-scoped URLs refer
    to a guild the bot is actually in. No Discord OAuth or public domain is
    required for this self-hosted dashboard.
    """
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    parts = request.url.path.split("/", 3)
    if len(parts) >= 3 and parts[1] == "guild":
        try:
            guild_id = int(parts[2])
        except ValueError:
            guild_id = None
        if guild_id is None or not db.is_bot_in_guild(guild_id):
            request.session.pop("guild_id", None)
            return RedirectResponse("/?error=not_accessible", status_code=303)
    return None


def validate_channel(guild_id: int, channel_id: int, allowed_types: tuple[str, ...]) -> bool:
    return any(cid == channel_id and ctype in allowed_types for cid, _name, ctype, _pos in db.list_bot_channels(guild_id))


MESSAGE_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def parse_message_reference(raw: str) -> tuple[Optional[int], Optional[int]]:
    """Same parsing the bot's reaction-role cog does - duplicated here since
    the dashboard is a separate process (see the note at the top of
    webui/db.py about this file being a deliberate copy, not an import).
    Accepts a pasted message link (Copy Message Link - no Developer Mode
    needed) or a bare message ID. Returns (channel_id, message_id);
    channel_id is None for a bare ID."""
    raw = raw.strip()
    match = MESSAGE_LINK_RE.search(raw)
    if match:
        _guild_id, channel_id, message_id = match.groups()
        return int(channel_id), int(message_id)
    if raw.isdigit():
        return None, int(raw)
    return None, None


def validate_role(guild_id: int, role_id: int) -> bool:
    return any(rid == role_id for rid, _name, _pos in db.list_bot_roles(guild_id))


def member_label(guild_id: int, user_id: int) -> str:
    name = db.get_member_name(guild_id, user_id)
    return name if name else f"Former member ({user_id})"


def channel_label(guild_id: int, channel_id: int | None) -> str:
    if channel_id is None:
        return "Not configured"
    name = db.get_channel_name(guild_id, channel_id)
    return f"#{name}" if name else f"Deleted channel ({channel_id})"


def role_label(guild_id: int, role_id: int | None) -> str:
    if role_id is None:
        return "Not configured"
    name = db.get_role_name(guild_id, role_id)
    return f"@{name}" if name else f"Deleted role ({role_id})"


# ---- login rate limiting ----
# In-memory per-IP lockout for failed local-password attempts.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

_failed_attempts: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def is_locked_out(request: Request) -> bool:
    ip = _client_ip(request)
    attempts = _failed_attempts.get(ip, [])
    cutoff = time.monotonic() - LOCKOUT_SECONDS
    recent = [t for t in attempts if t > cutoff]
    _failed_attempts[ip] = recent
    return len(recent) >= MAX_FAILED_ATTEMPTS


def record_failed_login(request: Request) -> None:
    _failed_attempts.setdefault(_client_ip(request), []).append(time.monotonic())


def clear_failed_logins(request: Request) -> None:
    _failed_attempts.pop(_client_ip(request), None)


def render(request: Request, page: str, guild_id: int, active: str, **extra):
    """Renders a guild page with the sidebar-common context (guild_id,
    which nav item is active) merged in, so individual routes only pass
    what's unique to that page."""
    return templates.TemplateResponse(
        request, page, {
            "guild_id": guild_id,
            "guild_name": db.get_guild_name(guild_id) or f"Server {guild_id}",
            "active": active,
            "discord_username": request.session.get("discord_username"),
            **extra,
        }
    )


# ---- auth ----

@app.get("/login")
async def login_form(request: Request):
    if is_authed(request):
        return RedirectResponse("/", status_code=303)
    error = request.query_params.get("error")
    error_message = {
        "failed": "Incorrect password. Please try again.",
        "locked": "Too many failed attempts. Try again in a few minutes.",
    }.get(error)
    return templates.TemplateResponse(request, "login.html", {"error": error_message})


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if is_locked_out(request):
        return RedirectResponse("/login?error=locked", status_code=303)

    if not hmac.compare_digest(password, WEBUI_PASSWORD):
        record_failed_login(request)
        return RedirectResponse("/login?error=failed", status_code=303)

    clear_failed_logins(request)
    request.session.clear()
    request.session["authenticated"] = True
    request.session["discord_username"] = "Dashboard Admin"
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---- guild picker ----

@app.get("/")
async def home(request: Request):
    if (r := await require_auth(request)):
        return r
    guild_id = request.session.get("guild_id")
    if guild_id:
        return RedirectResponse(f"/guild/{guild_id}", status_code=303)

    # Password authentication is intentionally local/shared: anyone who has
    # the dashboard password can manage the bot's servers.
    accessible_guilds = db.list_bot_guilds()
    error = request.query_params.get("error")
    return templates.TemplateResponse(
        request, "pick_guild.html",
        {
            "guilds": accessible_guilds,
            "not_accessible": error == "not_accessible",
            "no_permission": error == "no_permission",
        },
    )


@app.post("/select-guild")
async def select_guild(request: Request, guild_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not db.is_bot_in_guild(guild_id):
        return RedirectResponse("/?error=not_accessible", status_code=303)
    request.session["guild_id"] = guild_id
    return RedirectResponse(f"/guild/{guild_id}", status_code=303)


@app.post("/switch-guild")
async def switch_guild(request: Request):
    if (r := await require_auth(request)):
        return r
    request.session.pop("guild_id", None)
    return RedirectResponse("/", status_code=303)


# ---- overview ----

@app.get("/guild/{guild_id}")
async def overview(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    request.session["guild_id"] = guild_id  # keep in sync if they land here directly

    cfg = db.get_guild_config(guild_id)
    counting = db.get_counting(guild_id)
    stats = {
        "custom_commands": len(db.list_custom_commands(guild_id)),
        "birthdays": len(db.list_birthdays(guild_id)),
        "active_tempbans": len(db.list_scheduled_events(guild_id, "unban")),
        "pending_reminders": len(db.list_scheduled_events(guild_id, "reminder")),
        "welcome_configured": bool(cfg["welcome_channel_id"]),
        "autorole_configured": bool(cfg["autorole_id"]),
        "counting_channel": bool(counting),
        "counting_high_score": counting["high_score"] if counting else 0,
    }
    return render(request, "overview.html", guild_id, "overview", stats=stats)


# ---- welcome & autorole ----

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
async def save_autorole(request: Request, guild_id: int, role_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_role(guild_id, role_id):
        return RedirectResponse(f"/guild/{guild_id}/welcome?error=role", status_code=303)
    db.set_autorole(guild_id, role_id)
    return RedirectResponse(f"/guild/{guild_id}/welcome", status_code=303)


# ---- custom commands ----

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


# ---- birthdays ----

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


# ---- counting ----

@app.get("/guild/{guild_id}/counting")
async def counting_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    return render(request, "counting.html", guild_id, "counting", counting=db.get_counting(guild_id),
                  text_channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"))


@app.post("/guild/{guild_id}/counting/channel")
async def save_counting_channel(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("text", "news")):
        return RedirectResponse(f"/guild/{guild_id}/counting?error=channel", status_code=303)
    db.set_counting_channel(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/counting", status_code=303)


@app.post("/guild/{guild_id}/counting/saves")
async def save_counting_saves(request: Request, guild_id: int, milestone: int = Form(...), max_saves: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not 1 <= milestone <= 1_000_000 or not 0 <= max_saves <= 100:
        return RedirectResponse(f"/guild/{guild_id}/counting?error=invalid", status_code=303)
    db.set_save_settings(guild_id, milestone, max_saves)
    return RedirectResponse(f"/guild/{guild_id}/counting", status_code=303)


@app.post("/guild/{guild_id}/counting/highscorealerts")
async def save_counting_highscorealerts(request: Request, guild_id: int, enabled: str = Form(default="")):
    if (r := await require_auth(request)):
        return r
    db.set_high_score_alerts(guild_id, enabled == "on")
    return RedirectResponse(f"/guild/{guild_id}/counting", status_code=303)


# ---- scheduled background tasks (reminders and nickname reverts) ----

@app.get("/guild/{guild_id}/scheduled")
async def scheduled_page(request: Request, guild_id: int):
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

    nick_reverts = []
    for event_id, _, run_at, data in db.list_scheduled_events(guild_id, "revert_nick"):
        parsed = json.loads(data)
        nick_reverts.append({
            "id": event_id,
            "user_id": parsed["user_id"],
            "user_name": member_label(guild_id, parsed["user_id"]),
            "reverts_at": datetime.fromtimestamp(run_at).strftime("%Y-%m-%d %H:%M"),
        })

    return render(
        request, "scheduled.html", guild_id, "scheduled",
        reminders=reminders, nick_reverts=nick_reverts,
    )


@app.post("/guild/{guild_id}/scheduled/cancel-reminder")
async def cancel_reminder(request: Request, guild_id: int, event_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.delete_scheduled_event(event_id, guild_id)
    return RedirectResponse(f"/guild/{guild_id}/scheduled", status_code=303)


# ---- permissions (tempnick access rule) ----

@app.get("/guild/{guild_id}/permissions")
async def permissions_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    return render(
        request, "permissions.html", guild_id, "permissions",
        mode=db.get_tempnick_mode(guild_id),
        roles=[(rid, role_label(guild_id, rid)) for rid in db.list_tempnick_roles(guild_id)],
        role_choices=db.list_bot_roles(guild_id),
        bot_manager_roles=[(rid, role_label(guild_id, rid)) for rid in db.list_bot_manager_roles(guild_id)],
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


# ---- moderation (warn lookup) ----

@app.get("/guild/{guild_id}/moderation")
async def moderation_page(request: Request, guild_id: int, user_id: Optional[int] = None, tab: str = "overview"):
    if (r := await require_auth(request)):
        return r

    if tab not in {"overview", "warnings", "tempbans"}:
        tab = "overview"

    warns = []
    if user_id:
        rows = db.list_warns(guild_id, user_id)
        warns = [
            (warn_id, moderator_id, member_label(guild_id, moderator_id), reason, datetime.fromtimestamp(created_at).strftime("%Y-%m-%d %H:%M"))
            for warn_id, moderator_id, reason, created_at in rows
        ]

    tempbans = []
    for event_id, _, run_at, data in db.list_scheduled_events(guild_id, "unban"):
        parsed = json.loads(data)
        tempbans.append({
            "id": event_id,
            "user_id": parsed["user_id"],
            "user_name": member_label(guild_id, parsed["user_id"]),
            "unban_at": datetime.fromtimestamp(run_at).strftime("%Y-%m-%d %H:%M"),
        })

    cfg = db.get_guild_config(guild_id)
    return render(
        request, "moderation.html", guild_id, "moderation",
        tab=tab, looked_up_user_id=user_id,
        looked_up_user_name=(member_label(guild_id, user_id) if user_id else None),
        warns=warns, members=db.list_bot_members(guild_id), tempbans=tempbans,
        muted_role_id=cfg["muted_role_id"], roles=db.list_bot_roles(guild_id), muted_config=cfg,
    )


@app.post("/guild/{guild_id}/moderation/clear-warns")
async def clear_warns_route(request: Request, guild_id: int, user_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.clear_warns(guild_id, user_id)
    return RedirectResponse(f"/guild/{guild_id}/moderation?tab=warnings&user_id={user_id}", status_code=303)


@app.post("/guild/{guild_id}/moderation/muted-role")
async def save_muted_role(request: Request, guild_id: int, role_id: str = Form("auto")):
    if (r := await require_auth(request)):
        return r
    if role_id == "auto":
        db.set_muted_role(guild_id, None)
    elif validate_role(guild_id, int(role_id)):
        db.set_muted_role(guild_id, int(role_id))
    return RedirectResponse(f"/guild/{guild_id}/moderation", status_code=303)

@app.post("/guild/{guild_id}/moderation/muted-role-settings")
async def save_muted_role_settings(
    request: Request, guild_id: int,
    messages: Optional[str] = Form(None), reactions: Optional[str] = Form(None),
    threads: Optional[str] = Form(None), connect: Optional[str] = Form(None),
    speak: Optional[str] = Form(None), stream: Optional[str] = Form(None),
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
    )
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


# ---- logging ----

LOG_CATEGORIES = [
    ("messages", "Messages", "Edits, deletes, and bulk deletes"),
    ("members", "Members", "Joins, leaves, nicknames, and role changes"),
    ("moderation", "Moderation", "Bans, unbans, kicks, and timeouts"),
    ("server", "Server", "Channel, role, and server changes"),
    ("voice", "Voice", "Voice joins, leaves, and moves"),
]


@app.get("/guild/{guild_id}/logging")
async def logging_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    configured = db.get_all_log_channels(guild_id)
    ignored_ids = db.list_ignored_log_channels(guild_id)
    return render(
        request, "logging.html", guild_id, "logging",
        categories=LOG_CATEGORIES,
        configured=configured,
        ignored_channels=[(cid, channel_label(guild_id, cid)) for cid in ignored_ids],
        channel_choices=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"),
    )


@app.post("/guild/{guild_id}/logging/channel")
async def save_logging_channel(request: Request, guild_id: int, category: str = Form(...), channel_id: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    allowed = {key for key, _name, _desc in LOG_CATEGORIES}
    if category not in allowed:
        return RedirectResponse(f"/guild/{guild_id}/logging?error=category", status_code=303)
    if channel_id == "off":
        db.disable_log_category(guild_id, category)
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


# ---- youtube ----

@app.get("/guild/{guild_id}/youtube")
async def youtube_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    watches = [
        {
            "yt_channel_id": yt_id,
            "display": channel_name if channel_name else yt_id,
            "announce_name": channel_label(guild_id, announce_channel_id),
        }
        for yt_id, announce_channel_id, _last_video_id, channel_name, _role_id, _mode in db.list_youtube_watches(guild_id)
    ]
    return render(request, "youtube.html", guild_id, "youtube", watches=watches,
                  text_channels=db.list_bot_channels(guild_id, "text") + db.list_bot_channels(guild_id, "news"))


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


# ---- automod ----

@app.get("/guild/{guild_id}/automod")
async def automod_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    cfg = db.get_automod_config(guild_id)
    return render(
        request, "automod.html", guild_id, "automod",
        cfg=cfg,
        exempt_roles=[(rid, role_label(guild_id, rid)) for rid in db.list_automod_exempt_roles(guild_id)],
        role_choices=db.list_bot_roles(guild_id),
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


@app.post("/guild/{guild_id}/automod/words")
async def save_automod_words(request: Request, guild_id: int, words: str = Form("")):
    if (r := await require_auth(request)):
        return r
    db.set_automod_words(guild_id, words.split(","))
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
async def save_automod_escalation(
    request: Request, guild_id: int, threshold: int = Form(...), window_seconds: int = Form(...), mute_duration_seconds: int = Form(...)
):
    if (r := await require_auth(request)):
        return r
    if not 1 <= threshold <= 1000 or not 1 <= window_seconds <= 604800 or not 1 <= mute_duration_seconds <= 28 * 86400:
        return RedirectResponse(f"/guild/{guild_id}/automod?error=invalid", status_code=303)
    db.set_automod_escalation(guild_id, threshold, window_seconds, mute_duration_seconds)
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


# ---- reaction roles (read-mostly - adding one requires the bot to place
# the actual reaction on a message, so that stays a slash command; the
# dashboard can review what's configured and remove bindings) ----

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


# ---- temp voice ----

@app.get("/guild/{guild_id}/tempvoice")
async def tempvoice_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    hub_ids = db.list_voice_hubs(guild_id)
    return render(
        request, "tempvoice.html", guild_id, "tempvoice",
        hubs=[(cid, channel_label(guild_id, cid)) for cid in hub_ids],
        active_channels=[(cid, member_label(guild_id, owner_id), channel_label(guild_id, cid)) for cid, owner_id in db.list_temp_voice_channels(guild_id)],
        voice_channels=db.list_bot_channels(guild_id, "voice"),
    )


@app.post("/guild/{guild_id}/tempvoice/hub")
async def save_voice_hub(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    if not validate_channel(guild_id, channel_id, ("voice",)):
        return RedirectResponse(f"/guild/{guild_id}/tempvoice?error=channel", status_code=303)
    db.add_voice_hub(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/tempvoice", status_code=303)


@app.post("/guild/{guild_id}/tempvoice/remove")
async def remove_voice_hub_route(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_voice_hub(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/tempvoice", status_code=303)
