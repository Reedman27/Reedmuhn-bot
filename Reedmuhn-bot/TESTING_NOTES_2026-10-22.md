# Reedmuhn Bot review notes — 2026-10-22

## Scope and source limitation

This test build is based on the previously supplied `ReedMuhn-bot-security-fixed.zip` snapshot in the conversation library. Fetching the current GitHub branch was not possible in this environment, so compare this build with the current repository before replacing files. No Git remote was configured or pushed to; no changes were sent to GitHub.

## Changes in this test build

- Added `poll_voters()` to both SQLite DB implementations. It groups voter IDs by selected option and follows the current one-vote-per-user behavior.
- Discord poll embeds now include the voters for each option after a vote and when a poll is closed (up to ten mentions per option, with a count of additional voters).
- The WebUI Polls page now has expandable voter lists for each option. Existing votes remain in the same `poll_votes` table; no schema migration is required. Names use the cached guild member display name and fall back to a former-member label/ID.
- In the new top-navigation dashboard layout, the theme menu is aligned with the controls row and opens downward rather than above the picker. The classic sidebar layout keeps its upward-opening menu.
- Added regression tests for voter grouping, vote changes, and dashboard display.

## Automated checks

- `python -m compileall -q` on the project: passed.
- `pytest -q`: **644 passed**, with one existing Starlette deprecation warning.

## Not established by these checks

These are automated code tests, not a live Discord integration test. Test with a non-production Discord server before deployment, especially poll buttons after bot restarts, actual member-name cache population, permissions, poll closing, and the theme picker at desktop/mobile widths. This pass does not certify that every feature is equivalent to Carl-bot or that every command/integration works against live Discord APIs.

## Continued audit pass

### Additional fixes in this build

- **RSS/Atom SSRF protection:** feed URLs must use HTTP(S), may not contain credentials, may not target localhost/private/link-local IP literals, and may not use nonstandard ports. Actual fetches use a resolver that rejects non-global DNS results, do not trust proxy environment variables, validate every redirect, limit redirects to three, apply timeouts, and cap response bodies at 2 MiB. The bot parses the downloaded bytes rather than asking feedparser to fetch arbitrary URLs itself. Both the Discord `/extras feed-add` command and WebUI validate the URL before saving it.
- **Poll closure race:** vote writes now verify the poll is still open and the option index is valid inside a SQLite `BEGIN IMMEDIATE` transaction. A button click racing with closure cannot add a late vote after the poll is closed. Invalid indexes are rejected at the database boundary. The bot and WebUI database implementations are kept in sync.
- Added regression coverage for private/local feed URLs, unsafe ports and credentials, public URL syntax, non-global resolver addresses, closed polls and invalid poll option indexes.

### Updated automated checks

- `pytest -q`: **662 passed**, one existing Starlette lifespan deprecation warning.
- `python -m compileall -q .`: passed.
- Root and WebUI copies of `safe_feed.py` are identical.

### Remaining limitations

- No live Discord token/server was used. Slash-command registration, permissions and Discord API behavior still require a test server.
- Docker/Podman and `pip-audit` are not installed in the audit environment, so container builds and fresh dependency advisory checks were not run.
- This is still based on the supplied snapshot, not a fresh checkout of the current GitHub branch. Do not replace a newer working tree without comparing changes.
- The project has many independent cogs; this pass does not certify full Carl-bot parity or every feature. Music/Lavalink, anti-nuke/raid behavior, Discord permission edge cases, all background loops, and end-to-end dashboard-to-bot actions remain candidates for focused integration tests.

## Further bug-fix pass

- Fixed poll finalization to retry editing a Discord poll message even when the DB was already marked closed. Discord HTTP failures are propagated for scheduled auto-close retries instead of silently deleting the scheduled event. The WebUI includes a `Retry Discord update` action for closed polls.
- Fixed giveaway end failures so Discord message/entry-read failures release the atomic `ended` claim and retry rather than stranding a giveaway as ended. Failed slash-command announcements are reported to the user.
- Tightened RSS URL port checks so explicit HTTP and HTTPS ports must match their scheme.
- Added dashboard retry-route and cross-scheme port regression tests, plus optional Discord-runtime tests for poll/giveaway handling.
- Latest checks: **665 passed, 3 skipped**, one pre-existing Starlette deprecation warning; all Python files compile, all WebUI JS files pass `node --check`, WebUI import succeeds, and all 45 guild-scoped GET routes returned without server errors in a disposable DB smoke pass.
- Discord-specific tests were skipped in this runtime because bot dependencies `discord.py` and `feedparser` are not installed. No live Discord server or container build was exercised.
