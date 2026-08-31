# ReedMuhn Bot

A self-hosted Discord moderation and utility bot built with **Python, discord.py, SQLite, FastAPI, and Docker**.

Built for servers that want moderation, automation, tickets, logging, and a web dashboard without relying on a bunch of separate bots.

## ✨ Features

- 🛡️ Moderation — warnings, kicks, bans, tempbans, timeouts, mutes, purges
- 📋 Case system with numbered moderation cases and member history
- 🚨 AutoMod — invites, GIFs, banned words, spam, caps, mentions, escalation
- 🔥 Raid detection and emergency lockdown tools
- 🎫 Tickets with persistent buttons
- 📬 Modmail
- 🚩 Member reports
- 🔗 Invite tracking and milestone roles
- 🎭 Reaction roles
- 👋 Welcome messages and autoroles
- 🎂 Birthday tracking
- 🔢 Counting
- 🔔 Reminders and scheduled actions
- 📺 YouTube upload notifications
- 🎙️ Temporary voice channels
- ⭐ Starboard
- 💡 Suggestions
- 🎉 Fun commands
- 📊 Server activity logging
- 🖥️ Full web dashboard
- 🤖 Optional AI integrations
- 💾 SQLite-based persistent data
- 🐳 Docker deployment

## Requirements

- Docker + Docker Compose
- A Discord bot application
- A Discord server where you can add/manage bots

## 🚀 Installation

### 1. Create your Discord bot

Go to the [Discord Developer Portal](https://discord.com/developers/applications) and:

1. Create/open your application.
2. Go to **Bot**.
3. Create/reset the bot token.
4. Enable:
   - Server Members Intent
   - Message Content Intent
   - Presence Intent
5. Copy your bot token somewhere safe.

**Never put your Discord token directly into GitHub.**

### 2. Invite the bot

Under **OAuth2 → URL Generator**, select:

- `bot`
- `applications.commands`

Give the bot the permissions required for the features you want to use. For a full installation, the bot generally needs permissions such as:

- View Channels
- Send Messages
- Embed Links
- Attach Files
- Read Message History
- Manage Messages
- Manage Roles
- Manage Channels
- Manage Nicknames
- Kick Members
- Ban Members
- Moderate Members
- Manage Server

Make sure the bot's highest role is above roles it needs to manage.

### 3. Clone the repository

```bash
git clone https://github.com/Reedman27/Reedmuhn-bot.git
cd Reedmuhn-bot
```

Create your environment file:

```bash
cp .env.example .env
```

Edit `.env`:

```dotenv
DISCORD_TOKEN=your_discord_bot_token
WEBUI_PASSWORD=your_strong_dashboard_password
DEV_GUILD_ID=
```

`DEV_GUILD_ID` is optional and is useful when testing slash-command changes.

### 4. Start the bot

```bash
docker compose up -d --build
```

Check that everything is running:

```bash
docker compose ps
```

View bot logs:

```bash
docker compose logs -f discord-bot
```

The dashboard is normally available at:

```text
http://YOUR-SERVER-IP:8490
```

## 🐳 Docker Hub / GitHub Actions

The repository can automatically build and publish the Docker image to Docker Hub whenever changes are pushed to `main`.

GitHub repository secrets:

```text
DOCKERHUB_USERNAME
DOCKERHUB_TOKEN
```

`DOCKERHUB_USERNAME` should contain your Docker Hub username.

`DOCKERHUB_TOKEN` should contain your Docker Hub access token.

The workflow publishes:

```text
YOUR_USERNAME/reedmuhn-bot:latest
YOUR_USERNAME/reedmuhn-bot:<commit-sha>
```

### Using the published image

On a server where you want to pull the image instead of building it locally:

```bash
docker pull YOUR_USERNAME/reedmuhn-bot:latest
```

Then use the image in your `docker-compose.yml`.

## 🔄 Updating

If you're building from the Git repository:

```bash
git pull
docker compose up -d --build
```

If you're using the published Docker image:

```bash
docker compose pull
docker compose up -d
```

Your persistent database and configuration are stored in `data/`, so **do not delete the `data/` directory when updating**.

For extra safety, back it up first:

```bash
cp -a data data-backup
```

## 🌐 Dashboard

The web dashboard provides configuration for most ReedMuhn features, including:

- Moderation
- AutoMod
- Tickets
- Modmail
- Reports
- Logging
- Verification
- Reaction roles
- Welcome/autoroles
- Temporary voice
- Invites
- Raid detection
- Permissions
- Config snapshots
- AI settings
- Themes

Dashboard authentication uses the `WEBUI_PASSWORD` from `.env`.

The dashboard is designed primarily for self-hosted/LAN use. If exposing it to the internet, use HTTPS and appropriate network security.

## 💾 Data

Persistent data is stored in:

```text
data/
├── bot.db
├── bot.log
└── webui_secret_key
```

Back up this directory before major updates.

## 🛠️ Development

Useful syntax checks:

```bash
python -m py_compile bot.py db.py scheduler.py utils.py automod_checks.py
python -m py_compile cogs/*.py webui/main.py webui/db.py
```

## 📜 License

ReedMuhn Bot is licensed under **AGPL-3.0**.

See [`LICENSE`](LICENSE) and [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

---

Made for self-hosted Discord communities. ❤️