# Third-Party Notices

## Vocard — music subsystem

ReedMuhn's music subsystem (`voicelink/`, `langs/`, and `cogs/music/music.py`) is
based on **Vocard**, created by **Vocard Development / ChocoMeow**.

- Original project: https://github.com/ChocoMeow/Vocard
- License: MIT (full text preserved in `voicelink/VOCARD_LICENSE`)

The Vocard copyright/license header remains in the substantial derived source
files. ReedMuhn's port changes the surrounding bot integration and replaces
Vocard's document persistence layer with SQLite in `voicelink/sqlite_db.py`.
The `/music` commands are exposed as ReedMuhn's single `music` application
command group so the bot stays under Discord's top-level command limit.

## count-bot — counting-game reference / attribution

The counting game (`cogs/community/counting.py`, plus the `counting` table in `db.py`)
follows the game rules of **count-bot**, a Discord counting-game bot.

- Original project: https://git.nidus.me.uk/whaletech07/count-bot
- License: GNU Affero General Public License v3.0

count-bot's `bot.py` is not run as-is or imported at runtime - ReedMuhn's
`cogs/community/counting.py` is its own implementation built on ReedMuhn's cog/database
framework (slash-command groups, SQLite via `db.py`, WebUI settings at
`/guild/{id}/counting`). The rules it follows are: wrong number or counting
twice in a row both fail the same way. Saves are server-wide (see
`Db.apply_count_attempt`).

(2026-11-02: the counting cog was replaced with the rewritten version. The
`/counting calc` command, the per-number milestone reaction emoji and the
per-member save bank that were ported from count-bot are gone; `utils.safe_eval`
is no longer used by anything but is left in place.)

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
