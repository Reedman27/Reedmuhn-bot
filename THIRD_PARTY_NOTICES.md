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

## Lavalink

The music feature uses a separate **Lavalink** server for audio processing.

- Project: https://github.com/lavalink-devs/Lavalink
- License: MIT
- YouTube source plugin: https://github.com/lavalink-devs/youtube-source
- License: MIT

Lavalink is packaged as its own image under `lavalink/` and can be published
separately from the ReedMuhn bot image.
