# ReedMuhn Bot — continued code audit

## Source and test scope

- Source: supplied `Reedmuhn-bot-test-build-2026-10-22.zip` snapshot.
- No Git remote was configured and no changes were pushed to GitHub.
- Static review and regression tests only; no live Discord credentials or production server used.

## Fixed in this test build

### High: RSS feed SSRF

Before this pass, the extras cog called `feedparser.parse(url)` directly on a user-configured URL. That lets a configured feed target internal addresses and relies on feedparser's own redirect/network behavior. The bot now downloads the feed through `safe_feed.py`: HTTP(S) only, rejects credentials and unusual ports, validates every redirect, rejects non-global DNS results at connection resolution, does not use environment proxies, uses timeouts, and limits the response to 2 MiB. `feedparser` parses the fetched bytes only. Both the slash command and dashboard validate URL syntax/obvious unsafe targets before persisting the feed.

### Medium: poll closure race / option validation

Before this pass, `cast_poll_vote()` wrote votes without checking `polls.closed` or validating `option_index`. The callback checked closure earlier, but closure could race between that read and the write. The write now rechecks the poll under a SQLite immediate transaction and rejects closed/missing polls and invalid option indexes. The interaction reports that the poll is closed/unavailable when the write is rejected.

### Existing changes retained

- Poll voter lists in Discord embeds and the WebUI.
- Persistent poll button views re-registered at startup.
- Theme picker placement adjusted for the top-navigation layout.
- WebUI CSRF, origin checks, server-side session revocation, and existing route-wide regression tests.

## Verification

- `pytest -q`: 662 passed; one Starlette deprecation warning.
- `python -m compileall -q .`: passed.
- `safe_feed.py` and `webui/safe_feed.py` are identical.

## Not verified / follow-up priorities

1. Build and boot the bot/WebUI/Lavalink/Redis images with Docker or Podman.
2. Test poll button behavior after a real bot restart, close while votes are in flight, and test voters who left the server.
3. Exercise every command and dashboard setting in a disposable Discord server, including permission denials and API failures.
4. Review music/Lavalink, anti-nuke/raid, reaction roles, tickets, giveaways, AutoMod, leveling/economy, scheduled jobs, and all integrations with integration tests.
5. Run dependency vulnerability checks in a network-enabled environment and review dependency pinning/reproducibility.
6. Compare this archive with the current GitHub branch before adopting it; this snapshot may not contain the newest commits.

This is not a claim that every feature works or that the project is feature-equivalent to Carl-bot. No changes were pushed to GitHub.

## Additional bugs fixed in the follow-up pass

### High: polls could remain visually open after being closed

The database close flag was committed before the Discord message was edited, but message-edit failures were swallowed. A second close attempt returned early because the DB already said `closed`, so the stale embed and enabled-looking buttons could never be finalized. The close handler now retries message updates even for already-closed polls, can propagate Discord API failures to the durable scheduler, and treats deleted channels/messages as completed. The WebUI now exposes a retry action on closed polls and queues the same idempotent handler.

### Medium: giveaways could get stranded after an announcement failure

The `ended` flag is also used as the atomic claim to prevent duplicate winners. If posting the winners announcement failed, that flag could stay set permanently, stopping the periodic retry. The code now releases the claim and re-raises announcement/retrieval failures; the loop can retry and the slash command reports the failure instead of falsely saying the giveaway was not found.

### Low: mismatched HTTP/HTTPS port acceptance

The feed URL validator allowed `http://host:443` and `https://host:80` because it treated either standard port as acceptable for either scheme. It now requires port 80 for HTTP and port 443 for HTTPS if a port is explicitly supplied.

## Follow-up verification

- `pytest -q`: 665 passed, 3 skipped, one Starlette deprecation warning.
- The 3 runtime-specific regression tests are skipped here because `discord.py` (and, for the giveaway test, `feedparser`) are not installed in this audit runtime. They are included for an environment with the bot dependencies installed.
- `python -m compileall -q .`: passed.
- `node --check` passed for every `webui/static/*.js` file.
- WebUI import smoke test passed (179 routes registered).
- A smoke pass over all 45 guild-scoped GET routes rendered without server errors against a disposable SQLite database.
