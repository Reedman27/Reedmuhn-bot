# ReedMuhn Quickstart

Ten minutes from zero to a protected, logged server.

## 1. Run it
```bash
cp .env.example .env      # fill in DISCORD_TOKEN, DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI
docker compose up -d
```
Music is optional: set `LAVALINK_PASSWORD` in `.env` (any long random string) and run `docker compose --profile music up -d` to add it. Without it the bot runs normally and the boot report shows music as off.

The dashboard is on port 8490. Invite the bot with the OAuth2 URL from the Discord Developer Portal (scopes `bot` + `applications.commands`).

## 2. Watch it boot
Set `BOOT_LOG_CHANNEL_ID` in `.env` to a private channel and restart. The bot posts every cog file it loaded, grouped by folder, with the error if one failed. Skip features you don't want with `DISABLED_COGS=music,fun`.

## 3. Choose who sees the dashboard
Sign in with Discord, pick your server, open **Dashboard Access** (Moderation & Safety).

- **Just you:** choose *Only the owner and the people below* and add nobody. You see everything.
- **You and staff:** add each moderator as **Staff**. They get a trimmed menu: Dashboard, Search, Moderation, Moderation Queue, Reports, Tickets, Modmail, Suggestions, Staff Activity, Quickstart. They can act on cases, warnings, reports and tickets, but can't change settings.
- **Trusted admins:** add as **Full**. They see everything except Dashboard Access.

By default (nothing changed) the owner, Administrators and anyone with Manage Server get full access. Only the owner and Administrators can edit the access list.

## 4. Turn on the essentials
1. **Logging** - auto-create the log channels.
2. **AutoMod**, **Raid Detection**, **Anti-Nuke** - pick your thresholds.
3. **Welcome & Roles**, **Verification** - greet and gate new members.
4. **Temporary Voice** (optional) - pick hub channels, then choose what each room's creator can do inside their own room, whether they get a temporary role, and where creations are logged. Members can read the setup with `/voicehub perms`.
5. Everything else (tickets, reaction roles, leveling, music...) is optional.

The dashboard **Overview** shows people vs bots, channels, roles, boosts, owner and server age, and warns you if the bot is missing a permission its features need. `/serverinfo` shows the same in Discord.

## 5. Make it yours
Each folder under `cogs/` is one area and each file is one feature, so you can change a feature without touching the others. See `cogs/README.md` for adding, disabling and organizing cogs.
