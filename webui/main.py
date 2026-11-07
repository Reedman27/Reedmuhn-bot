"""Web dashboard for the bot. Reads and writes the exact same bot.db file
the Discord bot uses (via a mounted volume, see docker-compose.yml).

The dashboard uses Discord OAuth2 identity and per-user guild permissions.
The bot may be installed in many servers; each user only sees servers where
they have Manage Server/Administrator access and the bot is installed.

Pages live under /guild/{guild_id}/<page> as separate routes (not anchor
links on one long page) so each section gets its own URL, its own back
button behavior, and doesn't force loading/rendering every section's data
on every visit.
"""
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import hmac
import asyncio
import aiohttp

# db.py, redis_client.py and safe_feed.py are shared with the bot: one copy at
# the repo root. In the image they sit next to this file; from a checkout they
# are one folder up.
sys.path.append(str(Path(__file__).resolve().parent.parent))
from db import Db, TEMP_VOICE_OWNER_PERMS
from redis_client import RedisClient
from safe_feed import validate_feed_url

SESSION_TTL_SECONDS = 12 * 60 * 60

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


def _env_flag(name: str, default: bool = False) -> bool:
    """Parse a boolean environment variable. .env.example documents "true"
    while the code used to accept only "1", which silently left the session
    cookie without the Secure flag for anyone who followed the example."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = load_or_create_secret_key()
db = Db(DB_PATH)
redis_client = RedisClient(os.environ.get("REDIS_URL"))
_wake_tasks: set[asyncio.Task] = set()


async def _wake(topic: str) -> None:
    try:
        # If Redis was unavailable when the WebUI started, try again on the
        # next dashboard action. SQLite polling remains the correctness
        # fallback, so Redis is only an acceleration path.
        if not redis_client.enabled:
            await redis_client.connect()
        await redis_client.publish_json("webui:wake", {"topic": topic})
    except Exception:
        # SQLite polling remains the correctness fallback; a Redis outage
        # must never break a dashboard action.
        pass


def _wake_fire_and_forget(topic: str) -> None:
    task = asyncio.create_task(_wake(topic))
    _wake_tasks.add(task)
    task.add_done_callback(_wake_tasks.discard)


async def _lifespan(app: FastAPI):
    await redis_client.connect()
    try:
        yield
    finally:
        for task in tuple(_wake_tasks):
            task.cancel()
        if _wake_tasks:
            await asyncio.gather(*_wake_tasks, return_exceptions=True)
        await redis_client.close()


app = FastAPI(lifespan=_lifespan)


@app.middleware("http")
async def security_headers_and_origin_check(request: Request, call_next):
    # CSRF tokens are tied to the signed session. Token fields are injected into
    # every rendered HTML POST form below, so templates do not need to duplicate
    # security-sensitive boilerplate. Non-form clients may use X-CSRF-Token.
    state_changing = request.method in {"POST", "PUT", "PATCH", "DELETE"}
    if request.method == "GET" and not request.url.path.startswith("/static/"):
        request.session.setdefault("_csrf_token", secrets.token_urlsafe(32))

    def reject_request(message: str, status_code: int = 403):
        rejected = PlainTextResponse(message, status_code=status_code)
        rejected.headers.setdefault("X-Content-Type-Options", "nosniff")
        rejected.headers.setdefault("X-Frame-Options", "DENY")
        rejected.headers.setdefault("Referrer-Policy", "same-origin")
        rejected.headers.setdefault("Cache-Control", "no-store")
        return rejected

    if state_changing:
        # Preserve the existing exact-origin defense for authenticated dashboard
        # actions. Login is still protected by a session-bound token, but isn't
        # subject to proxy-origin assumptions that may otherwise break login.
        if request.url.path != "/login":
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            supplied = origin or referer
            if supplied:
                try:
                    supplied_origin = _origin_of(supplied)
                    allowed_origins = _allowed_origins(request)
                except ValueError:
                    return reject_request("Request origin could not be validated.")
                if supplied_origin not in allowed_origins:
                    return reject_request("Request origin did not match this dashboard.")

        # Parse once and replay the buffered body for FastAPI's downstream Form
        # dependencies. This lets the middleware validate CSRF fields without
        # consuming the form body before the route receives it.
        max_body_bytes = 8 * 1024 * 1024
        try:
            declared_length = int(request.headers.get("content-length", "0"))
        except ValueError:
            return reject_request("Invalid Content-Length.", status_code=400)
        if declared_length > max_body_bytes:
            return reject_request("Request body is too large.", status_code=413)

        original_receive = request._receive
        try:
            # Enforce the limit while streaming. Buffering request.body() first
            # would allow a huge chunked body to consume memory before rejection.
            body_chunks = []
            body_size = 0
            async for chunk in request.stream():
                body_size += len(chunk)
                if body_size > max_body_bytes:
                    return reject_request("Request body is too large.", status_code=413)
                body_chunks.append(chunk)
            body = b"".join(body_chunks)
            request._body = body
            form_data = await request.form()
        except Exception:
            return reject_request("Unable to validate request CSRF token.")

        submitted_token = form_data.get("_csrf_token") or request.headers.get("x-csrf-token")
        expected_token = request.session.get("_csrf_token")
        if (
            not isinstance(expected_token, str)
            or not isinstance(submitted_token, str)
            or not hmac.compare_digest(expected_token, submitted_token)
        ):
            return reject_request("CSRF validation failed. Reload the page and try again.")

        body_replayed = False

        async def replay_request_body():
            nonlocal body_replayed
            if not body_replayed:
                body_replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await original_receive()

        request._receive = replay_request_body

    response = await call_next(request)

    # Inject a per-session CSRF field into every rendered HTML POST form.
    # BaseHTTPMiddleware wraps downstream responses as streams, so buffer only
    # HTML pages and rebuild the response while retaining its status, headers,
    # cookies, and background task. Dashboard HTML pages are bounded UI output.
    csrf_token = request.session.get("_csrf_token")
    content_type = response.headers.get("content-type", "")
    if csrf_token and "text/html" in content_type and hasattr(response, "body_iterator"):
        token_bytes = csrf_token.encode("ascii")
        form_pattern = re.compile(
            rb"(<form\b(?=[^>]*\bmethod\s*=\s*(?:\"post\"|'post'|post)(?:\s|>))[^>]*>)(.*?</form\s*>)",
            re.IGNORECASE | re.DOTALL,
        )
        existing_token_pattern = re.compile(
            rb"<input\b(?=[^>]*\bname\s*=\s*(?:\"_csrf_token\"|'_csrf_token'|_csrf_token)(?:\s|>))[^>]*>",
            re.IGNORECASE,
        )
        hidden_field = (
            b'<input type="hidden" name="_csrf_token" value="'
            + token_bytes
            + b'">'
        )

        def inject_csrf_field(match):
            entire_form = match.group(0)
            if existing_token_pattern.search(entire_form):
                return entire_form
            return match.group(1) + hidden_field + match.group(2)

        original_body = b"".join([chunk async for chunk in response.body_iterator])
        secured_body = form_pattern.sub(inject_csrf_field, original_body)
        from starlette.responses import Response

        old_headers = [
            (key, value)
            for key, value in response.raw_headers
            if key.lower() not in {b"content-length", b"content-type", b"transfer-encoding"}
        ]
        background = getattr(response, "background", None)
        response = Response(
            content=secured_body,
            status_code=response.status_code,
            background=background,
        )
        response.raw_headers.extend(old_headers)
        response.headers["content-type"] = content_type
        response.headers["content-length"] = str(len(secured_body))
        response.headers.setdefault("Cache-Control", "private, no-store")

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self'; img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), usb=()")
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
            actor_id, actor_name = _current_identity(request)
            db.record_webui_action(guild_id, _client_ip(request), request.method, request.url.path,
                                   response.status_code, actor_id, actor_name)
        except Exception:
            pass
    return response


app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="lax",
    max_age=SESSION_TTL_SECONDS,
    https_only=_env_flag("WEBUI_HTTPS_ONLY"),
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def _nav_visible(items, level):
    """Sidebar entries a viewer may see. Staff only get the pages they can open."""
    if level != "staff":
        return items
    return [item for item in items if item[0] in STAFF_PAGES]


templates.env.filters["nav_visible"] = _nav_visible


DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "").strip()
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "").strip()
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "").strip()
DISCORD_API = "https://discord.com/api/v10"
OAUTH_CONFIGURED = bool(DISCORD_CLIENT_ID and DISCORD_CLIENT_SECRET and DISCORD_REDIRECT_URI)
# Optional local-only compatibility mode for development. Production compose
# requires OAuth credentials and does not pass WEBUI_PASSWORD.
WEBUI_PASSWORD = os.environ.get("WEBUI_PASSWORD", "")


def is_authed(request: Request) -> bool:
    session_id = request.session.get("session_id")
    if request.session.get("authenticated") is not True or not isinstance(session_id, str):
        return False
    try:
        return db.is_webui_session_valid(session_id)
    except Exception:
        # Fail closed if the session store cannot be checked.
        return False


TRUST_PROXY = _env_flag("WEBUI_TRUST_PROXY")
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)

# How long a user's Discord guild list is reused before asking Discord again.
# Without this every page view and every form POST hit Discord's API, which
# rate-limits quickly and then locks people out of servers they can manage.
# Demoting someone in Discord therefore takes up to GUILD_CACHE_TTL seconds to
# be noticed. If Discord is briefly unreachable, a cached answer up to
# GUILD_CACHE_STALE_GRACE seconds old is still used rather than failing open.
GUILD_CACHE_TTL = 60
GUILD_CACHE_STALE_GRACE = 300
_guild_cache: dict[str, tuple[float, list[dict]]] = {}
_session_locks: dict[str, asyncio.Lock] = {}


def _origin_of(url: str) -> tuple[str, str]:
    parts = urlsplit(url)
    return (parts.scheme.lower(), parts.netloc.lower())


def _allowed_origins(request: Request) -> set[tuple[str, str]]:
    """Origins a state-changing request may come from.

    Behind a TLS-terminating proxy (Tailscale Funnel, Caddy, nginx) the app only
    sees plain http, so ``request.base_url`` says ``http://host`` while the
    browser correctly sends ``Origin: https://host`` - and every POST would be
    rejected. The OAuth redirect URI is the one place the operator already
    declared the real public origin, so trust that as well.
    """
    allowed = {_origin_of(str(request.base_url))}
    if DISCORD_REDIRECT_URI:
        allowed.add(_origin_of(DISCORD_REDIRECT_URI))
    return allowed


def _safe_equal(a: str, b: str) -> bool:
    # hmac.compare_digest raises TypeError on non-ASCII str; compare bytes so a
    # hostile query string can't turn into a 500.
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _forget_session(session_id: str) -> None:
    _guild_cache.pop(session_id, None)
    _session_locks.pop(session_id, None)


def _current_identity(request: Request) -> tuple[Optional[str], Optional[str]]:
    """(discord_user_id, username) of the signed-in user, or (None, None)."""
    session_id = request.session.get("session_id")
    if not isinstance(session_id, str):
        return None, None
    row = db.get_webui_oauth_token(session_id)
    if row:
        return str(row[0]), str(row[1])
    if request.session.get("auth_mode") == "local-password":
        return "local", request.session.get("discord_username")
    return None, None


def _is_local_admin(request: Request) -> bool:
    return request.session.get("auth_mode") == "local-password" and not OAUTH_CONFIGURED


# What the "staff" access level can reach. Everything else (settings, automod,
# emergency tools, integrations, access control...) is full-access only.
# Pages are GET sections; actions are "section/sub" POST paths.
STAFF_PAGES = frozenset({
    "", "quickstart", "search", "moderation", "moderationqueue", "reports",
    "tickets", "modmail", "suggestions", "staffstats",
})
STAFF_ACTIONS = frozenset({
    "moderation/purge", "moderation/clear-warns", "moderation/add-warn", "moderation/remove-warn",
    "moderation/warn-notes", "moderation/case-edit", "moderation/case-void",
    "moderation/tempbans/unban", "moderation/action", "moderationqueue/decide",
    "reports/status", "reports/dismiss", "reports/resolve", "suggestions/status",
    "tickets/close", "modmail/unblock",
})


def _dashboard_level(guild_id_raw: str, user_id: str, is_owner: bool, is_manager: bool) -> tuple[Optional[str], bool]:
    """(level, can_manage_access) for one user in one server, or (None, False).

    The server owner always gets in. Administrators / Manage Server members get
    in unless the owner has narrowed access to named people only. Anyone listed
    on the Dashboard Access page gets the level they were given.
    """
    if is_owner:
        return "full", True
    try:
        gid = int(guild_id_raw)
    except ValueError:
        return None, False
    # A name on the access list wins over Discord permissions, so an
    # Administrator can be deliberately kept to the trimmed Staff view.
    granted = db.get_dashboard_access_level(gid, user_id)
    if granted:
        return granted, False
    if is_manager and db.get_dashboard_managers_allowed(gid):
        return "full", True
    return None, False


async def _fetch_guild_access(session_id: str) -> Optional[list[dict]]:
    """Ask Discord which servers this session's user may manage.

    Returns the list on success, ``[]`` when access is definitively gone (the
    session is revoked), or ``None`` for a transient failure (rate limit,
    Discord outage, network) so the caller can decide what to do.
    """
    token_row = db.get_webui_oauth_token(session_id)
    if not token_row:
        return []
    user_id, username, access_token, refresh_token, expires_at = token_row
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as client:
            if int(expires_at) <= int(time.time()) + 60:
                async with client.post(f"{DISCORD_API}/oauth2/token", data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                }, headers={"Content-Type": "application/x-www-form-urlencoded"}) as response:
                    if response.status in (400, 401):
                        # invalid_grant: the user revoked the app or the token
                        # was rotated elsewhere. End the session cleanly.
                        db.revoke_webui_session(session_id)
                        return []
                    if response.status != 200:
                        return None
                    refreshed = await response.json()
                access_token = refreshed["access_token"]
                refresh_token = refreshed.get("refresh_token", refresh_token)
                db.save_webui_oauth_token(session_id, user_id, username, access_token,
                                          refresh_token, int(time.time()) + int(refreshed.get("expires_in", 3600)))
            async with client.get(f"{DISCORD_API}/users/@me/guilds",
                                  headers={"Authorization": f"Bearer {access_token}"}) as response:
                if response.status == 401:
                    db.revoke_webui_session(session_id)
                    return []
                if response.status != 200:
                    return None
                guilds = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, TypeError):
        return None
    # Discord permission bitfield: ADMINISTRATOR=0x8, MANAGE_GUILD=0x20.
    accessible = []
    for guild in guilds if isinstance(guilds, list) else []:
        if not isinstance(guild, dict):
            continue
        try:
            permissions = int(guild.get("permissions", "0"))
        except (TypeError, ValueError):
            permissions = 0
        gid_raw = str(guild.get("id", ""))
        level, can_grant = _dashboard_level(
            gid_raw, str(user_id), guild.get("owner") is True, bool(permissions & (0x8 | 0x20)),
        )
        if level:
            accessible.append({
                "id": gid_raw, "name": str(guild.get("name", "")),
                "level": level, "can_grant": can_grant,
            })
    return accessible


async def discord_guild_access(request: Request) -> list[dict]:
    """The servers the current user may manage, per Discord.

    Tokens are stored server-side in SQLite, never in the signed browser cookie.
    Results are cached per session for GUILD_CACHE_TTL seconds, and a per-session
    lock stops concurrent requests from refreshing the same (rotating) refresh
    token twice, which would invalidate the session.
    """
    session_id = request.session.get("session_id")
    if not isinstance(session_id, str):
        return []
    if _is_local_admin(request):
        return [{"id": str(gid), "name": name, "level": "full", "can_grant": True} for gid, name in db.list_bot_guilds()]

    cached = _guild_cache.get(session_id)
    if cached and time.monotonic() - cached[0] < GUILD_CACHE_TTL:
        return cached[1]
    lock = _session_locks.setdefault(session_id, asyncio.Lock())
    async with lock:
        cached = _guild_cache.get(session_id)
        if cached and time.monotonic() - cached[0] < GUILD_CACHE_TTL:
            return cached[1]
        result = await _fetch_guild_access(session_id)
        if result is None:
            if cached and time.monotonic() - cached[0] < GUILD_CACHE_STALE_GRACE:
                return cached[1]
            return []
        if not result and not db.get_webui_oauth_token(session_id):
            _forget_session(session_id)
            return []
        _guild_cache[session_id] = (time.monotonic(), result)
        if len(_guild_cache) > 2000:
            oldest = sorted(_guild_cache.items(), key=lambda kv: kv[1][0])[:500]
            for key, _ in oldest:
                _forget_session(key)
        return result


async def accessible_guild_ids(request: Request) -> set[int]:
    return {int(g["id"]) for g in await discord_guild_access(request) if g.get("id", "").isdigit()}


async def _revoke_discord_tokens(access_token: str, refresh_token: str) -> None:
    """Best-effort: tell Discord to invalidate this session's tokens on logout."""
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as client:
            for token, hint in ((access_token, "access_token"), (refresh_token, "refresh_token")):
                async with client.post(f"{DISCORD_API}/oauth2/token/revoke", data={
                    "client_id": DISCORD_CLIENT_ID,
                    "client_secret": DISCORD_CLIENT_SECRET,
                    "token": token,
                    "token_type_hint": hint,
                }, headers={"Content-Type": "application/x-www-form-urlencoded"}):
                    pass
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass


async def require_auth(request: Request):
    """Require Discord OAuth and enforce per-user Manage Server access."""
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    parts = request.url.path.split("/", 3)
    if len(parts) >= 3 and parts[1] == "guild":
        try:
            guild_id = int(parts[2])
        except ValueError:
            guild_id = None
        entries = {int(g["id"]): g for g in await discord_guild_access(request) if g.get("id", "").isdigit()}
        entry = entries.get(guild_id) if guild_id is not None else None
        if entry is None or not db.is_bot_in_guild(guild_id):
            request.session.pop("guild_id", None)
            return RedirectResponse("/?error=no_permission", status_code=303)
        request.state.guild_level = entry.get("level", "full")
        request.state.can_grant_access = bool(entry.get("can_grant"))
        if request.state.guild_level == "staff":
            rest = parts[3] if len(parts) > 3 else ""
            section, _, sub = rest.partition("/")
            if request.method in ("GET", "HEAD"):
                permitted = section in STAFF_PAGES and not sub
            else:
                permitted = f"{section}/{sub.rstrip('/')}" in STAFF_ACTIONS
            if not permitted:
                return RedirectResponse(f"/guild/{guild_id}?error=staff_only", status_code=303)
    return None


def validate_channel(guild_id: int, channel_id: int, allowed_types: tuple[str, ...]) -> bool:
    return any(cid == channel_id and ctype in allowed_types for cid, _name, ctype, _pos in db.list_bot_channels(guild_id))


MESSAGE_LINK_RE = re.compile(r"discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)")


def parse_message_reference(raw: str) -> tuple[Optional[int], Optional[int]]:
    """Same parsing the bot's reaction-role cog does - duplicated here since
    the dashboard has no Discord connection and does not import the cogs.
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
    # Behind Tailscale Funnel / a reverse proxy every request otherwise appears
    # to come from the proxy, so the lockout and the dashboard log are useless.
    # Only honoured when WEBUI_TRUST_PROXY=1, because X-Forwarded-For is
    # attacker-controlled if clients can reach the dashboard port directly
    # (bind to 127.0.0.1 when using this). The last hop is the one the trusted
    # proxy itself appended.
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        if forwarded:
            return forwarded[:64]
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
        "oauth": "Discord sign-in failed. Please try again.",
        "config": "Discord OAuth is not configured on this dashboard yet.",
        "denied": "Discord authorization was cancelled.",
    }.get(error)
    return templates.TemplateResponse(request, "login.html", {
        "error": error_message,
        "oauth_configured": OAUTH_CONFIGURED,
        "legacy_password_enabled": bool(WEBUI_PASSWORD) and not OAUTH_CONFIGURED,
    })


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if is_locked_out(request):
        return RedirectResponse("/login?error=locked", status_code=303)
    if OAUTH_CONFIGURED or not WEBUI_PASSWORD:
        return RedirectResponse("/login?error=oauth", status_code=303)
    if not hmac.compare_digest(password.encode("utf-8"), WEBUI_PASSWORD.encode("utf-8")):
        record_failed_login(request)
        db.record_webui_login(_client_ip(request), False)
        return RedirectResponse("/login?error=failed", status_code=303)
    clear_failed_logins(request)
    old_session_id = request.session.get("session_id")
    if isinstance(old_session_id, str):
        db.revoke_webui_session(old_session_id)
    request.session.clear()
    session_id = secrets.token_urlsafe(32)
    db.create_webui_session(session_id, SESSION_TTL_SECONDS)
    request.session.update({"authenticated": True, "session_id": session_id,
                             "auth_mode": "local-password", "discord_username": "Local Dashboard Admin"})
    db.record_webui_login(_client_ip(request), True)
    return RedirectResponse("/", status_code=303)


@app.get("/auth/discord")
async def discord_oauth_start(request: Request):
    if not OAUTH_CONFIGURED:
        return RedirectResponse("/login?error=config", status_code=303)
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    from urllib.parse import urlencode
    params = urlencode({
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
        "state": state,
    })
    return RedirectResponse(f"https://discord.com/oauth2/authorize?{params}", status_code=303)


@app.get("/auth/discord/callback")
async def discord_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    expected_state = request.session.pop("oauth_state", None)
    if error:
        return RedirectResponse("/login?error=denied", status_code=303)
    if not OAUTH_CONFIGURED or not code or not isinstance(expected_state, str) or not _safe_equal(expected_state, state):
        return RedirectResponse("/login?error=oauth", status_code=303)
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as client:
            async with client.post(f"{DISCORD_API}/oauth2/token", data={
                "client_id": DISCORD_CLIENT_ID,
                "client_secret": DISCORD_CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": DISCORD_REDIRECT_URI,
            }, headers={"Content-Type": "application/x-www-form-urlencoded"}) as response:
                if response.status != 200:
                    return RedirectResponse("/login?error=oauth", status_code=303)
                token = await response.json()
            # Both scopes are required: "guilds" drives every permission check.
            if not {"identify", "guilds"} <= set(str(token.get("scope", "")).split()):
                return RedirectResponse("/login?error=oauth", status_code=303)
            async with client.get(f"{DISCORD_API}/users/@me", headers={
                "Authorization": f"Bearer {token['access_token']}"}) as response:
                if response.status != 200:
                    return RedirectResponse("/login?error=oauth", status_code=303)
                user = await response.json()
        old_session_id = request.session.get("session_id")
        if isinstance(old_session_id, str):
            db.revoke_webui_session(old_session_id)
            _forget_session(old_session_id)
        request.session.clear()
        session_id = secrets.token_urlsafe(32)
        db.create_webui_session(session_id, SESSION_TTL_SECONDS)
        username = user.get("global_name") or user.get("username") or "Discord user"
        db.save_webui_oauth_token(session_id, str(user["id"]), username,
                                  token["access_token"], token["refresh_token"],
                                  int(time.time()) + int(token.get("expires_in", 3600)))
        request.session["authenticated"] = True
        request.session["session_id"] = session_id
        request.session["discord_username"] = username
        db.record_webui_login(_client_ip(request), True, str(user["id"]), username)
        return RedirectResponse("/", status_code=303)
    except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, ValueError, TypeError, AttributeError):
        db.record_webui_login(_client_ip(request), False)
        return RedirectResponse("/login?error=oauth", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    session_id = request.session.get("session_id")
    if isinstance(session_id, str):
        token_row = db.get_webui_oauth_token(session_id)
        db.revoke_webui_session(session_id)  # also deletes the stored OAuth tokens
        _forget_session(session_id)
        if token_row and OAUTH_CONFIGURED:
            await _revoke_discord_tokens(token_row[2], token_row[3])
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# ---- dashboard-wide settings ----

@app.get("/dashboard-settings")
async def dashboard_settings_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    guild_id = request.session.get("guild_id")
    if guild_id and (guild_id not in await accessible_guild_ids(request) or not db.is_bot_in_guild(guild_id)):
        request.session.pop("guild_id", None)
        guild_id = None
    return templates.TemplateResponse(request, "dashboard_settings.html", {
        "guild_id": guild_id,
        "guild_name": db.get_guild_name(guild_id) if guild_id else None,
        "active": "dashboard_settings",
        "discord_username": request.session.get("discord_username"),
    })


# ---- dashboard log (login attempts + actions taken through the WebUI) ----
# Global rather than per-guild: login happens before a server is even
# selected. Login events are retained for operational auditing.

@app.get("/dashboard-log")
async def dashboard_log_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/login", status_code=303)
    # Multi-server dashboard: a signed-in Discord user may hold no server access
    # at all, so this page must only show their own sign-ins and the actions
    # taken in servers they can manage - never other people's IPs or servers.
    user_id, _name = _current_identity(request)
    if _is_local_admin(request):
        login_rows = db.list_webui_login_events(50)
        action_rows = db.list_webui_action_log(100)
    else:
        login_rows = db.list_webui_login_events(50, user_id=user_id)
        full_ids = {
            int(g["id"]) for g in await discord_guild_access(request)
            if g.get("id", "").isdigit() and g.get("level", "full") == "full"
        }
        action_rows = db.list_webui_action_log(100, guild_ids=full_ids)
    logins = [
        {"created_at": datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M:%S"), "ip": row[1], "success": bool(row[2])}
        for row in login_rows
    ]
    actions = [
        {
            "created_at": datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M:%S"), "guild_id": row[1],
            "guild_name": (db.get_guild_name(row[1]) if row[1] else None),
            # Other admins' IP addresses stay private; you only see your own.
            "ip": row[2] if (_is_local_admin(request) or row[7] == user_id) else "hidden",
            "user": row[6] or "Unknown",
            "method": row[3], "path": row[4], "status_code": row[5],
        }
        for row in action_rows
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

    allowed_ids = await accessible_guild_ids(request)
    accessible_guilds = [g for g in db.list_bot_guilds() if int(g[0]) in allowed_ids]
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
    allowed_ids = await accessible_guild_ids(request)
    if not db.is_bot_in_guild(guild_id) or guild_id not in allowed_ids:
        return RedirectResponse("/?error=no_permission", status_code=303)
    request.session["guild_id"] = guild_id
    return RedirectResponse(f"/guild/{guild_id}", status_code=303)


@app.post("/switch-guild")
async def switch_guild(request: Request):
    if (r := await require_auth(request)):
        return r
    request.session.pop("guild_id", None)
    return RedirectResponse("/", status_code=303)


# ---- overview ----

# Permissions the bot's features lean on, so the overview can say which ones
# the bot is missing in this server (Administrator covers all of them).
BOT_IMPORTANT_PERMISSIONS = (
    ("Manage Roles", 1 << 28), ("Manage Channels", 1 << 4), ("Kick Members", 1 << 1),
    ("Ban Members", 1 << 2), ("Moderate Members", 1 << 40), ("Manage Messages", 1 << 13),
    ("View Audit Log", 1 << 7), ("Move Members", 1 << 24),
)


def missing_bot_permissions(bot_permissions: int) -> list[str]:
    """Names of important permissions the bot lacks. An empty cache (0) means
    the bot hasn't reported yet, so nothing is flagged."""
    if not bot_permissions or bot_permissions & (1 << 3):
        return []
    return [name for name, bit in BOT_IMPORTANT_PERMISSIONS if not bot_permissions & bit]


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

    info = db.get_guild_info(guild_id)
    counts = db.get_server_counts(guild_id)
    server_info = {
        **info,
        "owner_name": member_label(guild_id, info["owner_id"]) if info["owner_id"] else "Unknown",
        "created": time.strftime("%Y-%m-%d", time.gmtime(info["created_at"])) if info["created_at"] else "Unknown",
        "humans": counts["humans"], "bots": counts["bots"],
        # Discord's own total can be larger than what the bot has cached.
        "total": info["member_total"] or counts["members"],
        "channels": counts["channels"], "roles": counts["roles"],
        "verification": info["verification_level"].replace("_", " ").title() or "Unknown",
        "missing_bot_perms": missing_bot_permissions(info["bot_permissions"]),
    }
    return render(
        request, "overview.html", guild_id, "overview",
        stats=stats, analytics_stats=analytics_stats, activity_days=activity_days, server_info=server_info,
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


# ---- welcome & autorole ----


# ---- custom commands ----


# ---- birthdays ----


# ---- counting ----


# ---- scheduled background tasks (reminders and nickname reverts) ----


# ---- permissions (tempnick access rule) ----


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

# ---- dashboard access (who may open this dashboard, and how much of it) ----

def _require_can_grant(request: Request) -> None:
    if not getattr(request.state, "can_grant_access", False):
        raise HTTPException(status_code=403, detail="Only the server owner or an Administrator can change dashboard access.")


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


# ---- fun command toggles ----

FUN_COMMANDS = [
    ("Social", [("hug", "Hug someone"), ("insult", "Playfully roast someone"), ("compliment", "Give someone a compliment"), ("pat", "Pat someone"), ("slap", "Playful slap"), ("highfive", "High-five someone"), ("ship", "Compatibility score")]),
    ("Games & Randomness", [("8ball", "Magic 8-ball"), ("coinflip", "Flip a coin"), ("roll", "Roll dice"), ("rps", "Rock-paper-scissors"), ("choose", "Pick between options"), ("wouldyourather", "Would-you-rather question")]),
    ("Text Toys", [("dadjoke", "Random dad joke"), ("mock", "MoCk TeXt"), ("reverse", "Reverse text")]),
    ("Leveling & Economy", [("rank", "Show your rank/level"), ("leaderboard", "XP leaderboard"), ("balance", "Show a coin balance"), ("daily", "Claim daily coins"), ("work", "Work a shift for coins"), ("pay", "Pay another member"), ("richest", "Richest members leaderboard")]),
]


# ---- music ----


# ---- starboard ----


# ---- suggestions ----


# ---- anti-nuke ----


# ---- raid detection ----

RAID_ACTIONS = {"alert": "Alert only", "kick_new": "Auto-kick new accounts", "lockdown": "Lock all channels"}


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

# Channel name each category's "Automatic" option creates/reuses - kept in
# sync with cogs/logging_cog.py's CATEGORY_LOG_CHANNEL_NAMES (that file is
# the bot process, which the webui container has no import path to since
# they only share the SQLite db, not Python modules).
LOG_CATEGORY_CHANNEL_NAMES = {
    "messages": "message-logs",
    "members": "member-logs",
    "moderation": "moderation-logs",
    "automod": "automod-logs",
    "tickets": "ticket-logs",
    "reports": "report-logs",
    "server": "server-logs",
    "voice": "voice-logs",
}


# ---- channel feed ----
# A read-only mirror of channel messages, written by cogs/channelfeed.py on
# the bot side when enabled. Lets you keep an eye on a server's channels
# from this dashboard alone, without the Discord client open.


# ---- AI ----


# ---- youtube ----


# ---- extras (leveling/economy leaderboards, counters, twitch, feeds, giveaways) ----

COUNTER_KINDS = {"members": "Member count", "online": "Online count", "bots": "Bot count", "channels": "Channel count"}


EXTRAS_MAX_AMOUNT = 1_000_000_000


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


# ---- reaction roles (read-mostly - adding one requires the bot to place
# the actual reaction on a message, so that stays a slash command; the
# dashboard can review what's configured and remove bindings) ----


# ---- temp voice ----


# ---- sticky roles ----


# ---- talk as the bot ----
# Whoever's logged into the dashboard can compose a message and have the
# bot send it into a text channel. The selected server is protected by the
# Discord OAuth permission check in require_auth.


# ---- verification ----


# ---- tickets ----


# ---- modmail ----


# ---- polls ----


# ---- server rules ----


# ---- member reports ----

REPORT_STATUS_LABELS = {"open": "Open", "reviewing": "Reviewing", "resolved": "Resolved", "dismissed": "Dismissed"}


# ---- staff activity ----


# ---- invite tracking ----


# ---- automod moderation queue ----


# ---- server-wide search ----


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


def _check_confirm_phrase(action: str, confirm_text: str) -> bool:
    expected = EMERGENCY_CONFIRM_PHRASES.get(action)
    return expected is not None and confirm_text.strip().upper() == expected


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




# Route handlers for each feature area live in webui/routes/ (mirrors cogs/),
# imported here purely to register them on `app` above. Order doesn't matter;
# everything these modules need already exists on `main` by this point.
from routes import community, dashboard, fun, moderation, music, notifications, utility  # noqa: E402,F401
