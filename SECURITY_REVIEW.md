# Reedmuhn Security Review

This is a static/code-level review of the project as it currently stands. It is not a substitute for a production penetration test.

## Authentication model

The dashboard uses a single shared `WEBUI_PASSWORD` (set in `.env`) - there is no Discord OAuth and no per-user login. Anyone with the password gets full dashboard access to every guild the bot is in. This is intentional for a self-hosted, single-operator/small-staff deployment; it is **not** meant for handing out individual accounts to a large staff team.

## In place today

- Password login uses `hmac.compare_digest` (constant-time comparison) and a per-IP failed-login lockout (5 attempts / 5 minutes).
- The app refuses to start if `WEBUI_PASSWORD` isn't set - there is no default password to accidentally ship.
- Session cookies are signed with a persistent random secret (`webui_secret_key`, generated on first run and stored alongside `bot.db`, `chmod 600` where the filesystem allows it) - so sessions survive container restarts without needing a fixed secret in `.env`.
- Session cookies use `SameSite=Lax` and a 12-hour lifetime. A random session ID is also recorded server-side, so logout revokes that session even if a copy of its signed cookie exists. Set `WEBUI_HTTPS_ONLY=1` to mark cookies `Secure` when the dashboard is served over HTTPS.
- Every state-changing request (POST/PUT/PATCH/DELETE, including password login) requires a cryptographically random CSRF token tied to the signed session. The response middleware inserts a hidden token into rendered HTML POST forms, and clients can alternatively send `X-CSRF-Token`. Non-login mutating requests also keep the exact `Origin`/`Referer` check as defense in depth. Requests without a valid token fail closed with HTTP 403.
- Responses set `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, and a restrictive `Content-Security-Policy`.
- Guild-scoped routes (`/guild/{guild_id}/...`) verify the bot is actually in that guild before doing anything, so a logged-in user can't act on a guild ID the bot doesn't track.
- Numeric settings (durations, multipliers, amounts) are bounds-checked; nicknames are capped at Discord's 32-character limit.
- The custom-commands arithmetic evaluator is AST-based with a fixed operator whitelist, not `eval()`.
- All SQL goes through parameterized queries. The handful of f-string-built statements in `db.py` only ever interpolate fixed column/table names from code, never request data - grepped and confirmed as part of this review.
- All Jinja2 templates render with autoescaping on; nothing uses the `|safe` filter to opt back out of it, so no template renders unescaped user-supplied text into HTML by default.

## Important limitations

- **No per-user accounts or audit trail of *who* took an action** - only "the dashboard did X" is logged (see `dashboard_log.html` / `record_webui_action`), not which staff member was at the keyboard, since everyone shares one password. If you need individual accountability, that would need a real multi-user login system, which this project doesn't have.
- The current password model remains single-operator/shared-password: there is no per-user identity or per-user audit trail. Logging out revokes the current browser session, but changing the shared password is still the way to rotate access for everyone. Discord OAuth is intentionally not part of this patch.
- The Origin/Referer check and the session cookie's `Lax` SameSite policy assume you aren't embedding the dashboard in an iframe or reverse-proxying from a wildly different origin without adjusting these settings.
- This is a code-level review only - it hasn't been exercised against a live penetration test or fuzzing pass.

## Deployment recommendations for anyone with real users

- Set `WEBUI_HTTPS_ONLY=1` and put the dashboard behind HTTPS (a reverse proxy like Caddy/nginx with a real cert, or a trusted VPN-only setup) if it's reachable from outside your own machine.
- The Channel Feed is opt-in, expires messages after 7 days, and keeps at most 300 messages per channel. Turning it off purges that guild's stored feed content.
- Treat `WEBUI_PASSWORD` and the `data/` directory (which now also holds `webui_secret_key`) with the same care as any other secret - don't commit `.env`, don't log it.
- Rotate `WEBUI_PASSWORD` if it's ever shared with someone who no longer needs access; there are no per-user accounts or per-person access controls.
- Rebuild both containers periodically and check `requirements.txt` / `webui/requirements.txt` against current dependency advisories - versions here are expressed as minimums intentionally, so a stale build can silently miss security patches.
