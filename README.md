# ReedMuhn Bot

A self-hosted Discord moderation and utility bot built with `discord.py`, SQLite, and a companion FastAPI web dashboard.

> Built primarily with [Claude](https://claude.ai) across an extended development session (architecture, cogs, database layer, web dashboard, security fixes). ChatGPT assisted with a later round of features and documentation, including parts of this README.


## What it does

- 🛡️ Moderation: tempbans, warnings, timeouts, kicks, purges, tempnick
- 🚨 Automod: invite blocking, banned words, caps, mention spam, message spam, duplicate spam, and violation escalation
- 👋 Welcome messages, optional generated welcome cards, and autoroles
- 🎂 Birthday tracking and announcements
- 🔢 Counting with high scores and earned saves
- ⚡ Custom commands
- 🔔 Reminders and scheduled nickname/tempban actions
- 📺 YouTube upload notifications through RSS (no YouTube API key)
- 🎙️ Temporary voice channels
- 🎭 Reaction roles - react to a message to get a role, un-react to remove it
- 📋 Server activity logging - message edits/deletes, joins/leaves/kicks/bans (resolved against the audit log for who + why), role/channel/server changes, and voice activity, each routed to its own configurable channel
- 🎉 Fun commands
- 🌐 Web dashboard for configuration

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

Reedmuhn requests both intents because member events power welcome/autorole and the message content intent is needed for custom commands and automod.

## 2. Invite the bot

In the Developer Portal, open **OAuth2 → URL Generator**.

Select these scopes:

- `bot`
- `applications.commands`

Give the bot the permissions required by the features you plan to use. A typical full-feature installation needs permissions such as:

- View Channels
- Send Messages
- Embed Links
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
WEBUI_PASSWORD=use-a-long-random-dashboard-password
DEV_GUILD_ID=
```

### `DISCORD_TOKEN`
Your Discord bot token from the Developer Portal.

### `WEBUI_PASSWORD`
The password used to access the self-hosted dashboard. Use a long, unique password. This dashboard currently uses a shared password rather than Discord OAuth, so anyone who knows this password can administer every server the bot can see.

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
2. Enter `WEBUI_PASSWORD`.
3. Choose your Discord server by **name**.
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

The dashboard has no Discord API connection of its own. The bot maintains cached server/channel/role/member names in SQLite. This keeps the dashboard simple while allowing it to show human-readable Discord objects.

If a saved object was later deleted or a member left, the dashboard shows a clear fallback such as `Deleted channel (...)` or `Former member (...)` instead of silently displaying a meaningless number.

## Security notes

- The dashboard password is rate-limited after repeated failures.
- Session cookies are signed with a persistent random secret stored beside the database.
- Session cookies use `SameSite=Lax` and expire after 12 hours.
- The dashboard verifies that a selected guild is actually tracked as a guild the bot is currently in.
- Channel, role, and member selections are validated against the bot's cached objects before being written to the database.
- Scheduled-event deletion is scoped to the selected guild.
- Dashboard responses include common browser security headers.
- The arithmetic command uses a restricted AST evaluator rather than Python `eval()`.
- User-controlled SQL values are passed through SQLite parameter binding rather than string concatenation.

### Important deployment limitation

The dashboard is a **trusted-admin interface**. It currently uses one shared password instead of Discord OAuth, so it cannot tell which Discord account is using the dashboard. If you expose it to the public internet, put it behind HTTPS and an additional access-control layer (VPN, reverse proxy authentication, or a future Discord OAuth implementation). Do not treat the shared password as per-user Discord authorization.

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


### Permissions and AutoMod exemptions

- Discord **Administrators** always have bot-management access.
- **Bot Manager roles** can be selected in the WebUI under Permissions. Members with one of those roles can use the bot's configuration and management commands even when they lack the command's normal Discord permission.
- AutoMod **does not automatically exempt Manage Messages**. AutoMod exemptions are explicitly configured by role in the WebUI; Administrators are always exempt.
- The WebUI itself remains protected by `WEBUI_PASSWORD` because this self-hosted build does not use Discord OAuth to identify the logged-in browser user. Selecting a Bot Manager role controls Discord bot management, not WebUI login.


## Muted role configuration

The `/mute` command uses a configurable Discord role rather than Discord's native timeout. Server administrators can choose an existing role or let the bot create/reuse a `Muted` role. The WebUI and Discord both expose the same configuration.

Discord commands:
- `/muterole set <role>` — use an existing role.
- `/muterole create` — create/reuse and configure the `Muted` role.
- `/muterole settings` — choose whether the role blocks messages, reactions, threads, voice connection, speaking, and streaming.
- `/mute <user> <duration> [reason]` — apply the configured role temporarily.
- `/unmute <user>` — remove the configured role.

The bot needs **Manage Roles**, and its highest role must be above the configured Muted role.