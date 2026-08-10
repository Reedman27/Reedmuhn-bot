# Reedmuhn Security Review

This is a static/code-level review of the uploaded project and the changes made for the dashboard update. It is not a substitute for a production penetration test.

## Fixed in this update

- Dashboard guild routes now verify the requested guild is one the bot currently tracks.
- Dashboard channel/role/member selections are backed by bot-maintained caches and validated before configuration writes.
- Scheduled-event cancellation is scoped to the selected guild, preventing a user with dashboard access from deleting an event belonging to another guild by guessing its numeric database ID.
- Dashboard login keeps the existing failed-login rate limit.
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

## Important limitation

The dashboard still uses one shared `WEBUI_PASSWORD`. It does not know which Discord account is logged in, so the password represents full administrative access to every guild the bot can see. This is appropriate only for trusted self-hosted use.

For public internet exposure, use HTTPS and an additional access-control layer such as a VPN or authenticated reverse proxy. A future Discord OAuth implementation would be the stronger long-term solution for per-user authorization.

## Dependency note

Dependency versions are intentionally expressed as minimum versions in the current project. Operators should periodically rebuild with current packages and review dependency advisories before exposing the dashboard publicly.
