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
from urllib.parse import urlsplit

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
        expected = urlsplit(str(request.base_url))
        supplied = origin or referer
        if supplied:
            supplied_parts = urlsplit(supplied)
            # Compare scheme + network location exactly. A simple startswith()
            # check is unsafe because an attacker-controlled origin such as
            # https://dashboard.example.evil can share the dashboard's prefix.
            if (supplied_parts.scheme, supplied_parts.netloc) != (expected.scheme, expected.netloc):
                return RedirectResponse("/login", status_code=303)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'")
    # Dashboard action log: every mutating (POST) request an authenticated
    # session makes, beyond login itself (which is logged separately in
    # login_submit) and static assets. Best-effort - a logging failure
    # should never break the actual request.
    if request.method == "POST" and request.url.path not in ("/login",) and is_authed(request):
        try:
            guild_id = None
            parts = request.url.path.split("/")
            if len(parts) >= 3 and parts[1] == "guild":
                guild_id = int(parts[2])
            db.record_webui_action(guild_id, _client_ip(request), request.method, request.url.path, response.status_code)
        except Exception:
            pass
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
        db.record_webui_login(_client_ip(request), False)
        return RedirectResponse("/login?error=failed", status_code=303)

    clear_failed_logins(request)
    db.record_webui_login(_client_ip(request), True)
    request.session.clear()
    request.session["authenticated"] = True
    request.session["discord_username"] = "Dashboard Admin"
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---- dashboard log (login attempts + actions taken through the WebUI) ----
# Global rather than per-guild: login happens before a server is even
# selected, and the shared-password model (see the note atop this file)
# means there's no per-admin identity to scope it to anyway.

@app.get("/dashboard-log")
async def dashboard_log_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    logins = [
        {"created_at": datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M:%S"), "ip": row[1], "success": bool(row[2])}
        for row in db.list_webui_login_events(50)
    ]
    actions = [
        {
            "created_at": datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M:%S"), "guild_id": row[1],
            "guild_name": (db.get_guild_name(row[1]) if row[1] else None),
            "ip": row[2], "method": row[3], "path": row[4], "status_code": row[5],
        }
        for row in db.list_webui_action_log(100)
    ]
    return templates.TemplateResponse(
        request, "dashboard_log.html",
        {
            "guild_id": None, "guild_name": None, "active": "dashboard_log",
            "discord_username": request.session.get("discord_username"),
            "logins": logins, "actions": actions,
        },
    )


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
            "guild_id": None,
            "guild_name": None,
            "discord_username": request.session.get("discord_username"),
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

    # Compact activity snapshot, same numbers the full Analytics page shows,
    # so people can see server health without leaving the overview.
    activity_days = 7
    since = int(time.time()) - activity_days * 86400
    analytics_stats = {**db.get_server_counts(guild_id), **db.get_activity_stats(guild_id, since)}

    return render(
        request, "overview.html", guild_id, "overview",
        stats=stats, analytics_stats=analytics_stats, activity_days=activity_days,
    )


# ---- analytics ----

def _parse_days(request: Request) -> int:
    try:
        days = int(request.query_params.get("days", "14"))
    except ValueError:
        days = 14
    if days not in (1, 7, 14, 30):
        days = 14
    return days


# Shared metadata for every drill-down-able analytics figure: which stat key
# it reads, its label/icon/color on the analytics grid, and whether it's a
# point-in-time snapshot (members/online/channels/roles) or a count of
# events over the selected time range (messages/commands/joins/leaves).
ANALYTICS_METRICS = {
    "members":  {"label": "Members",  "kind": "snapshot", "color": "purple"},
    "online":   {"label": "Online",   "kind": "snapshot", "color": "green"},
    "channels": {"label": "Channels", "kind": "snapshot", "color": "blue"},
    "roles":    {"label": "Roles",    "kind": "snapshot", "color": "yellow"},
    "messages": {"label": "Messages", "kind": "timeseries", "color": "purple", "event_type": "message.received"},
    "commands": {"label": "Commands", "kind": "timeseries", "color": "blue", "event_type": "command.completed"},
    "joins":    {"label": "Joins",    "kind": "timeseries", "color": "green", "event_type": "member.join"},
    "leaves":   {"label": "Leaves",   "kind": "timeseries", "color": "red", "event_type": "member.leave"},
    "message_edits":   {"label": "Message Edits",   "kind": "timeseries", "color": "yellow", "event_type": "message.edited"},
    "message_deletes": {"label": "Message Deletes", "kind": "timeseries", "color": "red", "event_type": "message.deleted"},
    "reactions":       {"label": "Reactions",       "kind": "timeseries", "color": "purple", "event_type": "reaction.added"},
    "voice_joins":     {"label": "Voice Joins",     "kind": "timeseries", "color": "green", "event_type": "voice.join"},
    "voice_leaves":    {"label": "Voice Leaves",    "kind": "timeseries", "color": "blue", "event_type": "voice.leave"},
}


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


# ---- permissions (tempnick access rule) ----

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


@app.post("/guild/{guild_id}/moderation/votekick")
async def save_votekick_config(request: Request, guild_id: int, enabled: str = Form(None), required_votes: int = Form(5), duration_minutes: int = Form(10)):
    if (r := await require_auth(request)): return r
    if not 1 <= required_votes <= 100 or not 1 <= duration_minutes <= 1440:
        return RedirectResponse(f"/guild/{guild_id}/moderation?error=votekick", status_code=303)
    db.set_votekick_config(guild_id, enabled == "on", required_votes, duration_minutes * 60)
    return RedirectResponse(f"/guild/{guild_id}/moderation", status_code=303)

# ---- moderation (warn lookup) ----

MOD_ACTION_LABELS = {
    "kick": "Kick",
    "ban": "Ban",
    "tempban": "Temporary ban",
    "unban": "Unban",
    "mute_role": "Mute (role)",
    "unmute_role": "Unmute (remove role)",
    "timeout": "Timeout",
    "untimeout": "Remove timeout",
}
MOD_TIMED_ACTIONS = {"tempban", "mute_role", "timeout"}
MOD_ACTION_TIMEOUT_MAX_SECONDS = 28 * 86400

CASE_ACTION_LABELS = {
    "warn": "Warn",
    "kick": "Kick",
    "mute": "Mute",
    "tempban": "Temporary Ban",
    "timeout": "Timeout",
}

# ---- permission security scanner ----
# Discord permission bitfield values (from the API's permission flags -
# hardcoded here rather than importing discord.py, since this process is
# deliberately kept discord.py-free and only reads what the bot process
# already cached).
PERM_ADMINISTRATOR = 0x8
PERM_RISKY = {
    "ban members": 0x4,
    "kick members": 0x2,
    "manage the server": 0x20,
    "manage roles": 0x10000000,
    "manage channels": 0x10,
    "manage webhooks": 0x20000000,
    "mention @everyone/@here": 0x20000,
    "timeout/moderate members": 0x10000000000,
}


def scan_guild_permissions(guild_id: int) -> list[dict]:
    """Flags roles (and @everyone) with Administrator or other permissions
    that are easy to hand out by accident and hard to notice once granted.
    Read-only, computed entirely from the roles the bot already has
    cached - no live Discord call."""
    findings = []
    for role_id, name, _position, perms, managed in db.list_bot_roles_full(guild_id):
        if perms & PERM_ADMINISTRATOR:
            findings.append({
                "severity": "critical",
                "subject": f"@{name}",
                "message": "has Administrator" + (" - this is a bot/integration role" if managed else ""),
            })
            continue
        risky = [label for label, bit in PERM_RISKY.items() if perms & bit]
        if risky:
            findings.append({"severity": "warning", "subject": f"@{name}", "message": f"can {', '.join(risky)}"})

    everyone_perms = db.get_everyone_permissions(guild_id)
    if everyone_perms & PERM_ADMINISTRATOR:
        findings.append({"severity": "critical", "subject": "@everyone", "message": "has Administrator"})
    else:
        risky = [label for label, bit in PERM_RISKY.items() if everyone_perms & bit]
        if risky:
            # Anything beyond a bare "can mention everyone" on the default
            # role (every member) is a much bigger deal than the same
            # permission on a normal role, since it applies to the whole
            # server with no way to un-assign it from anyone.
            only_mention_everyone = risky == ["mention @everyone/@here"]
            findings.append({
                "severity": "warning" if only_mention_everyone else "critical",
                "subject": "@everyone", "message": f"can {', '.join(risky)}",
            })

    order = {"critical": 0, "warning": 1}
    findings.sort(key=lambda f: order.get(f["severity"], 2))
    return findings


def queue_warn_escalation_if_due(guild_id: int, user_id: int, latest_reason: str) -> None:
    """Mirrors AutoMod.apply_warn_escalation on the bot side, run from the
    WebUI process instead: checks a freshly-added manual warning against
    the same escalation tiers configured on the Automod tab, using the
    member's warning count within the configured window. A "warn" tier is
    applied directly here (it's DB-only, no Discord connection needed); any
    other tier (mute/timeout/kick/ban/tempban) is queued onto
    dashboard_mod_actions for the bot process to actually execute, exactly
    like the moderation tab's own action buttons.
    """
    cfg = db.get_automod_config(guild_id)
    since = int(time.time()) - cfg["violation_window_seconds"]
    recent = db.count_recent_escalation_warnings(guild_id, user_id, since)
    tiers = db.list_automod_escalation_tiers(guild_id)
    # Exact match only (not "past every tier") - manual warns are a
    # permanent record that's never auto-cleared, so falling back to "past
    # every tier" here would re-fire the harshest punishment on every
    # single warning after the last configured threshold.
    tier = next((t for t in tiers if t["threshold"] == recent), None)
    if tier is None:
        return
    reason = f"Warnings: {tier['threshold']} warnings (latest: {latest_reason})"
    if tier["action"] == "warn":
        # A punishment tier is not itself another warning. Recording a warn here
        # would inflate the threshold that just triggered and create a loop.
        db.record_member_history(guild_id, user_id, "automod_warn_tier", 0, reason)
        return
    if tier["action"] not in MOD_ACTION_LABELS:
        return
    db.queue_mod_action(guild_id, user_id, tier["action"], tier["duration_seconds"], reason)


@app.get("/guild/{guild_id}/moderation")
async def moderation_page(request: Request, guild_id: int, user_id: Optional[int] = None, tab: str = "overview"):
    if (r := await require_auth(request)):
        return r

    if tab not in {"overview", "warnings", "cases", "tempbans", "tempnicks"}:
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

    tempnicks = []
    for event_id, _, run_at, data in db.list_scheduled_events(guild_id, "revert_nick"):
        parsed = json.loads(data)
        tempnicks.append({
            "id": event_id,
            "user_id": parsed["user_id"],
            "user_name": member_label(guild_id, parsed["user_id"]),
            "original_nick": parsed.get("original_nick"),
            "revert_at": datetime.fromtimestamp(run_at).strftime("%Y-%m-%d %H:%M"),
        })

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
        warns=warns, warned_users=warned_users, members=members, tempbans=tempbans, tempnicks=tempnicks,
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
    return RedirectResponse(f"/guild/{guild_id}/moderation", status_code=303)

MUTE_PRESETS = {
    "visible_no_talk": dict(  # A: see everything, can join VC, can't talk anywhere
        deny_send_messages=True, deny_reactions=True, deny_threads=True,
        deny_connect=False, deny_speak=True, deny_stream=True, deny_view_channel=False,
    ),
    "visible_no_voice_no_type": dict(  # B: see everything, can't join VC, can't type
        deny_send_messages=True, deny_reactions=True, deny_threads=True,
        deny_connect=True, deny_speak=True, deny_stream=True, deny_view_channel=False,
    ),
    "fully_isolated": dict(  # C: can't see or join anything
        deny_send_messages=True, deny_reactions=True, deny_threads=True,
        deny_connect=True, deny_speak=True, deny_stream=True, deny_view_channel=True,
    ),
}


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
    return RedirectResponse(f"/guild/{guild_id}/moderation", status_code=303)


@app.post("/guild/{guild_id}/moderation/muted-role-preset")
async def save_muted_role_preset(request: Request, guild_id: int, preset: str = Form(...)):
    if (r := await require_auth(request)):
        return r
    if preset not in MUTE_PRESETS:
        return RedirectResponse(f"/guild/{guild_id}/moderation?error=invalid", status_code=303)
    db.set_muted_settings(guild_id, **MUTE_PRESETS[preset])
    db.queue_mute_role_sync(guild_id, f"WebUI: Muted role preset applied ({preset})")
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


@app.post("/guild/{guild_id}/moderation/tempnicks/revert")
async def revert_tempnick_now(request: Request, guild_id: int, event_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    event = db.get_scheduled_event(event_id, guild_id, "revert_nick")
    if event:
        _event_id, _name, _run_at, data = event
        db.delete_scheduled_event(event_id, guild_id)
        parsed = json.loads(data)
        # Same near-immediate-reschedule pattern as "Unban now" above - the
        # dashboard has no Discord connection of its own, so the bot process
        # picks this up and reverts the nickname on its next scheduler tick.
        db.insert_scheduled_event(
            "revert_nick", guild_id, int(time.time()),
            {"user_id": parsed["user_id"], "original_nick": parsed.get("original_nick")},
        )
    return RedirectResponse(f"/guild/{guild_id}/moderation?tab=tempnicks", status_code=303)


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
    return RedirectResponse(f"/guild/{guild_id}/moderation?action=queued", status_code=303)


# ---- fun command toggles ----

FUN_COMMANDS = [
    ("Social", [("hug", "Hug someone"), ("insult", "Playfully roast someone"), ("compliment", "Give someone a compliment"), ("pat", "Pat someone"), ("slap", "Playful slap"), ("highfive", "High-five someone"), ("ship", "Compatibility score")]),
    ("Games & Randomness", [("8ball", "Magic 8-ball"), ("coinflip", "Flip a coin"), ("roll", "Roll dice"), ("rps", "Rock-paper-scissors"), ("choose", "Pick between options"), ("wouldyourather", "Would-you-rather question")]),
    ("Text Toys", [("dadjoke", "Random dad joke"), ("mock", "MoCk TeXt"), ("reverse", "Reverse text")]),
    ("Leveling & Economy", [("rank", "Show your rank/level"), ("leaderboard", "XP leaderboard"), ("balance", "Show a coin balance"), ("daily", "Claim daily coins"), ("work", "Work a shift for coins"), ("pay", "Pay another member"), ("richest", "Richest members leaderboard")]),
]

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

# ---- starboard ----

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

# ---- suggestions ----

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

# ---- anti-nuke ----

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

# ---- raid detection ----

RAID_ACTIONS = {"alert": "Alert only", "kick_new": "Auto-kick new accounts", "lockdown": "Lock all channels"}


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

# ---- logging ----

LOG_CATEGORIES = [
    ("messages", "Messages", "Edits, deletes, and bulk deletes"),
    ("members", "Members", "Joins, leaves, nicknames, role changes, and verification"),
    ("moderation", "Moderation", "Staff-issued warns, mutes, kicks, bans, tempbans, and emergency actions"),
    ("automod", "AutoMod", "Filter catches (deleted messages, DMs sent), escalations, and queued-for-review holds"),
    ("tickets", "Tickets", "Ticket opened and closed"),
    ("reports", "Reports", "Reports filed, resolved, and dismissed"),
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


# ---- channel feed ----
# A read-only mirror of channel messages, written by cogs/channelfeed.py on
# the bot side when enabled. Lets you keep an eye on a server's channels
# from this dashboard alone, without the Discord client open.

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


# ---- AI ----

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


# ---- youtube ----

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


# ---- extras (leveling/economy leaderboards, counters, twitch, feeds, giveaways) ----

COUNTER_KINDS = {"members": "Member count", "online": "Online count", "bots": "Bot count", "channels": "Channel count"}


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
    url = url.strip()
    if not (url.startswith("http://") or url.startswith("https://")):
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


EXTRAS_MAX_AMOUNT = 1_000_000_000


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
    db.set_extras_xp(guild_id, user_id, new_xp)
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


# ---- automod ----

def format_duration(seconds: int) -> str:
    """Same rounding behavior as the bot's utils.format_duration, kept as a
    small local copy since the webui container doesn't have utils.py (see
    the note atop db.py about this codebase's process split)."""
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


AUTOMOD_ACTION_LABELS = {
    "mute_role": "Mute (role)",
    "timeout": "Timeout",
    "kick": "Kick",
    "ban": "Ban",
    "tempban": "Temporary ban",
}
AUTOMOD_TIMED_ACTIONS = {"mute_role", "timeout", "tempban"}
AUTOMOD_MAX_TIMEOUT_SECONDS = 28 * 86400


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


# ---- temp voice ----

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
    )


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
    return RedirectResponse(f"/guild/{guild_id}/tempvoice?limitrequested=1", status_code=303)


@app.post("/guild/{guild_id}/tempvoice/remove")
async def remove_voice_hub_route(request: Request, guild_id: int, channel_id: int = Form(...)):
    if (r := await require_auth(request)):
        return r
    db.remove_voice_hub(guild_id, channel_id)
    return RedirectResponse(f"/guild/{guild_id}/tempvoice", status_code=303)



# ---- sticky roles ----

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


# ---- talk as the bot ----
# Whoever's logged into the dashboard can compose a message and have the
# bot send it into a text channel. Anyone with the WEBUI_PASSWORD can use
# this - same trust level as everything else behind require_auth - since
# there's no per-admin dashboard login, just a shared password.

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
        try:
            db.record_bot_event("dashboard.talk.queued", guild_id, None, channel_id,
                                f"message_id={message_id} content_length={len(content)}", source="dashboard_talk")
        except Exception:
            pass
    except ValueError as exc:
        error = "length" if "2000" in str(exc) else "empty"
        return RedirectResponse(f"/guild/{guild_id}/talk?error={error}", status_code=303)
    return RedirectResponse(f"/guild/{guild_id}/talk?queued={message_id}", status_code=303)


# ---- verification ----

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
    return RedirectResponse(f"/guild/{guild_id}/verification?saved=1", status_code=303)


# ---- tickets ----

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
    return RedirectResponse(f"/guild/{guild_id}/tickets?closequeued={ticket_id}", status_code=303)


# ---- modmail ----

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


# ---- polls ----

@app.get("/guild/{guild_id}/polls")
async def polls_page(request: Request, guild_id: int):
    if (r := await require_auth(request)):
        return r
    polls = []
    for poll in db.list_polls(guild_id, 20):
        counts = db.poll_results(poll["id"], len(poll["options"]))
        total = sum(counts)
        results = [
            {
                "option": opt, "count": counts[i],
                "pct": round(counts[i] / total * 100) if total else 0,
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
    if poll["closed"]:
        return RedirectResponse(f"/guild/{guild_id}/polls?error=alreadyclosed", status_code=303)
    db.queue_poll_close(guild_id, poll_id)
    return RedirectResponse(f"/guild/{guild_id}/polls?closequeued={poll_id}", status_code=303)


# ---- server rules ----

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


# ---- member reports ----

REPORT_STATUS_LABELS = {"open": "Open", "reviewing": "Reviewing", "resolved": "Resolved", "dismissed": "Dismissed"}


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


# ---- staff activity ----

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


# ---- invite tracking ----

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


# ---- automod moderation queue ----

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
        return RedirectResponse(f"/guild/{guild_id}/moderationqueue?dismissqueued={review_id}", status_code=303)
    # Confirming needs to actually apply the escalation ladder, which needs
    # a live Discord connection the dashboard process doesn't have - so
    # it's queued and the bot's poller applies it (see automod.py's
    # _poll_queue_decisions), same bridge pattern as every other
    # WebUI->Discord action.
    db.queue_automod_decision(guild_id, review_id, "confirm", 0)
    return RedirectResponse(f"/guild/{guild_id}/moderationqueue?confirmqueued={review_id}", status_code=303)


# ---- server-wide search ----

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


# ---- emergency control center ----
# Dashboard-only actions - no matching slash commands (see the module note
# atop cogs/emergency.py for why). Every destructive action here goes
# through the same "type a confirmation phrase" front-end guard as purge
# and mod actions, at a bigger blast radius, so it gets checked twice:
# once client-side (JS confirm/phrase match, in the template) and once by
# a plain server-side "did they actually type the right word" check here -
# the second check is what actually matters, the JS is just UX.

EMERGENCY_CONFIRM_PHRASES = {
    "lockdown": "LOCKDOWN",
    "unlock": "UNLOCK",
    "revoke_invites": "REVOKE INVITES",
    "mass_timeout": "MASS TIMEOUT",
}


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


def _check_confirm_phrase(action: str, confirm_text: str) -> bool:
    expected = EMERGENCY_CONFIRM_PHRASES.get(action)
    return expected is not None and confirm_text.strip().upper() == expected


@app.post("/guild/{guild_id}/emergency/lockdown")
async def emergency_lockdown_route(request: Request, guild_id: int, confirm_text: str = Form("")):
    if (r := await require_auth(request)):
        return r
    if not _check_confirm_phrase("lockdown", confirm_text):
        return RedirectResponse(f"/guild/{guild_id}/emergency?error=confirm", status_code=303)
    # actor 0: no per-admin dashboard login to attribute this to, same
    # sentinel used by every other dashboard-issued action in this codebase.
    db.queue_emergency_request(guild_id, "lockdown", {"started_by": 0})
    return RedirectResponse(f"/guild/{guild_id}/emergency?queued=lockdown", status_code=303)


@app.post("/guild/{guild_id}/emergency/unlock")
async def emergency_unlock_route(request: Request, guild_id: int, confirm_text: str = Form("")):
    if (r := await require_auth(request)):
        return r
    if not _check_confirm_phrase("unlock", confirm_text):
        return RedirectResponse(f"/guild/{guild_id}/emergency?error=confirm", status_code=303)
    db.queue_emergency_request(guild_id, "unlock", {})
    return RedirectResponse(f"/guild/{guild_id}/emergency?queued=unlock", status_code=303)


@app.post("/guild/{guild_id}/emergency/revoke-invites")
async def emergency_revoke_invites_route(request: Request, guild_id: int, confirm_text: str = Form("")):
    if (r := await require_auth(request)):
        return r
    if not _check_confirm_phrase("revoke_invites", confirm_text):
        return RedirectResponse(f"/guild/{guild_id}/emergency?error=confirm", status_code=303)
    db.queue_emergency_request(guild_id, "revoke_invites", {})
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
    return RedirectResponse(f"/guild/{guild_id}/emergency?queued=mass_timeout", status_code=303)


# ---- config snapshots ----
# Scoped to ReedMuhn's own settings tables, not live Discord role/channel
# permissions - see capture_config_snapshot_data's docstring in db.py.

def _flatten_snapshot(data: dict, prefix: str = "") -> dict:
    """Flattens a snapshot's nested dict into {dotted.path: value} - lists
    are kept as single leaf values (diffed as a set below) rather than
    recursed into by index, since most of the lists in here (tempnick
    roles, ignored log channels, voice hubs) are unordered ID collections
    where "index 2 changed" means nothing to a human reading a diff."""
    flat = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten_snapshot(value, path))
        else:
            flat[path] = value
    return flat


def diff_snapshot_data(old: dict, new: dict) -> list[dict]:
    """Compares two capture_config_snapshot_data() dicts (or one of those
    against another) and returns a flat list of human-readable changes.
    Scoped to the same bot-settings-only data the snapshots themselves
    capture - this is not a diff of real Discord server structure."""
    old_flat, new_flat = _flatten_snapshot(old), _flatten_snapshot(new)
    changes = []
    for path in sorted(set(old_flat) | set(new_flat)):
        missing = object()
        ov = old_flat.get(path, missing)
        nv = new_flat.get(path, missing)
        if ov == nv:
            continue
        if isinstance(ov, list) and isinstance(nv, list):
            added = [x for x in nv if x not in ov]
            removed = [x for x in ov if x not in nv]
            if not added and not removed:
                continue
            summary = ", ".join(filter(None, [f"+{len(added)} added" if added else "", f"-{len(removed)} removed" if removed else ""]))
            changes.append({"path": path, "kind": "list", "summary": summary})
        else:
            changes.append({
                "path": path, "kind": "value",
                "old": "(not set)" if ov is missing else ov,
                "new": "(not set)" if nv is missing else nv,
            })
    return changes


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
