# Curated build (2026-10-22) - based on GitHub main 1d7d019

Starts from the v3 test build, reviewed line by line against main. NOT pushed anywhere.

## Kept from v3 (reviewed, tested)
- Poll voter lists (Discord embed + WebUI), vote changes move the vote, race-safe voting, retryable close
- Theme picker opens downward in the top-nav layout
- Giveaway end is retried instead of being marked ended when the announcement fails
- SSRF-safe RSS fetching (safe_feed.py), CSRF tokens, revocable WebUI sessions, POST logout
- Music slash commands now honor the same DJ/vote rules as the buttons; /music leave uses teardown()
- Pinned GitHub Action SHAs (verified to exist upstream)

## Changed from v3 in this build
- Lavalink password is NOT mandatory again (port is internal-only); existing .env files keep working
- Channel Feed 7-day expiry is now opt-in via FEED_RETENTION_DAYS (default 0 = off); turning the feed off still clears stored messages, and the UI says so
- Poll embed: voter line sits directly under its option; voter lists shrink automatically if the embed nears Discord's 4096-char limit

## Behavior changes to be aware of when testing
- Everyone is logged out once after deploy (sessions are now server-side)
- Non-DJ members can only vote on pause/resume/skip/back/shuffle/leave; volume, loop, seek, remove, clear, filter, autoplay need DJ / Manage Server
- RSS feeds must use standard ports and public hosts
- docker compose needs DISCORD_TOKEN and WEBUI_PASSWORD in .env

## Verification
669 tests pass (discord.py installed), compileall clean, pyflakes shows no undefined names except dead Vocard code (voicelink/lyrics.py bs4). Not run: live Discord, container builds.
