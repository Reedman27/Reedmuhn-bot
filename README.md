# ReedMuhn Bot

A self-hosted Discord moderation and utility bot built with `discord.py`, SQLite, and a companion FastAPI web dashboard.

> Built primarily with [Claude](https://claude.ai) across an extended development session (architecture, cogs, database layer, web dashboard, security fixes). ChatGPT assisted with a later round of features and documentation, including parts of this README.


## What it does

- 🛡️ Moderation: tempbans, warnings, timeouts, kicks, purges, tempnick, and a configurable mute role
- 🚨 Automod: invite blocking, banned words, caps, mention spam, message spam, duplicate spam, and violation escalation
- 👋 Welcome messages, optional generated welcome cards, and autoroles
- 🎂 Birthday tracking and announcements
- 🔢 Counting with high scores and earned saves
- ⚡ Custom commands
- 🔔 Reminders and scheduled nickname/tempban actions
- 📺 YouTube upload notifications through RSS (no YouTube API key)
- 🎙️ Temporary voice channels
- 🎭 Reaction roles - react to a message to get a role, un-react to remove it
- 📋 Server activity logging - message edits/deletes, joins/leaves/kicks/bans (resolved against the audit log for who + why), role/channel/server changes, and voice activity, each routed to its own configurable channel. Purges get a full transcript: every purged message's author and content, with any pasted links (gifs, images, etc.) kept as plain text rather than re-hosted embeds, plus a `.txt` attachment with the complete list so nothing is lost even on a large purge
- 🎉 Fun commands
- 🌐 Web dashboard for configuration, protected by a single shared password (no Discord OAuth or public callback required)

The dashboard is designed for people who **do not want to copy Discord IDs everywhere**. Server, channel, role, and member choices are presented by their Discord names. IDs remain internal values used by Discord and the database.

## Requirements

- Docker Engine with Docker Compose
- A Discord application/bot
- A Discord server where you have permission to add/manage the bot

## 1. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create an application, or open the application you want to use.
3. Open **Bot**.
4. Create/reset the bot token and copy it somewhere safe. **Never commit the token to Git or post it publicly.**
5. Under **Privileged Gateway Intents**, enable:
   - **Server Members Intent**
   - **Message Content Intent**
6. Save the changes.

Reedmuhn requests both intents because member events power welcome/autorole and the message content intent is needed for custom commands, automod, and message-log/purge transcripts.

## 2. Invite the bot

In the Developer Portal, open **OAuth2 → URL Generator**.

Select these scopes:

- `bot`
- `applications.commands`

Give the bot the permissions required by the features you plan to use. A typical full-feature installation needs permissions such as:

- View Channels
- Send Messages
- Embed Links
- Attach Files (for purge transcripts)
- Read Message History
- Manage Messages
- Manage Nicknames
- Manage Roles
- Kick Members
- Ban Members
- Moderate Members
- Move Members / Manage Channels for temporary voice features

Discord's role hierarchy still applies. For example, the bot cannot manage a role above its highest role and cannot change a member's nickname if Discord's hierarchy prevents it.

## 3. Configure the project

Clone the repository and enter it:

```bash
git clone https://github.com/Reedman27/Reedmuhn-bot.git
cd Reedmuhn-bot
```

Copy the environment template:

```bash
cp .env.example .env
```

Edit `.env`:

```dotenv
DISCORD_TOKEN=your_bot_token_here
WEBUI_PASSWORD=change_this_to_a_strong_password
DEV_GUILD_ID=
```

### `DISCORD_TOKEN`
Your Discord bot token from the Developer Portal. Used by both the bot and the dashboard - the dashboard needs it to look up server/channel/role/member names for the guilds the bot is in.

### `WEBUI_PASSWORD`
The single shared password used to access the self-hosted dashboard. This is deliberately local/self-hosted authentication: it works on LAN IP addresses and does not require a public domain, HTTPS, Discord OAuth application, or redirect URL. Anyone who knows this password can manage every server the bot is in through the dashboard, so keep it private and use a strong value.

### `DEV_GUILD_ID` (optional)
Put a Discord server ID here while actively developing/testing slash commands. Commands are synced to that server immediately. Leave it blank for normal global command sync.

You **do not need Developer Mode or a server ID for normal dashboard configuration**. The dashboard lists the servers it sees by name.

## 4. Start Reedmuhn

From the project directory:

```bash
docker compose up -d --build
```

Check the containers:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f discord-bot
```

The web dashboard is exposed on port `8490` by default:

```text
http://YOUR-HOST:8490
```

If you changed the port mapping in `docker-compose.yml`, use that port instead.

## 5. Open the dashboard

1. Open the dashboard in your browser.
2. Enter the `WEBUI_PASSWORD` you set in `.env`.
3. Pick a server from the list - every server the bot is currently in shows up, by name.
4. Open a feature from the sidebar.
5. Select channels, roles, or members from readable dropdowns.
6. Save the setting.

The bot and dashboard share the same SQLite database through `./data`, so dashboard changes are immediately available to the bot.

## 6. Updating

Back up the database first:

```bash
cp -a data data-backup
```

Then update the source and rebuild:

```bash
git pull
docker compose up -d --build
```

The database schema uses additive migrations for supported older databases, so normal upgrades should not require deleting `data/bot.db`.

### A note on release pace

This is a one-person, self-hosted project, and new features get tested against a real running server before they land here - not pushed the moment they compile. That means updates on this repo can lag behind whatever's actively being worked on, and a given release might take a little longer to show up than you'd see from a larger team. The tradeoff is deliberate: it's better to wait a bit longer for something that's actually been exercised end-to-end than to pull a change that looks fine on paper and breaks moderation on your server at 2am.

## 7. Backups

Your important persistent data lives under `data/`, especially:

```text
data/bot.db
data/webui_secret_key
```

Back up the entire directory before upgrades or major changes.

Do **not** back up or publish `.env` to a public repository.

**Updating the bot's code never touches this folder.** Dragging in new files and running `docker compose up -d --build` only replaces the application code baked into the image - `data/` is a separate mounted folder on the host, so counting high scores, warnings, birthdays, and every other setting survive updates automatically. There's nothing extra to do to preserve them; it's the default behavior, not something you have to opt into.

## Web dashboard architecture

```text
Discord
   │
   ▼
bot.py ───────────────┐
   │                   │
   │ writes settings   │ Discord object cache
   ▼                   ▼
        data/bot.db
             ▲
             │
      webui/main.py
             │
             ▼
       FastAPI dashboard
```

Most of the dashboard's data comes from the bot's cached server/channel/role/member names in SQLite, kept simple so the dashboard can show human-readable Discord objects without its own live gateway connection. Login is intentionally local/password-based so the dashboard works on LAN-only deployments without a public OAuth callback.

If a saved object was later deleted or a member left, the dashboard shows a clear fallback such as `Deleted channel (...)` or `Former member (...)` instead of silently displaying a meaningless number.

## Dashboard access control

The dashboard uses the single shared `WEBUI_PASSWORD` from `.env` - there is no Discord OAuth flow, per-user login, public domain requirement, or registered redirect URL. Anyone who enters the correct password gets management access to every server the bot is currently in.

The login is protected by a short in-memory per-IP lockout after repeated failed attempts, and session cookies are signed with a persistent random secret stored beside the database, use `SameSite=Lax`, and expire after 12 hours.

## Permissions and AutoMod exemptions

- **Bot Manager roles** are configured per server in the WebUI under Permissions. Members holding one of those Discord roles can use the bot's Discord moderation/configuration **commands** even when they lack the command's normal Discord permission (e.g. `manage_messages`, `ban_members`). Discord **Administrators** always have this access too.
- This is separate from *dashboard* access, which is controlled entirely by `WEBUI_PASSWORD` (see "Dashboard access control" above) - holding a Bot Manager role does not by itself grant dashboard login.
- AutoMod **does not automatically exempt Manage Messages**. AutoMod exemptions are explicitly configured by role in the WebUI; Administrators are always exempt.

## Muted role configuration

The `/mute` command uses a configurable Discord role rather than Discord's native timeout. Server administrators can choose an existing role or let the bot create/reuse a `Muted` role. The WebUI and Discord both expose the same configuration.

Discord commands:
- `/muterole set <role>` — use an existing role.
- `/muterole create` — create/reuse and configure the `Muted` role.
- `/muterole settings` — choose whether the role blocks messages, reactions, threads, voice connection, speaking, and streaming.
- `/mute <user> <duration> [reason]` — apply the configured role temporarily.
- `/unmute <user>` — remove the configured role.

The bot needs **Manage Roles**, and its highest role must be above the configured Muted role.

## Durable bot memory and logs

The bot keeps persistent state in the configured SQLite database (normally `data/bot.db`) and an append-only application log at `data/bot.log`. Do not delete or replace the `data/` directory when updating the bot code.

The database includes durable member history for moderation/AutoMod events and durable bot events for important state changes such as counting activity, completed commands, YouTube delivery, and guild lifecycle events. The file logger also captures the bot's application logs and errors.

There are no `/remember`, `/memories`, or `/forget` commands. Memory is automatic.

## Security notes

- Login attempts are rate-limited with a per-IP lockout after repeated failures.
- Session cookies are signed with a persistent random secret stored beside the database, use `SameSite=Lax`, and expire after 12 hours.
- The dashboard verifies that a selected guild is actually tracked as a guild the bot is currently in on every guild-scoped request.
- Channel, role, and member selections are validated against the bot's cached objects before being written to the database.
- Scheduled-event deletion is scoped to the selected guild.
- Dashboard responses include common browser security headers.
- The arithmetic command uses a restricted AST evaluator rather than Python `eval()`.
- User-controlled SQL values are passed through SQLite parameter binding rather than string concatenation.

### Deployment note

The dashboard is designed for self-hosted use, including local/LAN addresses such as `http://192.168.x.x:8490`. It does not require a public domain or Discord OAuth. If you expose the dashboard to the public internet, protect it with HTTPS, a strong password, and appropriate network controls.

## Development

The root bot and `webui/db.py` intentionally contain matching SQLite schema/database methods because they run in separate containers. If you change the database layer, keep both copies synchronized.

Useful checks before committing:

```bash
python -m py_compile bot.py db.py scheduler.py utils.py automod_checks.py
python -m py_compile cogs/*.py webui/main.py webui/db.py
```

## Third-party source and compatibility

Reedmuhn Bot is intentionally designed as a Python/`discord.py` bot rather than a fork of another bot framework. The project has been developed with **Red-DiscordBot** as a compatibility and implementation reference for moderation/scheduled-action patterns, and with **Sapphire Framework** as a reference for modular command/store/listener architecture. Red-DiscordBot is GPL-3.0 and Sapphire Framework is MIT; their notices and license texts are preserved under [`third_party/`](third_party/).

The uploaded Sapphire TypeScript source is not left inside the runtime project: non-Python framework concepts are translated into Python so Reedmuhn remains a single Python application. Red-DiscordBot itself is not installed as a runtime dependency.

If you redistribute or modify Reedmuhn, keep the AGPL-3.0 license and the third-party notices intact. See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) for attribution and source references.

## License

AGPL-3.0. See [`LICENSE`](LICENSE).
