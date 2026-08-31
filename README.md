# ReedMuhn Bot

A self-hosted Discord moderation and utility bot built with `discord.py`, SQLite, and a companion FastAPI web dashboard.

> Built primarily with [Claude](https://claude.ai) across an extended development session (architecture, cogs, database layer, web dashboard, security fixes). ChatGPT assisted with a later round of features and documentation, including parts of this README.


## What it does

- 🛡️ Moderation: tempbans, warnings (with citable numbered rules and free-text evidence/notes), timeouts, kicks, purges, tempnick, and a configurable mute role
- 🗂️ Case system: every warn/kick/mute/tempban gets a sequential per-server case number automatically. `/case view`, `/case search`, `/case edit`, `/case delete` (soft-delete/void, kept for audit), `/history <user>`, and private staff `/notes` not tied to any one incident - browsable from the dashboard's Moderation → Cases tab too
- 🔎 Permission security scanner: dashboard page (Permissions → Security Scanner) that flags roles and @everyone with Administrator or other high-risk permissions (ban/kick/manage roles/manage webhooks/mention everyone) - read-only, never changes anything
- 🚨 Raid detection: watches join velocity and reacts once too many joins happen too fast in a short window - alert only, auto-kick new accounts under a configurable age, or a full channel lockdown. Configured entirely from the dashboard (Moderation → Raid Detection); `/raidmode status` for a quick in-Discord check
- 🔗 Invite tracking: attributes each join to whichever invite link was used, an in-Discord leaderboard (`/invites check`, `/invites leaderboard`), a dashboard page with recent joins and a "left within 24h" signal, and configurable milestone roles (e.g. 10 invites → @Recruiter) granted automatically
- 📬 Modmail: members DM the bot directly instead of using the Tickets panel; the bot opens a private staff channel, relays the DM in, and relays staff replies back as DMs. Staff-only `/modmail close`, `/modmail block`/`unblock`, optional anonymous-staff mode, all configured from the dashboard
- 💬 `/say <message> [channel]` - makes the bot post a message in a channel. Restricted to the server owner or Administrators only (a tighter bar than the configurable Bot Manager role tier other commands use), mentions are always suppressed, and every use is logged
- 🏓 `/ping` - Roundtrip/Gateway/Database latency at a glance
- 🚨 Automod: invite blocking, GIF blocking (uploads and Tenor/Giphy links), per-GIF allowlist/blocklist exceptions, banned words, caps, mention spam, message spam, duplicate spam, violation escalation, and an optional review queue that holds fuzzy-match catches for a moderator to confirm or dismiss instead of auto-punishing
- 📋 Server rules: numbered rules a warning can cite, managed from Discord or the dashboard
- 🚩 Reports: members can flag something for staff with `/report`; the dashboard's triage queue can resolve a report straight into a real warning, linking it to that member's actual history
- 👮 Staff activity: per-moderator counts of warnings, tickets closed, and reports resolved, over 7/30/90-day/all-time windows
- 🔎 Dashboard search: one query box across warnings, reports, rules, tickets, polls, and the automod review queue
- 🆘 Emergency Control Center: dashboard-only, confirmation-gated actions for an active incident - server-wide lockdown/unlock (remembers and restores each channel's exact prior state), revoke every active invite, and mass-timeout everyone holding a chosen role
- 💾 Config snapshots: save and restore this bot's own settings (automod, verification, tickets, reports, tempnick permissions, bot manager roles, voice hubs, logging routes, welcome/autorole) as a named point-in-time backup - does not touch real Discord role/channel permissions. A Compare tool diffs any two snapshots, or a snapshot against current live settings
- ✅ Verification: a persistent button in a chosen channel grants a role on click
- 🎫 Tickets: `/ticket` opens a private support channel, or members can click a persistent "Open a Ticket" panel button in one designated channel (no slash command needed - a drop-in replacement for a dedicated ticket-tool bot), closeable via command, button, or dashboard
- 📊 Polls: up to 5 options, live results, optional auto-close
- 🧷 Sticky roles: persist eligible member roles across leaves/rejoins, with Discord and WebUI controls plus role exclusions for privileged roles
- 🎭 Reaction roles - react to a message to get a role, un-react to remove it
- 👋 Welcome messages, optional generated welcome cards, and autoroles
- 🎂 Birthday tracking and announcements
- 🔢 Counting with high scores and earned saves
- ⚡ Custom commands
- 🔔 Reminders and scheduled nickname/tempban actions
- 📺 YouTube upload notifications through RSS (no YouTube API key)
- 🎙️ Temporary voice channels
- 📋 Server activity logging - messages, members (including verification), moderation, automod, tickets, reports, server, and voice each route to their own configurable channel, with an ignore list. Manual Discord moderation actions are resolved against the audit log for who + why. Purges get a full transcript: every purged message's author and content, with any pasted links (gifs, images, etc.) kept as plain text rather than re-hosted embeds, plus a `.txt` attachment with the complete list so nothing is lost even on a large purge
- 🖥️ WebUI parity: every feature above has a matching dashboard page - analytics streams, temp-voice shutdowns, banned-word configuration, sticky-role exclusions, targeted purges, ticket/verification panel posting, and more
- 🎉 Fun commands - every fun command can be individually enabled/disabled from the dashboard
- ⭐ Starboard - configurable ⭐ threshold and channel, with live updates when reactions are added or removed
- 💡 Suggestions - member suggestions with a dedicated channel and persistent Approve/Deny staff controls
- 🌐 Web dashboard for configuration, protected by a single shared password (no Discord OAuth or public callback required) - 30+ built-in themes plus a custom theme editor, and its own login/action audit log

The dashboard sidebar is organized into collapsible Carl-bot-style categories and scrolls independently on desktop, while keeping the footer controls visible.

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
   - **Presence Intent**
6. Save the changes.

Reedmuhn requests these privileged intents because member events power welcome/autorole, message content is needed for custom commands and automod, and presence data powers the live online/idle/dnd analytics count.

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
- Manage Server (Emergency's revoke-invites, and required for Invite Tracking to list/see invite use counts at all)
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
Your Discord bot token from the Developer Portal, used by the `discord-bot` container to log in. The dashboard container does not need it - it never calls the Discord API directly, and instead reads the server/channel/role/member names the bot has already cached into `bot.db` (see "Web dashboard architecture" below).

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

### Disabling or removing a cog

Cogs are loaded from the hardcoded `INITIAL_COGS` list near the top of `bot.py`, not auto-discovered from the `cogs/` folder. If you want to turn a feature off, remove its entry from `INITIAL_COGS` in `bot.py` - deleting the `cogs/<name>.py` file by itself is not enough and will crash-loop the bot on startup.

As of this version, a cog that's missing or fails to load is logged and skipped instead of taking the whole bot down, but the fix above (removing it from `INITIAL_COGS`) is still the correct way to disable a feature on purpose.

### Command groups and Discord's 100-command limit

Discord caps a bot at 100 top-level application (slash) commands. This bot organizes related commands under a shared group instead of registering each one as its own top-level command:

- `/fun` - all 16 social/game/text commands (`/fun hug`, `/fun roll`, etc.)
- `/moderation` - warnings, purge, kick, tempban, tempnick, and mute/unmute, including `/moderation muterole` as a nested subgroup (`/moderation muterole set`, etc.)
- `/automod` - all automod configuration and the review queue, including `/automod word`, `/automod gifallow`, `/automod gifblock`, and `/automod escalation` as nested subgroups
- `/antinuke`, `/modmail`, `/case`, `/notes`, `/logging`, `/stickyroles`, `/birthday`, `/invites`, `/raidmode` are also groups rather than flat commands

Only the group itself counts against the 100-command cap - its subcommands don't. Currently the bot registers **66** top-level commands. If you add new commands, prefer adding them as a subcommand of an existing group (or a new group) over a new flat top-level command, especially in `fun.py`, `moderation.py`, or `automod.py`.

On every startup, `bot.py` logs how many top-level commands are registered and logs a warning once that count reaches 90. If a sync is ever rejected by Discord (most commonly for going over the limit), the bot logs the error and keeps running instead of crash-looping - fix the command list and restart to retry the sync.

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
- A small number of commands (currently just `/say`) use a **tighter** check than Bot Manager roles: server owner or Discord Administrator only. This is deliberate for commands whose blast radius is bigger than a typical config command - holding a Bot Manager role is not enough to run these.

## Muted role configuration

The `/moderation mute` command uses a configurable Discord role rather than Discord's native timeout. Server administrators can choose an existing role or let the bot create/reuse a `Muted` role. The WebUI and Discord both expose the same configuration.

Discord commands:
- `/moderation muterole set <role>` — use an existing role.
- `/moderation muterole create` — create/reuse and configure the `Muted` role.
- `/moderation muterole settings` — choose whether the role blocks messages, reactions, threads, voice connection, speaking, and streaming.
- `/moderation mute <user> <duration> [reason]` — apply the configured role temporarily.
- `/moderation unmute <user>` — remove the configured role.

The bot needs **Manage Roles**, and its highest role must be above the configured Muted role.

## Ticket panel ("ticket tool" replacement)

`/setticketpanel <channel> [title] [description]` posts a persistent embed with an **Open a Ticket** button in one channel - members click it, fill in an optional one-line subject, and get their own private ticket channel immediately, with no slash command to remember. Anyone can see the panel message; only the person who clicks it ever gets access to the channel it creates, and a member can only have one open ticket at a time.

`/setuptickets <category> <support_role>` still needs to be run once to say where ticket channels go and who on staff can see them - the panel and `/ticket` both use that same configuration. The panel channel and its wording can also be set from the dashboard's Tickets page, which posts/updates the live message within a couple seconds since the dashboard itself has no direct Discord connection.

## Modmail

Members DM the bot directly instead of navigating to the Tickets panel inside the server. The bot opens a private staff channel in a configured category, relays the DM into it, and relays staff replies back to the member as DMs - no slash command needed on the member's side, just a normal DM.

New modmail channels intentionally get **no explicit permission overwrites** - they inherit whatever's set on the configured category, the same way tickets rely on their category/support role. Keep that category staff-only in Discord's own permissions.

If a member shares more than one server with modmail enabled, the bot asks which one their message is about before opening a thread. Staff close a thread with `/modmail close [reason]` (run inside the thread channel), and can `/modmail block`/`/modmail unblock` a member from opening new threads at all. An optional anonymous-staff mode shows the member "Staff" instead of who specifically replied. All of this - enabling modmail, the category, an optional log channel, anonymous mode, and the blocked-members list - is configured from the dashboard's Modmail page.

## Case system

Every `/moderation warn`, `/moderation kick`, `/moderation mute`, and `/moderation tempban` automatically opens a sequential, per-server numbered case (Case #1, #2, ...) layered on top of the bot's existing member-history log rather than a separate duplicate record. `/case view <number>`, `/case search <user>`, `/case edit <number> <reason>`, and `/case delete <number>` (a soft void - kept for audit, excluded from active counts, numbers are never reused) manage them; `/history <user>` is a quick shortcut for search. Private staff `/notes add`/`/notes view` track observations about a member that aren't tied to any one incident. The dashboard's Moderation → Cases tab mirrors all of this with inline editing.

## Raid detection

Watches join velocity - too many joins too fast within a short window - the same burst-detector shape as Anti-Nuke, but keyed on member joins instead of audit-log actions. Configured entirely from the dashboard's Moderation → Raid Detection page: the threshold, the response (alert only, auto-kick new accounts under a configurable age, or lock every text channel via the same mechanism as Emergency's Lockdown), and where alerts get posted. `/raidmode status` gives staff a quick in-Discord check of whether it's currently active. A lockdown triggered this way still needs a manual `/unlock` (or the dashboard's Emergency page) once things settle - it's deliberately not automatic.

## Invite tracking

Attributes each join to whichever invite link was actually used - the standard technique for this, since Discord's API doesn't report it directly: the bot snapshots every invite's use count, and whichever one incremented right after a join gets the credit. Needs **Manage Server** on the bot to list invites at all; without it, joins are recorded as unattributed rather than guessed at.

`/invites check [user]` and `/invites leaderboard` for a quick Discord-side look. The dashboard's Invites page (under Statistics) shows the full leaderboard, a recent-joins log, and how many invited members left within 24 hours of joining - a rough signal for invite-reward farming or a raid dressed up as normal joins. Milestone roles (e.g. 10 invites → @Recruiter) are configured there too, and get granted automatically the moment a tracked join pushes someone over a threshold.

## Permission security scanner

A read-only dashboard page (Permissions → Security Scanner) that checks every role - and the `@everyone` role, which Discord treats separately from normal roles - for Administrator or other permissions that are easy to hand out by accident and easy to miss once granted (ban/kick members, manage roles/channels/webhooks, mention @everyone). It never changes anything; it just tells you what to go double-check in Discord's own role settings.

## `/say`

`/say <message> [channel]` makes the bot post a message in a channel, defaulting to the current one. Restricted to the **server owner or Discord Administrators only** - deliberately tighter than the configurable Bot Manager role tier other commands use, since this lets whoever can run it make the bot say anything, anywhere it can post. Mentions are always suppressed (it can't be used to ping `@everyone`, a role, or a user), and every use is logged.

## Emergency Control Center and Config Snapshots

Two dashboard-only tools for larger-scale situations, both reached from the sidebar:

- **Emergency Control Center** - server-wide lockdown (denies Send Messages for @everyone across every text channel the bot can manage, remembering each channel's exact prior permission so Unlock restores it precisely rather than opening everything), revoke every active invite link, and mass-timeout every current member of a chosen role. The server owner, Administrators, bots, and anyone above the bot's own role are automatically skipped by mass timeout. Every action requires typing a confirmation phrase before it's queued.
- **Config Snapshots** - saves a named, restorable copy of this bot's own settings: automod (including escalation tiers), welcome/autorole, verification, tickets (including the panel), reports, tempnick permissions, bot manager roles, voice hubs, and logging routes. Deliberately does **not** touch real Discord role/channel permissions, so restoring one can't clobber a manual permission change made outside the bot. Restoring a snapshot that changes the Muted role still needs a manual `/moderation muterole` sync afterward to apply it to every channel, same as any other Muted-role change. A **Compare** tool sits alongside it, diffing any two snapshots - or a snapshot against the server's current live settings - so you can see exactly what a restore would change before running it.

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

The dashboard is designed for self-hosted use, including local/LAN addresses such as `http://192.168.x.x:8490`. It does not require a public domain or Discord OAuth. If you expose the dashboard to the public internet, protect it with HTTPS, a strong password, and appropriate network controls. If you put it behind a reverse proxy that terminates HTTPS, also set `WEBUI_HTTPS_ONLY=1` in `.env` so the session cookie is marked Secure-only.

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


### Moderation additions

- `/moderation purge amount:<1-1000> user:<member>` deletes recent messages, optionally restricted to a specific member. Discord's 14-day bulk-delete limit is handled automatically by falling back to individual deletes for older messages.
- `/automod word add word:<word-or-phrase>` adds a server-wide banned word/phrase.
- `/automod word remove word:<word-or-phrase>` removes one.
- `/automod word list` shows the configured list.
- `/automod invites block:<true|false>` toggles Discord invite-link blocking.
- `/automod gifs block:<true|false>` toggles GIF blocking - catches uploaded `.gif` attachments and links to GIF-hosting sites (Tenor, Giphy), which is also what Discord's own built-in GIF picker posts. Static images are unaffected.
- `/automod gifallow add|remove|list` manages GIFs exempt from blanket blocking; `/automod gifblock add|remove|list` manages GIFs that are always blocked. A blocklist entry overrides the allowlist.
- `/votekick <user> <reason>` starts a community vote when Vote Kick is enabled; `/votekicktoggle <enabled>` toggles it. The WebUI also controls required yes votes and vote duration.
- The existing WebUI **AutoMod → Banned words** editor manages the same list, so changes made in Discord and the WebUI stay synchronized.
- `/automod queue`, `/automod queueconfirm <id>`, `/automod queuedismiss <id>` - when the AutoMod review queue is turned on (WebUI AutoMod page), a fuzzy word-filter match is deleted immediately but held for a moderator to confirm (apply the escalation ladder) or dismiss, instead of auto-punishing on a match type that's inherently more false-positive-prone. Also manageable from the dashboard's Moderation Queue page.
- `/rules`, `/addrule <text>`, `/removerule <number>` manage numbered server rules; `/moderation warn` takes an optional rule number to cite.
- `/report <user> <reason>`, `/reports` (staff shortcut to the open queue), `/setreportschannel <channel>` - members can flag something for staff, who triage from Discord or the dashboard's Reports page. Resolving a report can issue a real warning through the same path `/moderation warn` uses, so it's linked into that member's actual warning history and staff stats rather than being a separate record.

### AI connections

The WebUI has an **AI** page for OpenAI, Groq, Ollama/local servers, or any provider exposing an OpenAI-compatible `/chat/completions` endpoint. The API key is never rendered back into the dashboard.

- **Index server channels** is an opt-in toggle. When enabled, ReedMuhn stores a rolling searchable copy of channel messages in the same SQLite database as the rest of the bot, and `/ask` only sends a small set of matching indexed messages to the configured AI provider.
- `/aiindex [channel] [limit]` manually backfills recent messages from a text channel after indexing is enabled. `/aiclearindex` deletes the server's indexed AI messages.
- Channel indexing is off by default. Live current-channel context is a separate toggle from indexing.
- The SQLite database is created automatically on first startup if it does not already exist; both the Discord bot and WebUI use the same `DB_PATH` volume in Docker.
