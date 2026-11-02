# Reedmuhn Security Review

This is a static/code-level review of the project as it currently stands. It is not a substitute for a production penetration test.

## Authentication model

The production dashboard uses Discord OAuth2 (`identify guilds`) for individual user identity. The bot can be installed in multiple servers, but the dashboard only exposes a server when the signed-in user owns it or has Discord's `MANAGE_GUILD` or `ADMINISTRATOR` permission, and the bot is installed there too. Guild-scoped requests repeat this authorization check on every request against a per-session cache (60 s) of Discord's guild list; changing a URL's guild ID is not an authorization mechanism. A user demoted in Discord therefore keeps access for at most about a minute. If Discord is rate-limiting or down, a cached answer up to 5 minutes old is used; with no cache the request is denied (fail closed).

OAuth access and refresh tokens are stored server-side in the shared SQLite database, keyed to the revocable dashboard session. They are not placed in the signed browser cookie. The optional `WEBUI_PASSWORD` path exists only as a local development compatibility mode when OAuth is not configured; production Compose requires OAuth credentials and does not pass that password.

## In place today

- OAuth uses the authorization-code flow with a random, session-bound `state` value, server-side token storage, refresh-token rotation, and Discord API identity/guild membership lookups.
- Refreshing an expiring token is serialised per session (Discord rotates refresh tokens, so two concurrent refreshes would kill the session). A revoked grant (HTTP 400/401 from Discord) ends the dashboard session. All Discord calls have a 10 s timeout.
- Logout deletes the stored tokens and also asks Discord to revoke them. Tokens of expired sessions are purged, so they never outlive their session.
- The Dashboard Log is scoped per viewer: you see your own sign-ins and the actions taken in servers you can manage; other admins' IP addresses are hidden.
- The OAuth callback requires both the `identify` and `guilds` scopes and tolerates malformed `state` values.
- The same-origin check also accepts the origin of `DISCORD_REDIRECT_URI`, so POSTs work behind a TLS-terminating proxy such as Tailscale Funnel. Set `WEBUI_TRUST_PROXY=1` (with `WEBUI_BIND_ADDRESS=127.0.0.1`) so the log and lockout see real client IPs.
- Guild authorization accepts only server owners, `MANAGE_GUILD`, or `ADMINISTRATOR`; the bot must independently be present in the server.
- An optional local development password fallback uses `hmac.compare_digest` and a per-IP failed-login lockout. It is not passed into the production Compose container.
- Session cookies are signed with a persistent random secret (`webui_secret_key`, generated on first run and stored alongside `bot.db`, `chmod 600` where the filesystem allows it) - so sessions survive container restarts without needing a fixed secret in `.env`.
- Session cookies use `SameSite=Lax` and a 12-hour lifetime. A random session ID is also recorded server-side, so logout revokes that session even if a copy of its signed cookie exists. Set `WEBUI_HTTPS_ONLY=1` to mark cookies `Secure` when the dashboard is served over HTTPS.
- Every state-changing request (POST/PUT/PATCH/DELETE, including password login) requires a cryptographically random CSRF token tied to the signed session. The response middleware inserts a hidden token into rendered HTML POST forms, and clients can alternatively send `X-CSRF-Token`. Non-login mutating requests also keep the exact `Origin`/`Referer` check as defense in depth. Requests without a valid token fail closed with HTTP 403.
- Responses set `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, and a restrictive `Content-Security-Policy`.
- Guild-scoped routes (`/guild/{guild_id}/...`) verify both the user's current Discord permissions and the bot's membership before doing anything.
- Numeric settings (durations, multipliers, amounts) are bounds-checked; nicknames are capped at Discord's 32-character limit.
- The custom-commands arithmetic evaluator is AST-based with a fixed operator whitelist, not `eval()`.
- All SQL goes through parameterized queries. The handful of f-string-built statements in `db.py` only ever interpolate fixed column/table names from code, never request data - grepped and confirmed as part of this review.
- All Jinja2 templates render with autoescaping on; nothing uses the `|safe` filter to opt back out of it, so no template renders unescaped user-supplied text into HTML by default.

## Important limitations

- This implementation has been tested without a live Discord OAuth application. The OAuth callback, token exchange, refresh behavior, and live guild permission response still need an end-to-end test against the configured Discord application.
- OAuth token rows are stored in the SQLite database. Protect the `data/` directory and backups as credentials; the container's app user and host administrators can access them.
- The Origin/Referer check and the session cookie's `Lax` SameSite policy assume the dashboard is not embedded in an iframe. When reverse-proxying, ensure the external HTTPS origin is preserved consistently.
- This is a code-level review only - it has not been exercised against a live penetration test or fuzzing pass.

## Deployment recommendations for anyone with real users

- Set `WEBUI_HTTPS_ONLY=1` and put the dashboard behind HTTPS (a reverse proxy like Caddy/nginx with a real cert, or a trusted VPN-only setup) if it's reachable from outside your own machine.
- The Channel Feed is opt-in, expires messages after 7 days, and keeps at most 300 messages per channel. Turning it off purges that guild's stored feed content.
- Treat `DISCORD_CLIENT_SECRET`, OAuth token rows, and the `data/` directory (which holds `bot.db` and `webui_secret_key`) as secrets. Do not commit `.env` or expose database backups.
- Revoke the Discord OAuth grant for a user who should lose dashboard access; their Discord server permissions are also rechecked against the API on guild-scoped requests.
- Rebuild both containers periodically and check `requirements.txt` / `webui/requirements.txt` against current dependency advisories - versions here are expressed as minimums intentionally, so a stale build can silently miss security patches.
