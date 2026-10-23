# Third-Party Notices

## Vocard — music subsystem

ReedMuhn's music subsystem (`voicelink/`, `langs/`, and `cogs/music.py`) is
based on **Vocard**, created by **Vocard Development / ChocoMeow**.

- Original project: https://github.com/ChocoMeow/Vocard
- License: MIT (full text preserved in `voicelink/VOCARD_LICENSE`)

The Vocard copyright/license header remains in the substantial derived source
files. ReedMuhn's port changes the surrounding bot integration and replaces
Vocard's document persistence layer with SQLite in `voicelink/sqlite_db.py`.
The `/music` commands are exposed as ReedMuhn's single `music` application
command group so the bot stays under Discord's top-level command limit.

## DingoLingo — reference / attribution

**DingoLingo** was consulted as a reference for music-bot architecture and
playlist/URL-handling ideas. It is an older project and is **not treated as a
current runtime dependency of ReedMuhn**.

- Project: DingoLingo
- Source archive used for reference: `DingoLingo-master(read only).zip`
- License: GNU General Public License v3.0

No DingoLingo source file is required at runtime by ReedMuhn. The full GPL-3.0
license from the reference archive is retained in `third_party/DINGOLINGO-GPL-3.0.txt`
for clear attribution and provenance.

## count-bot — counting-game reference / attribution

The counting game (`cogs/counting.py`, plus the `counting`/`counting_users`
tables in `db.py`) is modeled on **count-bot**, a Discord counting-game bot.

- Original project: https://git.nidus.me.uk/whaletech07/count-bot
- License: GNU Affero General Public License v3.0

count-bot's `bot.py` is not run as-is or imported at runtime - ReedMuhn's
`cogs/counting.py` is its own implementation built on ReedMuhn's cog/database
framework (slash-command groups, SQLite via `db.py`, WebUI settings at
`/guild/{id}/counting`). It follows count-bot's game rules closely though:
wrong number or counting twice in a row both fail the same way (and both
count-bot and ReedMuhn's `/counting saves` and `/counting calc` derive from
its `simple_eval`-based `evaluate()`/`calc` design, reimplemented here as
`utils.safe_eval` - an AST-whitelist evaluator used in place of `simpleeval`
for tighter sandboxing), and the default milestone reaction emoji
(67 😒 / 100 💯 / 1234 🔢 / 2024 🐋) are carried over verbatim from count-bot's
`specialEmojis` dict, just made per-guild configurable instead of hardcoded.

No separate copy of count-bot's source or license text is kept in this repo:
ReedMuhn is itself licensed AGPL-3.0 (see the repository `LICENSE`), the same
license count-bot uses, so a second copy of the identical license text would
just be redundant - one AGPL-3.0 already covers the whole project, counting
game included, and ReedMuhn's own source is already made available to users
as the AGPL's network-use clause (§13) requires. (Compare this to the Vocard
entry above: that one *does* require a bundled license copy, because MIT's
license text has to travel with any copy of the code.)

## Lavalink

The music feature uses a separate **Lavalink** server for audio processing.

- Project: https://github.com/lavalink-devs/Lavalink
- License: MIT
- YouTube source plugin: https://github.com/lavalink-devs/youtube-source
- License: MIT

Lavalink is packaged as its own image under `lavalink/` and can be published
separately from the ReedMuhn bot image.
