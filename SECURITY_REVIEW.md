# Reedmuhn Security Review

This is a static/code-level review of the project as it currently stands. It is not a substitute for a production penetration test.

## Authentication model

The dashboard uses a single shared `WEBUI_PASSWORD` (set in `.env`) - there is no Discord OAuth and no per-user login. Anyone with the password gets full dashboard access to every guild the bot is in. This is intentional for a self-hosted, single-operator/small-staff deployment; it is **not** meant for handing out individual accounts to a large staff team.

## In place today

- Password login uses `hmac.compare_digest` (constant-time comparison) and a per-IP failed-login lockout (5 attempts / 5 minutes).
- The app refuses to start if `WEBUI_PASSWORD` isn't set - there is no default password to accidentally ship.
- Session cookies are signed with a persistent random secret (`webui_secret_key`, generated on first run and stored alongside `bot.db`, `chmod 600` where the filesystem allows it) - so sessions survive container restarts without needing a fixed secret in `.env`.
- Session cookies use `SameSite=Lax` and a 12-hour lifetime. Set `WEBUI_HTTPS_ONLY=1` to also mark them `Secure` once the dashboard is served over HTTPS - do this for anything reachable outside your LAN.
- Every authenticated POST request checks the `Origin`/`Referer` header against the dashboard's own origin (exact scheme+host match, not a prefix check) and rejects mismatches - this covers CSRF for the common case where a browser sends one of those headers.
- Responses set `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, and a restrictive `Content-Security-Policy`.
- Guild-scoped routes (`/guild/{guild_id}/...`) verify the bot is actually in that guild before doing anything, so a logged-in user can't act on a guild ID the bot doesn't track.
- Numeric settings (durations, multipliers, amounts) are bounds-checked; nicknames are capped at Discord's 32-character limit.
- The custom-commands arithmetic evaluator is AST-based with a fixed operator whitelist, not `eval()`.
- All SQL goes through parameterized queries. The handful of f-string-built statements in `db.py` only ever interpolate fixed column/table names from code, never request data - grepped and confirmed as part of this review.
- All Jinja2 templates render with autoescaping on; nothing uses the `|safe` filter to opt back out of it, so no template renders unescaped user-supplied text into HTML by default.

## Important limitations

- **No per-user accounts or audit trail of *who* took an action** - only "the dashboard did X" is logged (see `dashboard_log.html` / `record_webui_action`), not which staff member was at the keyboard, since everyone shares one password. If you need individual accountability, that would need a real multi-user login system, which this project doesn't have.
- **No CSRF token** - the Origin/Referer check covers browsers that send those headers, but a request with neither header present isn't rejected. This is a reasonable trade-off for a small trusted-staff tool but wouldn't pass a strict security audit.
- The Origin/Referer check and the session cookie's `Lax` SameSite policy assume you aren't embedding the dashboard in an iframe or reverse-proxying from a wildly different origin without adjusting these settings.
- This is a code-level review only - it hasn't been exercised against a live penetration test or fuzzing pass.

## Deployment recommendations for anyone with real users

- Set `WEBUI_HTTPS_ONLY=1` and put the dashboard behind HTTPS (a reverse proxy like Caddy/nginx with a real cert, or at minimum a Tailscale/VPN-only setup) if it's reachable from outside your own machine.
- Treat `WEBUI_PASSWORD` and the `data/` directory (which now also holds `webui_secret_key`) with the same care as any other secret - don't commit `.env`, don't log it.
- Rotate `WEBUI_PASSWORD` if it's ever shared with someone who no longer needs access - there's no per-user revocation, only the one shared password.
- Rebuild both containers periodically and check `requirements.txt` / `webui/requirements.txt` against current dependency advisories - versions here are expressed as minimums intentionally, so a stale build can silently miss security patches.
