# cogs/

Each folder is one area of the bot. Every `.py` file with an `async def setup(bot)` is loaded automatically at boot - there is no list to edit.

| Folder | What lives here |
|---|---|
| `moderation/` | moderation, automod, antinuke, raiddetection, emergency, cases, rules, reports, votekick, verification, modmail, logging_cog |
| `community/` | welcome, birthdays, counting, reactionroles, stickyroles, starboard, suggestions, invites |
| `utility/` | customcommands, reminders, tempvoice, tickets, userinfo, serverinfo, say, ping, ai |
| `fun/` | fun, polls, extras (XP, economy, giveaways, counters, Twitch/RSS) |
| `music/` | music |
| `notifications/` | youtube |
| `dashboard/` | dashboardtalk, dashboardmoderation, channelfeed - cogs that only exist to serve the web dashboard |

## Add a cog
1. Drop `mything.py` into the right folder (or make a new folder with an empty `__init__.py`).
2. Give it an `async def setup(bot)` that adds your cog.
3. Restart. It shows up in the boot report. Optionally add a `Feature(...)` line in `bot.py` so it has a description (the boot report flags cogs that don't).

## Turn a cog off
Put its name in `DISABLED_COGS` in `.env`, e.g. `DISABLED_COGS=music,fun.polls`, and restart. Nothing is deleted.

## Boot report
Set `BOOT_LOG_CHANNEL_ID` and every restart posts one embed per folder listing each file as loaded, disabled, or failed (with the error). A broken cog never stops the others from loading.

## Rules of thumb
- One cog per file, and each file owns its own commands, so you can edit one feature without touching the rest.
- Cogs find each other with `bot.get_cog("Logging")`, not imports, so folder moves don't break them.
- Discord allows 100 top-level slash commands. Group related ones in an `app_commands.Group`.
