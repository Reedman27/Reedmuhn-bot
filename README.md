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
- 🎵 Music playback — queues, playlists, lyrics, effects, DJ role, 24/7 mode
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
# Optional - shared secret between the bot and the bundled Lavalink
# container (see "Music" below). Defaults to "youshallnotpass" if unset,
# which is fine since Lavalink is only reachable on the internal compose
# network, not the host, but you can change it here.
LAVALINK_PASSWORD=
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

# 🎵 Music

ReedMuhn includes a dedicated `/music` command group using the Vocard-derived
Voicelink engine and a separate Lavalink audio node. Music data (playlists,
history, per-guild settings) is stored in the existing SQLite database; no
external database service is required.

### Commands

- `/music` — connect, play, search, pause/resume, skip, queue, volume, loop,
  shuffle, seek/rewind/forward, lyrics, filters, and more
- `/music playlist` — create, list, add to, play, and delete saved playlists

### Configuration

Per-server settings (default volume, DJ role, 24/7 mode) are configured from
the **Music** page in the web dashboard, under a guild's sidebar. Bot-wide
defaults are set via optional environment variables in `.env`:

| Variable | Default | Purpose |
|---|---|---|
| `LAVALINK_HOST` | `lavalink` | Hostname of the Lavalink node (the compose service name) |
| `LAVALINK_PORT` | `2333` | Lavalink's REST/WS port |
| `LAVALINK_PASSWORD` | `youshallnotpass` | Shared secret with the Lavalink container - see `.env` above |
| `MUSIC_SEARCH_PLATFORM` | `youtube` | Default source for bare (non-URL) search queries |
| `MUSIC_LYRICS_PLATFORM` | `lrclib` | Lyrics provider |
| `MUSIC_MAX_QUEUE` | `1000` | Max tracks per guild queue |
| `MUSIC_MAX_PLAYLISTS` | `10` | Max saved playlists per user |
| `MUSIC_MAX_TRACKS` | `500` | Max tracks per saved playlist |
| `MUSIC_INACTIVE_CLEANUP` | `600` | Seconds of inactivity before the bot leaves an idle voice channel |

### Credit

The project gives explicit credit to Vocard Development / ChocoMeow (MIT) and
keeps the original Vocard license. DingoLingo is credited as a historical GPLv3
reference for music-bot design; it is not a runtime dependency. See
`THIRD_PARTY_NOTICES.md`.

### Lavalink image

The Lavalink node is published as the `:lavalink` tag in the same repo as
the bot/webui images:

`docker.io/<your-dockerhub-user>/reedmuhn-bot:lavalink`

The GitHub Actions workflow `.github/workflows/lavalink-publish.yml` is intended
for a dedicated `lavalink` branch. Changes under `lavalink/` on that branch
publish only the Lavalink image. The normal `main` workflow publishes the bot
and WebUI images.

# ⚡ Redis (fast/temporary state)

SQLite (`data/bot.db`) stays the source of truth for anything permanent -
warnings, cases, tickets, economy balances, music settings, etc. A small
Redis instance handles data that's fine to lose: short cooldowns (XP gain,
`/extras work`), rate limits, and WebUI<->bot wake signaling via Redis pub/sub.

Docker Hub installs get this automatically - `docker-compose.yml` already
includes a `redis` service, only reachable from the other containers on the
compose network (never published to the host). There's nothing to configure;
`REDIS_URL` is set for you.

Redis is optional at the code level: if it is unavailable, cooldowns and
rate-limit state fall back to memory and WebUI actions remain safe because
their SQLite queues are the source of truth. When Redis is available, the
WebUI publishes a small `webui:wake` notification after queueing an action, and
the bot immediately wakes the matching worker instead of waiting for its next
poll interval. The normal SQLite polling loop remains in place as the reliable
fallback.

# 🤝 Contributing

Bug reports, suggestions, and pull requests are welcome.

If you find a bug or have an idea, open an issue on GitHub.

# 📜 License

ReedMuhn Bot is licensed under **AGPL-3.0**.

See [`LICENSE`](LICENSE) for the full license.

---

**ReedMuhn Bot — One bot. One dashboard. Your server. ❤️**
