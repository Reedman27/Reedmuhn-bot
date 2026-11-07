# Upgrading an existing checkout

This zip is a full snapshot of the tree, not a git patch. If you unzip it
**over** a folder that already has an older copy of the bot (rather than
replacing the folder entirely), files this version removed, renamed or
merged will NOT be deleted automatically — they'll just sit there unused.
This page is the list of what to clean up, and what's new.

**Safest option:** don't overlay. Keep your `.env` and `data/` (and
`lavalink/plugins/` if you added any) somewhere safe, delete everything
else, unzip this fresh, put `.env` and `data/` back. Everything below is for
people who'd rather overlay and clean up by hand.

## Delete these if present

These either no longer exist, were merged into something else, or were
replaced:

| Path | Why it's gone |
|---|---|
| `webui/db.py` | Merged into the one root-level `db.py`, which both the bot and the dashboard now import. The dashboard's Dockerfile copies it in at build time — see `webui/Dockerfile`. |
| `webui/redis_client.py` | Same — now a single shared copy at the repo root. |
| `webui/safe_feed.py` | Same. |
| `webui/.dockerignore` | No longer needed (single `.dockerignore` at the repo root covers both images now that the build context is the repo root). |
| `third_party/DINGOLINGO-GPL-3.0.txt` | DingoLingo was a design reference only, never a runtime dependency; the license file and its mention in `THIRD_PARTY_NOTICES.md`/`README.md` are removed. |
| `third_party/RED-DISCORD-BOT-LICENSE.txt` | Same situation — not an actual dependency, removed. |
| `AUDIT_2026-11-02.md`, `AUDIT_2026-11-06.md`, `CHANGES_2026-11-02.md`, `CHANGES_2026-11-06.md`, `SECURITY_AUDIT_REPORT.md`, `SECURITY_REVIEW.md`, `TESTING_NOTES_2026-10-22.md` | These were one-off dated audit/change-log files from past sessions — exactly the kind of file that makes a repo accumulate junk. Replaced by two files that get updated in place instead of growing forever: `SECURITY.md` (the vulnerability history) and `CHANGELOG.md` (everything else). |

If you have any other dated `AUDIT_*.md`, `CHANGES_*.md`, `TESTING_NOTES_*.md`
or similarly one-off `.md` files from earlier rounds that aren't `README.md`,
`QUICKSTART.md`, `SECURITY.md`, `CHANGELOG.md`, `THIRD_PARTY_NOTICES.md`,
`UPGRADING.md`, `LICENSE`, or `cogs/README.md`/`docs/screenshots/README.md` —
those are leftovers too. This version doesn't add any more of them going
forward; new findings go into `SECURITY.md`, new behavior changes into
`CHANGELOG.md`.

## New paths

| Path | What it is |
|---|---|
| `SECURITY.md` | Current security model + the full fixed-vulnerability history (replaces the removed audit files). |
| `CHANGELOG.md` | Everything else that's changed, by date. |
| `UPGRADING.md` | This file. |
| `docs/screenshots/` | Empty folder with instructions for dropping in your own dashboard screenshots. |
| `lavalink/entrypoint.sh` | Makes the bundled Lavalink image refuse to start with no password, instead of a hardcoded default. |
| `requirements-dev.txt` | Test-only deps (`pytest`, `pytest-asyncio`, `httpx`). The test suite needs `pytest-asyncio` or ~20 async tests fail for environment reasons, not code reasons. |
| `cogs/utility/serverinfo.py` | `/serverinfo` command. |
| `webui/routes/` | The dashboard's ~190 routes used to all live in one 4,000-line `webui/main.py`. They're now split into `webui/routes/{moderation,community,utility,fun,music,notifications,dashboard}.py`, mirroring the `cogs/` folder layout (see `cogs/README.md`'s table for the same grouping). `webui/main.py` keeps only the shared app setup, login/OAuth, and the overview page, and imports these at the bottom to register their routes. Nothing about how the dashboard behaves changed — this is a pure reorganization, and the full dashboard test suite (762 tests) passes identically before and after. |
| `tests/test_tempvoice.py`, `webui/tests/test_tempvoice_and_serverinfo.py` | Tests for the create-a-VC creator permissions and server info features (see `CHANGELOG.md`). |

## Config changes that need action if you're already running this

- **Lavalink (music) is now opt-in.** `docker-compose.yml`'s `lavalink`
  service has `profiles: ["music"]`, so a plain `docker compose up -d` no
  longer starts it. If you use music, either add
  `COMPOSE_PROFILES=music` to your `.env` so a plain `up -d` still includes
  it, or always run `docker compose --profile music up -d`. Without
  `LAVALINK_PASSWORD` set, the bot now starts normally with music shown as
  **off** in the boot report, instead of refusing to start at all.
- **The dashboard image's build context changed** from `./webui` to the
  repo root (`.`), because its Dockerfile now needs to copy in the shared
  `db.py`/`redis_client.py`/`safe_feed.py` from the root. If you have your
  own CI or deploy scripts that build the webui image directly (rather than
  through the provided `docker-compose.yml`), update them: `dockerfile:
  webui/Dockerfile` with `context: .`, not `context: ./webui`.

## After upgrading

Rebuild both images (`docker compose up -d --build`, or
`--profile music` if you use it) and watch the boot report — it lists every
cog, loaded or not, in Discord. Nothing here needs a database migration;
`db.py`'s existing `ALTER TABLE ... IF NOT EXISTS`-style upgrade code handles
new columns/tables automatically on first boot.
