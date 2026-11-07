# Changelog

Security fixes have their own history in `SECURITY.md`. This file is
everything else: features, behavior changes, reorganizations.

## 2026-11-06

- **Repo cleanup:** removed scattered dated audit/change-log files in favor of
  one `SECURITY.md` (vulnerability history) and this `CHANGELOG.md`. Removed
  `third_party/DINGOLINGO-GPL-3.0.txt` and `third_party/RED-DISCORD-BOT-LICENSE.txt`
  and their mentions — neither was a runtime dependency (DingoLingo was a
  design reference only).
- **Lavalink is now optional.** The bundled Lavalink service doesn't start
  with a plain `docker compose up -d` (`profiles: ["music"]` — start it with
  `docker compose --profile music up -d`). With no `LAVALINK_PASSWORD` set,
  the bot starts normally and the music cog shows as **off** (not failed) in
  the boot report; set the password to turn it on.
- **One shared `db.py`.** The bot and dashboard used to each carry their own
  copy of the database layer, and they had drifted apart. There is now a
  single `db.py` at the repo root that both import; the dashboard's Dockerfile
  copies it in at build time.
- **`webui/main.py` split into per-section routers** (mirroring `cogs/`), so a
  single ~3,900-line file isn't the only way to find a route.
- **Create-a-VC ("Temporary Voice") creator permissions:** admins pick what a
  room's creator can do inside their own room (rename/edit, manage
  permissions on that room, invites, mute, deafen, move, priority speaker).
  The list is a hard ceiling enforced in the dashboard, the database and the
  bot — Administrator, Manage Server and server-wide Manage Roles can never
  be granted this way. Optional temporary role (deleted with the room),
  optional log channel, one room per person, a per-server cap, a short
  cooldown, expiring invites. New `/voicehub rename`, `/voicehub invite`,
  `/voicehub perms` commands.
- **Server info:** the dashboard Overview and a new `/serverinfo` command show
  member counts (people vs. bots), channels, roles, boosts, owner, server age,
  verification level, and flag any bot permission the server is missing.
- Fixed a crash in the music embed template language when a template had no
  `??` branch.
- Corrected dashboard Permissions page text that claimed Bot Manager roles
  could open the dashboard (they never could).

## 2026-11-02

- **Dashboard rework:** per-user Discord OAuth2 (`identify guilds`) replacing
  shared-password-only access; a server is shown only when the signed-in user
  owns it or has `MANAGE_GUILD`/`ADMINISTRATOR` there and the bot is actually
  installed. 22 new OAuth tests against a fake Discord API.
- **Cog reorganization:** cogs moved into feature folders
  (`moderation/`, `community/`, `utility/`, `fun/`, `music/`, `notifications/`,
  `dashboard/`). Dropping a `.py` file with an `async def setup(bot)` into a
  folder is enough to add a cog — nothing to register by hand. A folder also
  works the same way (add an `__init__.py`).
- **Boot report:** the bot posts an embed on startup listing every cog by
  folder — loaded, failed (with the error), or disabled/off — instead of
  silently swallowing load failures.
- **Dashboard access levels:** Full vs. Staff access per server, with a
  per-viewer-scoped action log.
- **Counting game rewritten:** saves are now server-wide (one shared bank,
  default off) instead of per-member; only plain whole numbers count (math
  expressions no longer do); `/counting calc` and milestone-emoji reactions
  were removed in favor of `/counting save-config`. Existing databases upgrade
  automatically (old columns/tables are left in place, unused, rather than
  dropped).
- **AutoMod invite-link controls:** what used to be a single on/off switch is
  now `strike` / `delete` / `log` action modes, an allowlist (invite code,
  invite link, or server ID), an "allow this server's own invites" toggle, and
  exempt channels — all default to the old all-or-nothing behavior until
  configured.
- Sidebar/navigation cleanup: duplicate nav icons and dead markup removed;
  "Counting Game" and "Music" filed under Fun & Economy; "Live Counters" filed
  under Server Tools; the orphaned, unreachable `templates/extras.html` page
  deleted.
- Dashboard Log page no longer claims a shared password.

## 2026-10-22

- Poll embeds and the dashboard Polls page now show who voted for each option
  (grouped by choice, existing vote data reused — no migration needed).
- Theme picker in the new top-navigation layout now opens downward instead of
  overlapping the controls row.
- RSS/Atom feed URL validation tightened: explicit HTTP/HTTPS ports must match
  their scheme.
- Poll finalization retries editing the Discord message instead of giving up
  when a Discord API call fails mid-close; a "Retry Discord update" action was
  added to the dashboard for polls stuck in that state.
- Giveaway-ending failures now release the atomic "ended" claim and retry,
  instead of leaving a giveaway permanently stuck as ended with no winner
  announced.

## Earlier

Counting game, AutoMod, the OAuth dashboard rework and the CSRF/session
hardening all went through earlier iterations before the dates above; see
`SECURITY.md` for the security-relevant subset of that history. Nothing
earlier than 2026-10-12 is tracked here in detail.
