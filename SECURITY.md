# Security

This is a code-level review of the project as it stands, kept as one document
instead of a pile of dated audit files. It is not a substitute for a
production penetration test, and nothing here has been run against a live
Discord connection — see "Not run against live Discord" at the bottom.

## Authentication model

The dashboard uses Discord OAuth2 (`identify guilds`) for individual user
identity. A server is only shown to a signed-in user when they own it or have
`MANAGE_GUILD`/`ADMINISTRATOR` there, and the bot is installed in it too.
Guild-scoped requests re-check this on every request against a 60s per-session
cache of Discord's guild list — changing a URL's guild ID is not an
authorization mechanism. A user demoted in Discord loses dashboard access
within about a minute. If Discord is rate-limited or down, an answer up to 5
minutes old is used; with no cache, the request is denied (fail closed).

OAuth access/refresh tokens live server-side in SQLite, keyed to the revocable
dashboard session, never in the browser cookie. `WEBUI_PASSWORD` is a local
development fallback only; the production Compose setup requires OAuth
credentials and does not use it.

## In place today

- OAuth authorization-code flow, random session-bound `state`, server-side
  token storage, refresh-token rotation (serialised per session — Discord
  rotates refresh tokens, so two concurrent refreshes would otherwise kill the
  session), 10s timeout on every Discord call. A revoked grant ends the
  session.
- Logout deletes stored tokens and asks Discord to revoke them; tokens of
  expired sessions are purged.
- The Dashboard Log is scoped per viewer: your own sign-ins, and actions only
  in servers you can manage — other admins' IPs and changes are hidden.
- CSRF: every state-changing request needs a token tied to the signed
  session (form field or `X-CSRF-Token`); `Origin`/`Referer` is also checked
  (and accepts `DISCORD_REDIRECT_URI`'s origin, so a TLS-terminating proxy
  works); requests without a valid token fail closed (403).
- Session cookies: signed with a persistent random secret
  (`webui_secret_key`, generated on first run, `chmod 600` where possible),
  `SameSite=Lax`, 12-hour lifetime, revocable server-side even if a cookie
  copy survives. `WEBUI_HTTPS_ONLY=1` marks them `Secure`.
- Security headers: `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, a restrictive `Content-Security-Policy`.
- SQL goes through parameterized queries; the handful of f-string-built
  statements in `db.py` only interpolate fixed column/table names from code,
  never request data.
- Jinja2 autoescaping is on everywhere; no `|safe`/`Markup` used, so no
  template renders unescaped user text into HTML.
- Custom-commands arithmetic is AST-based with a fixed operator whitelist —
  no `eval()`.
- Numeric settings are bounds-checked; nicknames capped at Discord's 32-char
  limit.
- The music embed template language (`voicelink/placeholders.py`) is
  AST-validated (`safe_condition`): only literals, `@@variables@@`,
  comparisons and `and/or/not`. Variable values (track titles, etc.) are
  passed as data, never spliced into source text.
- RSS/Atom feed URLs must be HTTP(S), may not embed credentials or target
  localhost/private/link-local addresses or nonstandard ports; DNS results
  that resolve non-global are rejected, redirects are capped at 3 and
  re-validated, responses capped at 2 MiB, a 10s timeout, and the bot parses
  downloaded bytes itself rather than letting a library fetch arbitrary URLs.
- Music playback controls go through a shared vote/DJ authorization guard,
  matching the interactive player's own policy.
- Request bodies are capped (8 MiB) as they stream in, not after buffering.
- Temp-voice creator permissions are enforced as a hard allow-list in three
  places (dashboard form, database read/write, bot overwrite builder) — see
  `TEMP_VOICE_OWNER_PERMS` in `db.py`. Administrator, Manage Server and
  server-wide Manage Roles can never be granted this way, even from a
  hand-edited database.
- GitHub Actions are pinned to full commit SHAs; workflows use
  `contents: read`.

## Known limitations

- OAuth token rows are stored in SQLite. Protect the `data/` directory and
  backups as credentials — the container's app user and host admins can read
  them.
- The `Origin`/`Referer` check and `SameSite=Lax` assume the dashboard isn't
  embedded in an iframe; keep the external HTTPS origin consistent behind a
  reverse proxy.
- Deleting Channel Feed rows is an application/database delete — it does not
  promise forensic erasure from SQLite free pages, WAL files, or backups.
  Protect the data directory and backups accordingly.
- Dashboard port 8490 binds all interfaces by default; set
  `WEBUI_BIND_ADDRESS` to restrict it to loopback or a chosen LAN/VPN
  address.
- Login throttling (5 failures / IP / 300s) assumes real client IPs. Behind a
  proxy, set `WEBUI_TRUST_PROXY=1` (with `WEBUI_BIND_ADDRESS=127.0.0.1`) or
  every client looks like the proxy's IP.
- `requirements.txt`/`webui/requirements.txt` use `>=` ranges intentionally so
  security patches aren't silently missed by a stale pin, but that also means
  builds aren't fully reproducible — rebuild periodically and check current
  advisories.

## Fixed vulnerability history

Chronological, newest first. Severity is this project's own call, not a CVSS
score. "Status" reflects this repository as it stands — a line marked fixed
here is fixed in the current tree, whatever an older snapshot said.

| Date found | Severity | What was wrong | Fix | Where |
|---|---|---|---|---|
| 2026-11-06 | High | Music embed templates ran `eval()` with track titles spliced into the expression text. A crafted song title (e.g. containing a quote) could break out and run code — the empty `__builtins__` used was not a sandbox. | Replaced with `safe_condition()`: AST-validated, comparisons/`and`/`or`/`not` only; values passed as data. Attack-payload tests in `tests/test_placeholders.py`. | `voicelink/placeholders.py` |
| 2026-11-06 | Medium (latent, unreachable) | A leftover Vocard debug view (`voicelink/views/debug.py`) ran arbitrary Python via `exec`. Nothing in the bot referenced it. | Deleted. | `voicelink/views/debug.py` |
| 2026-11-06 | Medium | An Administrator added to Dashboard Access as "Staff" still got full dashboard access (Discord permission won over the explicit list entry). | An explicit Dashboard Access entry now wins for everyone except the server owner. | `webui/main.py` |
| 2026-11-06 | Low | Staff-level dashboard users could read what full-access users had changed in the action log. | The action log only shows servers where the viewer has Full access. | `webui/main.py` |
| 2026-11-02 → 2026-11-06 | Low | Lavalink fell back to the public default password `youshallnotpass` in Compose, the music cog and `application.yml` (reported fixed once, found not actually fixed, then fixed for real). | `docker-compose.yml`'s Lavalink service is now off by default (`docker compose --profile music up -d`); when enabled, `LAVALINK_PASSWORD` is required and the container's entrypoint refuses to start without it. | `docker-compose.yml`, `cogs/music/music.py`, `lavalink/` |
| 2026-11-02 | High | `/dashboard-log` showed every login IP and every action in every server to any signed-in Discord user, including people with no access to that server. | Scoped: own sign-ins only; actions only for servers you manage. | `webui/main.py` |
| 2026-11-02 | High | The CSRF/same-origin check compared `http://host` (what the app saw behind a reverse proxy) against the browser's `https://host`, so every POST 403'd behind such a proxy. | Also accepts the origin of `DISCORD_REDIRECT_URI`. | `webui/main.py` |
| 2026-11-02 | High | A comment promised a Discord guild-list cache; there wasn't one. Every page view/POST called the Discord API, which rate-limited and bounced managers out as "no permission". | 60s per-session cache, 5-minute stale grace on 429/outage, fail closed with no cache. | `webui/main.py` |
| 2026-11-02 | High | Concurrent requests could refresh the same rotating OAuth refresh token at once, killing the session. | Refresh is serialised per session. | `webui/main.py` |
| 2026-11-02 | Medium | OAuth tokens of expired sessions stayed in SQLite forever. | Purged with the session. | `db.py` |
| 2026-11-02 | Medium | Logout only deleted local session rows; Discord kept the OAuth grant's tokens valid. | Best-effort revoke at Discord on logout. | `webui/main.py` |
| 2026-11-02 | Medium | A revoked OAuth grant (Discord 400/401) just returned "no servers" forever instead of ending the session. | Session is ended and the user sent to login. | `webui/main.py` |
| 2026-11-02 | Medium | No HTTP timeout on Discord API calls (library default is 5 minutes). | 10s timeout on every call. | `webui/main.py` |
| 2026-11-02 | Low | Non-ASCII OAuth `state` raised `TypeError` (500) in the callback. | Compared as bytes. | `webui/main.py` |
| 2026-11-02 | Low | The OAuth callback didn't verify the `guilds` scope was actually granted. | Requires `identify` + `guilds`. | `webui/main.py` |
| 2026-11-02 | Low | `/dashboard-settings` could still show a server the user had just lost access to. | Re-checked on load. | `webui/main.py` |
| 2026-11-02 | Low | Behind a reverse proxy, every request showed the proxy's own IP, making the login log and lockout meaningless. | Opt-in `WEBUI_TRUST_PROXY=1`. | `webui/main.py` |
| 2026-10-12 | Medium | State-changing requests could be accepted without a CSRF token. | CSRF token required on every mutating request. | `webui/main.py` |
| 2026-10-12 | Medium | Music slash commands could directly mutate playback (skip/pause/etc.) without the vote/DJ policy the interactive player enforced. | Shared authorization guard added to mutating playback controls. | `cogs/music/music.py` |
| 2026-10-12 | Low | `GET /logout` changed state (a GET request should be side-effect-free). | Logout moved behind a POST with CSRF. | `webui/main.py`, `webui/templates/base.html` |
| 2026-10-12 | Low | Non-ASCII password attempts on the local-dev password login could raise `TypeError`, possibly bypassing the login lockout. | Fixed comparison; lockout applies regardless. | `webui/main.py` |
| 2026-10-12 | Low | `WEBUI_HTTPS_ONLY=true` (the string) wasn't interpreted as true. | Parsed as a real boolean. | `webui/main.py`, `.env.example` |
| 2026-10-12 | Low | Signed, stateless session cookies couldn't be revoked after logout if a copy of one existed. | Server-side session records with expiry/revocation checks. | `webui/main.py`, `db.py` |
| 2026-10-12 | Low | Request body size was only checked after the whole body was buffered. | Enforced as chunks stream in (8 MiB cap). | `webui/main.py` |
| 2026-10-12 | Low | Build contexts could pick up `.env` and runtime data. | `.dockerignore` added/fixed. | `.dockerignore` |
| 2026-10-12 | Low | GitHub Actions used floating tags and lacked least-privilege permissions. | Pinned to commit SHAs; `contents: read`. | `.github/workflows/` |
| 2026-10-12 | Low | Compose could pass empty deployment secrets into containers instead of failing before startup. | Superseded by OAuth requiring non-empty client ID/secret/redirect URI via Compose's required-variable syntax. | `docker-compose.yml` |
| 2026-10-12 | Privacy | Disabling the Channel Feed left stored message content behind; pruning only capped row counts, not age. | Disabling purges that guild's rows; retention capped at 7 days / 300 rows per channel. | `db.py`, `cogs/dashboard/channelfeed.py` |
| 2026-10-12 | Per advisory | `dnspython` was pinned to a version with a known advisory. | Updated to 2.6.1. | `requirements.txt` |
| 2026-10-22 | — (hardening, not a vuln) | RSS feed fetching tightened further: nonstandard-port and cross-scheme-port checks. | See "In place today" above. | `safe_feed.py` |
| 2026-10-22 | Low (race) | A poll vote could land after the poll had already closed (a write/close race). | Vote writes verify the poll is still open and the option index is valid inside one SQLite `BEGIN IMMEDIATE` transaction. | `db.py` |

## Not run against live Discord

Everything above is a code-level review and test-suite result, not a
production penetration test or live integration test. In particular, nobody
has run this against a real Discord application, a real Lavalink node, or a
real two-server two-account OAuth session. Run your own smoke test (login,
guild switching, a few mutating actions, music if enabled) before trusting it
with a server that matters to you.

## Reporting something

This is a personal/small-scale project without a formal disclosure program.
If you find something, open an issue describing it (skip exploit payloads in
a public issue if it's serious) or reach the maintainer directly.
