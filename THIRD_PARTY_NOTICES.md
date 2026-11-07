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


The music feature uses a separate **Lavalink** server for audio processing.

- Project: https://github.com/lavalink-devs/Lavalink
- License: MIT
- YouTube source plugin: https://github.com/lavalink-devs/youtube-source
- License: MIT

Lavalink is packaged as its own image under `lavalink/` and can be published
separately from the ReedMuhn bot image.
