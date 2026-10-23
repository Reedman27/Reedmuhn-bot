# Security audit report — follow-up fixes

- **Follow-up date:** 2026-10-12
- **Source:** supplied `Reedmuhn-bot-security-audit.zip` project snapshot plus `SECURITY_AUDIT_REPORT.md` and `security-audit.patch` from the outer archive.
- **Repository metadata:** the extracted source does not include `.git`, so this follow-up could not independently verify the original commit, branch, or repeat the historical Git-history scan described in the supplied report.
- **OAuth:** not changed. The WebUI remains password-only; no Discord OAuth flow was introduced.
- **Secrets:** no `.env` file, database, live Discord token, or live refresh token was read or printed.

## Follow-up scope

Rechecked the findings in the supplied report against the extracted source. Reviewed the WebUI CSRF/origin middleware and login/session flow, session persistence, request-size enforcement, the channel-feed writer/retention paths in both database copies, music slash-command control permissions against the interactive controller, Lavalink password configuration, Compose required variables, deployment documentation, Docker ignore files, and all GitHub Actions workflows.

This is a code and regression-test review, not a production penetration test. The project snapshot has no `.git` history and the environment did not have Docker/Podman, the bot's `discord.py` dependency, or `pip-audit` installed; live integration and fresh dependency-advisory checks therefore remain outstanding.

## Findings

| # | Finding | Severity | Status | Where |
|---|---------|----------|--------|-------|
| 1 | State-changing requests could be accepted without a CSRF token | Medium | **Fixed in supplied snapshot; regression tests pass** | `webui/main.py` |
| 2 | `GET /logout` changed state | Low | **Fixed in supplied snapshot** | `webui/main.py`, `webui/templates/base.html` |
| 3 | Non-ASCII password attempts could raise `TypeError` and bypass normal throttling behavior | Low | **Fixed in supplied snapshot** | `webui/main.py` |
| 4 | `WEBUI_HTTPS_ONLY=true` was not interpreted as true | Low | **Fixed in supplied snapshot** | `webui/main.py`, `.env.example` |
| 5 | Compose used a predictable Lavalink password fallback | Low | **Fixed; Compose now requires the shared secret** | `docker-compose.yml` |
| 6 | Lavalink and music code still had a predictable password fallback | Low | **Fixed; both fail closed without environment configuration** | `lavalink/application.yml`, `cogs/music.py` |
| 7 | `dnspython` was pinned to an older version reported vulnerable in the supplied audit | Per advisory | **Updated to 2.6.1 in supplied snapshot; advisory scan not rerun in this follow-up** | `requirements.txt` |
| 8 | Python bytecode could be included in source/build context | Info | **Ignored by `.gitignore`/`.dockerignore`; whether any bytecode was historically tracked cannot be reverified without `.git`** | root, `webui/` |
| 9 | Build contexts could include `.env` and runtime data | Low | **Fixed by Docker ignore files** | `.dockerignore`, `webui/.dockerignore` |
| 10 | GitHub Actions used floating action tags or lacked minimal workflow permissions | Low | **Fixed; all workflow actions are pinned to full commit SHAs and workflows use `contents: read`** | `.github/workflows/` |
| 11 | Disabling the message feed left stored message content behind, and pruning only capped row counts | Privacy | **Fixed; disabling purges that guild's rows, writes re-check opt-in, and retention is 7 days plus 300/channel** | `db.py`, `webui/db.py`, `cogs/channelfeed.py`, `webui/templates/feed.html` |
| 12 | Signed stateless session cookies could not be revoked after logout if copied | Low | **Fixed with server-side session records and expiry/revocation checks** | `webui/main.py`, `webui/db.py` |
| 13 | Dashboard port 8490 publishes on all interfaces by default | Deployment-dependent | **Not changed intentionally; `WEBUI_BIND_ADDRESS` can restrict the bind to loopback or a chosen LAN/VPN address** | `docker-compose.yml`, `.env.example` |
| 14 | Music slash commands could directly mutate playback without the DJ/member-vote policy used by the interactive player | Medium | **Fixed; shared authorization guard added to mutating playback controls** | `cogs/music.py` |
| 15 | Request body size was checked only after buffering the entire request | Low | **Fixed; middleware enforces the 8 MiB limit as chunks stream in** | `webui/main.py` |
| 16 | Compose passed empty `DISCORD_TOKEN` or `WEBUI_PASSWORD` into containers instead of failing before startup | Low | **Fixed; all three required deployment secrets now use Compose required-variable expressions** | `docker-compose.yml` |

### Additional security details

- Music control policy allows ordinary members to vote for pause/resume/skip/back/leave/shuffle where the interactive player supports it. The requester exception only applies to skipping the current track; skipping directly to an arbitrary queue position still requires privilege.
- The Channel Feed deletes rows at the application/database level. It does not promise forensic erasure from pre-existing SQLite free pages, WAL files, snapshots, or backups; protect the data directory and backups accordingly.
- The WebUI remains a single shared-password interface. Anyone who knows that password has the same dashboard authority; there are no per-user identities or individual action attribution.

## Other reviewed areas

- **SQL injection:** inspected SQL construction in the WebUI database methods; values use placeholders, with interpolated identifiers coming from fixed code constants.
- **XSS:** Jinja autoescaping remains enabled; no use of `|safe` or `Markup` was found in the templates/source reviewed.
- **Login throttling:** the existing in-memory lockout remains (5 failures per IP per 300 seconds). A reverse proxy may cause all clients to share an observed source IP unless trusted-proxy handling is designed explicitly; forwarded headers are not trusted by the app.
- **Feed opt-in:** the feed defaults to disabled, and the message insert itself checks that the guild's flag is still enabled to close a disable/purge race.
- **`/say`:** the supplied audit reports it is owner/admin-gated and suppresses mentions; this follow-up did not re-review all bot cogs.

## Tests and checks run in this follow-up

| Command/check | Result |
|---|---|
| `python -m pytest webui/tests -q` | **642 passed**, 1 Starlette deprecation warning |
| `python -m compileall -q .` | Passed |
| YAML parse of `docker-compose.yml`, `lavalink/application.yml`, and all workflow `.yml` files | Passed |
| Structural Compose service check (`discord-bot`, `redis`, `lavalink`, `webui`) | Passed |
| Source-level guardrails for music authorization, required secrets, and pinned CI actions | Passed |

The original supplied report records prior `pip-audit` runs after the `dnspython` update as clean and records an earlier suite result of 631 tests. **That advisory scan was not repeated in this follow-up** because `pip-audit` is not installed here. `pip check` in the shared validation environment reports an unrelated global-environment conflict (`moviepy 2.2.1` requires Pillow `<12`, but Pillow `12.3.0` is installed); this is not a finding against the project's declared dependencies. The current environment also lacks `discord.py`, so music code was syntax-checked and AST-regression-tested, but not imported or exercised against a live Discord/Lavalink runtime.

## Not run / remaining limitations

- `docker compose config`, image builds, container startup, and a real Lavalink password handshake: Docker and Podman are unavailable in this environment. YAML parsing and the Compose variable expressions were checked, but those are not a substitute for booting the actual container image.
- Fresh dependency-advisory scan (`pip-audit`) and a fully isolated bot dependency install.
- Live Discord, Lavalink, Redis, migration-on-copy, and backup/restore tests.
- A line-by-line permission audit of every bot command in every cog. The previous report's heuristic identified commands without obvious guards; this follow-up manually addressed the music controls found to bypass the interactive policy, but did not claim to review every unguarded command.
- Container hardening (non-root user, dropped capabilities, read-only root filesystem/resource limits), base-image digest pinning, and a complete third-party licensing review remain outside this pass. Dependency ranges using `>=` also mean builds are not fully reproducible.

## Deployment notes

1. Back up `data/` before deploying. It contains the SQLite database and the WebUI signing key.
2. Set non-empty `DISCORD_TOKEN`, `WEBUI_PASSWORD`, and a strong random `LAVALINK_PASSWORD` in `.env` (for example, generate the Lavalink secret with `openssl rand -hex 32`). Compose now fails if any of these are blank. Never paste secrets into issue reports or chats.
3. Set `WEBUI_BIND_ADDRESS=127.0.0.1` when only a local reverse proxy or SSH tunnel should reach the dashboard, or set a specific trusted LAN/VPN address when that is the intended access path. Do not expose plain-HTTP dashboard access to the public internet.
4. Set `WEBUI_HTTPS_ONLY=1` only when HTTPS is actually active.
5. Rebuild/restart the bot and WebUI. The additive `webui_sessions` table is created automatically. No SQL migration command is needed.

**Rollback:** redeploy the previous code/images if needed; the additive session table is harmless to older code. However, feed records deleted when a guild disables the feed and records expired by the 7-day retention policy are not restored by a code rollback. Restore them only from an appropriate backup, if one exists.
