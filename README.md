# 🤖 ReedMuhn Bot

**ReedMuhn** is a self-hosted Discord moderation and utility bot built with Python, `discord.py`, SQLite, FastAPI, and Docker.

It combines moderation, automation, server management, and a full web dashboard into one bot.

## ⚠️ Status

This is a self-hosted personal/hobby project that gets iterated on a lot, including with AI assistance. Most of it is tested and works, but treat it as **hit-and-miss**, not a polished commercial product:

- Some features may be half-finished, untested in production, or break on edge cases.
- Configuration through the web dashboard is generally more reliable than raw database edits, but neither is guaranteed bug-free.
- Back up your `data/` folder (the SQLite database) before updating.
- Issues and PRs are welcome, but there's no support SLA - use at your own risk.

## ✨ Features

- 🛡️ Moderation — warnings, kicks, bans, tempbans, timeouts, mutes, purges
- 📋 Numbered moderation cases and member history
- 🚨 AutoMod and raid detection
- 🔒 Emergency lockdown and security tools
- 🎫 Tickets and modmail
- 🚩 Reports
- 🔗 Invite tracking
- 🎭 Reaction roles and verification
- 👋 Welcome messages and autoroles
- 🎂 Birthdays
- 🔢 Counting
- 🔔 Reminders
- 📺 YouTube notifications
- 📡 Twitch and RSS/Atom notifications, live member/online/bot/channel counters
- 🧮 XP levels, economy (coins), and giveaways
- 🎙️ Temporary voice channels
- ⭐ Starboard
- 💡 Suggestions
- 🎉 Fun commands
- 📊 Server activity logging
- 🖥️ Full web dashboard
- 🤖 Optional AI integrations
- 💾 Persistent SQLite storage

---

# 🐳 Installation

ReedMuhn can be deployed in two ways.

### Recommended

**Docker Hub** — pull the pre-built image and run it with Docker Compose.

### For developers

**GitHub** — clone the source code and build the image yourself.

---

# 🐳 Option 1 — Docker Hub

This is the easiest way to run ReedMuhn.

### Requirements

- Docker
- Docker Compose
- A Discord bot application

### 1. Download the Compose configuration

Download the `docker-compose.yml` from this repository.

Or clone only the repository files if preferred.

### 2. Configure `.env`

Create a `.env` file next to `docker-compose.yml`:

```dotenv
DISCORD_TOKEN=your_discord_bot_token
WEBUI_PASSWORD=your_dashboard_password
DEV_GUILD_ID=
```

Never share your Discord bot token.

### 3. Pull the image

```bash
docker pull reedman27/reedmuhn-bot:latest
```

### 4. Start ReedMuhn

```bash
docker compose up -d
```

Check the containers:

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

---

# 🛠️ Option 2 — Build from GitHub

Use this method if you want to modify the source code or build the image yourself.

### 1. Clone the repository

```bash
git clone https://github.com/Reedman27/Reedmuhn-bot.git
cd Reedmuhn-bot
```

### 2. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env` and add your Discord bot token and dashboard password.

### 3. Build and start

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

---

# 🔄 Updating

## Docker Hub installation

Pull the newest image:

```bash
docker compose pull
docker compose up -d
```

Or explicitly:

```bash
docker pull reedman27/reedmuhn-bot:latest
docker compose up -d
```

## GitHub installation

Pull the newest source code and rebuild:

```bash
git pull
docker compose up -d --build
```

### 💾 Your data

ReedMuhn stores persistent data in the `data/` directory.

**Do not delete the `data/` directory when updating.**

Your database, settings, logs, and other persistent data will survive container updates.

---

# 🌐 Dashboard

Once ReedMuhn is running, open:

```text
http://YOUR-SERVER-IP:8490
```

The dashboard lets you configure most ReedMuhn features without manually entering Discord IDs.

---

# 🤝 Contributing

Bug reports, suggestions, and pull requests are welcome.

If you find a bug or have an idea, open an issue on GitHub.

# 📜 License

ReedMuhn Bot is licensed under **AGPL-3.0**.

See [`LICENSE`](LICENSE) for the full license.

---

**ReedMuhn Bot — One bot. One dashboard. Your server. ❤️**