# Reedmuhn Security Review

This is a static/code-level review of the uploaded project and the changes made for the dashboard update. It is not a substitute for a production penetration test.

## Fixed in this update

- - Dashboard login uses the shared `WEBUI_PASSWORD` with a per-IP failed-login lockout; Discord OAuth is not required.
- The guild picker only lists servers the logged-in account is actually authorized for, instead of every server the bot happens to be in.
- OAuth `state` parameter is a per-session random token checked with a constant-time comparison, closing the CSRF window on the login callback.

- Bot Manager role changes immediately invalidate the cached access decision for that guild, instead of waiting out the cache window.
- Dashboard guild routes now verify the requested guild is one the bot currently tracks.
- Dashboard channel/role/member selections are backed by bot-maintained caches and validated before configuration writes.
- Scheduled-event cancellation is scoped to the selected guild, preventing a user with dashboard access from deleting an event belonging to another guild by guessing its numeric database ID.
- Session signing uses a persistent random secret; the generated secret file is restricted to owner read/write where the filesystem permits it.
- Session cookies use `SameSite=Lax` and a 12-hour lifetime. `WEBUI_HTTPS_ONLY=1` can force the Secure cookie flag when the dashboard is served over HTTPS.
- Dashboard responses add `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, and a restrictive Content Security Policy.
- Same-origin checks are applied to authenticated POST actions when browsers provide an Origin header.
- Dashboard numeric settings have reasonable upper bounds to prevent accidental extreme values.
- Tempban/tempnick durations are bounded and nicknames are capped at Discord's 32-character limit.
- The arithmetic evaluator remains AST-based with a fixed operator whitelist rather than using `eval()`.
- SQL operations continue to use parameterized queries.

## Reviewed areas

- `webui/main.py`
- `webui/discord_oauth.py`
- `db.py`
- `webui/db.py`
- `bot.py`
- `cogs/moderation.py`
- `cogs/tempvoice.py`
- `cogs/youtube.py`
- `cogs/automod.py`
- `automod_checks.py`
- `utils.py`
- Docker configuration and dependency manifests

## Important limitations


- Access decisions are cached in-process per (guild, user) for up to 5 minutes to limit REST calls. A role change made directly in Discord (not through this dashboard's Bot Manager roles list) - e.g. an admin removing someone's Administrator permission - can take up to that window to be reflected here. Removing a Bot Manager role through the dashboard itself takes effect immediately.
- The cache is in-memory and per-process; it resets on restart and isn't shared across multiple webui instances if the deployment is ever scaled beyond one.
- `DISCORD_CLIENT_SECRET` and `DISCORD_TOKEN` are both required in the webui container's environment now (previously only `DISCORD_TOKEN` was needed by the bot container). Treat `.env` with the same care as any other secret store - do not commit it or log its contents.
- This has been reviewed at the code level only; the OAuth exchange, callback, and REST calls have not been exercised against Discord's live API as part of this review. Test the full login flow against a real Discord application before relying on it in production.

For public internet exposure, still use HTTPS (`WEBUI_HTTPS_ONLY=1`) - Discord OAuth authenticates *who* is logging in, but the traffic itself should still be encrypted.

## Dependency note

Dependency versions are intentionally expressed as minimum versions in the current project. Operators should periodically rebuild with current packages and review dependency advisories before exposing the dashboard publicly.
