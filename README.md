# 🤖 ReedMuhn Bot

**ReedMuhn** is a self-hosted Discord moderation and utility bot built with Python, `discord.py`, SQLite, FastAPI, and Docker.

It combines moderation, automation, server management, and a full web dashboard into one bot.

## ✨ Features

### 🛡️ Moderation
- Warnings, kicks, bans, tempbans, timeouts, and mutes
- Numbered moderation cases
- Member history and staff notes
- Message purging with transcripts
- Configurable server rules

### 🚨 Security & AutoMod
- Raid detection
- Invite blocking
- GIF blocking
- Banned words
- Spam and duplicate-message detection
- Caps and mention-spam protection
- Violation escalation
- Emergency server lockdown
- Permission security scanner

### 🎫 Server Tools
- Tickets with persistent buttons
- Modmail
- Reports
- Reaction roles
- Verification
- Sticky roles
- Welcome messages and autoroles
- Invite tracking
- Temporary voice channels

### 📊 Dashboard
Manage your server from a web interface:

- Moderation
- AutoMod
- Tickets
- Modmail
- Reports
- Logging
- Verification
- Invites
- Raid detection
- Permissions
- Server settings
- Config snapshots
- Themes

### 🎉 Extras
- Fun commands
- Counting
- Birthdays
- Reminders
- Suggestions
- Starboard
- YouTube upload notifications
- Optional AI integrations

## 🐳 Docker Installation

### Requirements

- Docker
- Docker Compose
- A Discord bot application

### 1. Download ReedMuhn

```bash
git clone https://github.com/Reedman27/Reedmuhn-bot.git
cd Reedmuhn-bot
```

### 2. Configure the bot

Create your environment file:

```bash
cp .env.example .env
```

Open `.env` and enter your Discord bot token and dashboard password.

### 3. Start ReedMuhn

```bash
docker compose up -d --build
```

Check the bot:

```bash
docker compose ps
```

View logs:

```bash
docker compose logs -f discord-bot
```

The dashboard is normally available at:

```text
http://YOUR-SERVER-IP:8490
```

## 🔄 Updating

From the ReedMuhn directory:

```bash
git pull
docker compose up -d --build
```

Your server data is stored in the `data/` directory and persists between updates.

**Do not delete the `data/` directory when updating.**

## 🤝 Contributing

Issues, suggestions, and pull requests are welcome.

If you find a bug or have an idea for a feature, open an issue on GitHub.

## 📜 License

ReedMuhn Bot is licensed under the **AGPL-3.0** license.

See [`LICENSE`](LICENSE) for the full license.

---

**ReedMuhn Bot** — One bot. One dashboard. Your server. ❤️