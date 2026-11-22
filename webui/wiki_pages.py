"""Content for the dashboard Wiki pages.

Plain data, rendered by templates/wiki_topic.html with Jinja autoescaping, so
nothing in here is ever treated as HTML. The slash-command lists are not
written here: each topic names command prefixes and the lines come from the
generated help_commands.py, so they follow the code.

Facts stated here (defaults, limits, behaviour) were taken from the cogs and
the database layer; when one of those changes, update the topic too.
"""
from __future__ import annotations

from help_commands import COMMANDS

GROUPS = (
    "Basics",
    "Moderation & Safety",
    "Community",
    "Fun & Economy",
    "Server Tools",
    "Integrations",
)


def sec(title: str, text=(), bullets=()):
    return {"title": title, "text": list(text), "bullets": list(bullets)}


def topic(slug, title, group, summary, *, page=None, page_label=None, commands=(), sections=(), see_also=()):
    return {
        "slug": slug,
        "title": title,
        "group": group,
        "summary": summary,
        "page": page,  # dashboard sidebar slug ("" is the dashboard home)
        "page_label": page_label or title,
        "commands": tuple(commands),  # command-name prefixes, matched against help_commands.COMMANDS
        "sections": list(sections),
        "see_also": tuple(see_also),
    }


TOPICS = [
    # ------------------------------------------------------------------ Basics
    topic(
        "start", "Getting started", "Basics",
        "What ReedMuhn is, where to begin, and how the dashboard and the bot fit together.",
        page="quickstart", page_label="Quickstart",
        sections=[
            sec(
                "Description",
                [
                    "ReedMuhn is a Discord bot plus this web dashboard, both working on the same SQLite database. "
                    "Most features can be configured from either place; the dashboard is the easier one because it "
                    "lets you pick channels and roles by name instead of pasting IDs.",
                ],
            ),
            sec(
                "First steps",
                bullets=[
                    "Logging: let the dashboard auto-create the log channels.",
                    "AutoMod, Raid Detection and Anti-Nuke: pick thresholds. All three are off until you turn them on.",
                    "Welcome & Roles and Verification: greet and gate new members.",
                    "Temporary Voice is optional; everything else (tickets, reaction roles, leveling, music) is too.",
                ],
            ),
            sec(
                "Dashboard or slash command?",
                [
                    "Some actions only exist in the dashboard. Ban, unban, timeout and untimeout have no slash command; "
                    "queue them from the Moderation page's member actions instead.",
                    "Slash commands are registered globally unless DEV_GUILD_ID is set, and a global sync can take up "
                    "to an hour to show up in Discord.",
                ],
            ),
        ],
        see_also=("dashboard-access", "moderation", "logging"),
    ),
    topic(
        "dashboard-access", "Dashboard Access", "Basics",
        "Who can sign in to this dashboard for your server, and what they can do.",
        page="access",
        sections=[
            sec(
                "Description",
                [
                    "You sign in with Discord. A server appears only if the bot is installed there and you own it or "
                    "have Manage Server or Administrator. Every server page re-checks this, so changing the server ID "
                    "in the URL does not get anyone anywhere.",
                ],
            ),
            sec(
                "Access levels",
                bullets=[
                    "Default: the owner, Administrators and anyone with Manage Server get full access.",
                    "Only the owner and the people listed: choose this and add nobody to be the only person who sees "
                    "everything.",
                    "Full: everything except the Dashboard Access page.",
                    "Staff: a trimmed menu (Dashboard, Search, Moderation, Moderation Queue, Reports, Tickets, "
                    "Modmail, Suggestions, Staff Activity, Quickstart and Wiki). Staff can act on cases, warnings, "
                    "reports and tickets, but cannot change settings.",
                ],
            ),
            sec(
                "Notes",
                [
                    "A name on the access list wins over Discord permissions for everyone except the server owner, "
                    "so an Administrator can deliberately be kept to the Staff view.",
                    "A user demoted in Discord loses dashboard access within about a minute.",
                ],
            ),
        ],
        see_also=("permissions", "start"),
    ),
    topic(
        "permissions", "Permissions", "Basics",
        "Bot Manager roles and who may use temporary nicknames.",
        page="permissions",
        sections=[
            sec(
                "Bot Manager roles",
                [
                    "Most configuration commands accept three kinds of person: an Administrator, someone with the "
                    "command's normal Discord permission (usually Manage Server), or a member holding a Bot Manager "
                    "role. Choose those roles on the Permissions page.",
                    "Bot Manager roles do not open the dashboard; use Dashboard Access for that. A few commands "
                    "(the /extras admin commands, for example) check the Discord permission only and ignore Bot "
                    "Manager roles.",
                ],
            ),
            sec(
                "Temporary nicknames",
                [
                    "The tempnick rule is one of: everyone, only members with a listed role (allowlist), or everyone "
                    "except members with a listed role (denylist).",
                ],
            ),
        ],
        see_also=("dashboard-access", "moderation"),
    ),
    # ------------------------------------------------------- Moderation & Safety
    topic(
        "moderation", "Moderation", "Moderation & Safety",
        "Warnings, timeouts, kicks, bans, tempbans, mutes, purges, numbered cases and staff notes.",
        page="moderation",
        commands=("moderation", "case", "notes", "history"),
        sections=[
            sec(
                "Slash commands and the dashboard",
                [
                    "The slash commands cover warn, warnings, clearwarns, mute, unmute, kick, tempban, purge and "
                    "tempnick. The Moderation page can also queue kick, ban, tempban, unban, mute, unmute, timeout and "
                    "untimeout against a member.",
                    "The dashboard is a separate process, so these are queued in the database and carried out by the "
                    "bot, which keeps Discord permissions and role-hierarchy checks on the bot's side. Temporary "
                    "failures (rate limits, Discord server errors) are retried with a growing delay, up to six "
                    "attempts. Timeouts are capped at Discord's own limit of 28 days.",
                ],
            ),
            sec(
                "Cases and notes",
                [
                    "Every action is a numbered case with a member history. Deleting a case voids it: it stays for "
                    "audit purposes and is left out of active counts. Staff notes are private to staff.",
                ],
            ),
            sec(
                "Muted role",
                [
                    "/moderation muterole set, create, settings and preset configure the role, including what it "
                    "blocks. Roles stripped for a mute are restored when the mute ends.",
                ],
            ),
        ],
        see_also=("warnings", "automod", "rules", "logging"),
    ),
    topic(
        "warnings", "Warnings and the warning window", "Moderation & Safety",
        "How warnings add up toward automatic punishments, and why they never expire unless you ask.",
        page="automod", page_label="AutoMod (Warning window, Escalation tiers)",
        commands=("automod escalation",),
        sections=[
            sec(
                "Description",
                [
                    "Saved warnings are permanent. Nothing deletes them automatically; only /moderation clearwarns or "
                    "an explicit Remove or Clear in the dashboard does.",
                    "Escalation tiers (for example 3 warnings: mute, 5: kick) count a member's recent manual warnings "
                    "plus AutoMod violations, starting from their last escalation reset.",
                ],
            ),
            sec(
                "Warning window",
                [
                    "The warning window limits how far back that count looks, in seconds. 0, the default, means "
                    "warnings never age out of the count. Set a number (up to 604800, seven days) if you want old "
                    "warnings to stop counting toward escalation.",
                    "Servers that already had a window saved keep their value until you change it on the AutoMod page.",
                ],
            ),
            sec(
                "Resetting the count",
                [
                    "A punishment that resolves the situation (anything except a plain warn) starts a new "
                    "escalation cycle without deleting history. Warn is not offered as a tier punishment because the "
                    "warning that reaches the threshold is already recorded.",
                    "For manual warnings a tier fires only when the count equals the threshold exactly. For AutoMod "
                    "violations, passing the last tier applies the last tier again.",
                ],
            ),
        ],
        see_also=("automod", "moderation"),
    ),
    topic(
        "automod", "AutoMod", "Moderation & Safety",
        "Automated message and spam protection with an escalating punishment ladder.",
        page="automod",
        commands=("automod",),
        sections=[
            sec(
                "Description",
                ["AutoMod is off until you enable it. Values below are the defaults for a new server."],
            ),
            sec(
                "What it can catch",
                bullets=[
                    "Banned words, optionally including look-alike spellings. Look-alike (fuzzy) matches can be held "
                    "in a review queue for a moderator to confirm or dismiss instead of being punished at once.",
                    "Discord invite links, with an action mode (strike, delete or log), an allowlist of invite codes, "
                    "links or server IDs, a switch to allow this server's own invites, and exempt channels.",
                    "GIFs (uploaded, or linked from Tenor and Giphy), with per-GIF allow and block lists.",
                    "Capital letters: 70 percent, with at least 10 capital letters.",
                    "Mention spam: 5 unique mentions in one message.",
                    "Message spam: 5 messages in 5 seconds.",
                    "Duplicate spam: 4 identical messages within 30 seconds.",
                ],
            ),
            sec(
                "Exemptions",
                ["Roles can be exempted from AutoMod. This is separate from Bot Manager roles."],
            ),
        ],
        see_also=("warnings", "moderation"),
    ),
    topic(
        "antinuke", "Anti-Nuke", "Moderation & Safety",
        "Detects bursts of destructive actions by one person and punishes them automatically.",
        page="antinuke",
        commands=("antinuke",),
        sections=[
            sec(
                "Description",
                [
                    "Anti-Nuke reads the audit log and reacts when the same person crosses a threshold within a short "
                    "window. It cannot stop a single action, because Discord only reports it afterwards. Deleting one "
                    "channel looks normal; five in ten seconds looks like a nuke.",
                ],
            ),
            sec(
                "Defaults",
                bullets=[
                    "Off until enabled.",
                    "Threshold 3 watched actions within 10 seconds.",
                    "Watched actions: channel delete, role delete, ban, kick, webhook create, bot added.",
                    "Punishment: ban. The choices are ban, kick, timeout and strip roles.",
                    "Auto-recovery is on: deleted channels and roles are recreated by name and type only, not "
                    "permissions or position.",
                ],
            ),
            sec(
                "Notes",
                [
                    "Members can be whitelisted, and incidents are listed and optionally reported to a log channel.",
                    "Counters live in memory, so a bot restart resets everyone's count to zero.",
                ],
            ),
        ],
        see_also=("raid", "emergency"),
    ),
    topic(
        "raid", "Raid Detection", "Moderation & Safety",
        "Watches how fast people join and reacts when it spikes.",
        page="raid",
        commands=("raidmode",),
        sections=[
            sec(
                "Description",
                [
                    "Raid mode starts when the join threshold is crossed within the window, and stays active while "
                    "people keep joining. It is configured on the dashboard; /raidmode status shows what is set and "
                    "whether raid mode is active.",
                ],
            ),
            sec(
                "Defaults",
                bullets=[
                    "Off until enabled.",
                    "Threshold 10 joins within 60 seconds.",
                    "Action: alert only. Other choices are auto-kick new joiners with young accounts, or lock all "
                    "text channels.",
                    "A young account is one created within the last 168 hours (seven days).",
                    "Raid mode ends 300 seconds after the last join.",
                ],
            ),
        ],
        see_also=("antinuke", "emergency", "verification"),
    ),
    topic(
        "emergency", "Emergency tools", "Moderation & Safety",
        "Dashboard-triggered server-wide lockdown, invite revoke and mass timeout.",
        page="emergency",
        sections=[
            sec(
                "Description",
                [
                    "Four buttons for when something is going wrong right now: lockdown, unlock, revoke all invites, "
                    "and mass timeout. They are carried out by the bot, so the bot needs the matching permissions.",
                ],
            ),
        ],
        see_also=("raid", "antinuke"),
    ),
    topic(
        "verification", "Verification", "Moderation & Safety",
        "A button new members click to get a role.",
        page="verification",
        commands=("verification",),
        sections=[
            sec(
                "Description",
                [
                    "Post the verification button in a channel; clicking it gives the member the configured role. "
                    "Use /verification off to turn it off.",
                ],
            ),
        ],
        see_also=("welcome", "raid"),
    ),
    topic(
        "reports", "Reports", "Moderation & Safety",
        "Members report other members; staff triage the reports on the dashboard.",
        page="reports",
        commands=("report",),
        sections=[
            sec(
                "Description",
                [
                    "Reports can be set to notify a channel. On the Reports page staff can change a report's status, "
                    "dismiss it or resolve it. The Staff view includes this page.",
                ],
            ),
        ],
        see_also=("moderation",),
    ),
    topic(
        "rules", "Rules", "Moderation & Safety",
        "Numbered server rules that a warning can cite.",
        page="rules",
        commands=("rule",),
        sections=[
            sec(
                "Description",
                ["Deleting a rule shifts the numbers of later rules down by one."],
            ),
        ],
        see_also=("moderation",),
    ),
    topic(
        "votekick", "Vote kick", "Moderation & Safety",
        "Let the community vote to kick a member.",
        page="moderation", page_label="Moderation (Vote kick settings)",
        commands=("votekick",),
        sections=[
            sec(
                "Description",
                [
                    "Off until enabled. A vote needs 5 yes votes by default and runs for 600 seconds by default. "
                    "Votes are buttons on a message and survive a bot restart.",
                ],
            ),
        ],
        see_also=("moderation",),
    ),
    topic(
        "logging", "Logging", "Moderation & Safety",
        "Server activity logs split into five categories.",
        page="logging",
        commands=("logging",),
        sections=[
            sec(
                "Description",
                [
                    "Categories are messages, members, moderation, server and voice, each sent to a channel of your "
                    "choice. /logging setup (or the dashboard's auto-create) makes any missing channels and wires "
                    "them up. Individual channels can be ignored for message logs, and /logging import backfills a "
                    "channel with existing history for one category.",
                ],
            ),
        ],
        see_also=("snapshots",),
    ),
    topic(
        "snapshots", "Snapshots", "Moderation & Safety",
        "Save and restore ReedMuhn's own settings for a server.",
        page="snapshots",
        sections=[
            sec(
                "Description",
                [
                    "A snapshot captures ReedMuhn's settings tables for the server. It does not touch real Discord "
                    "role or channel permissions, so restoring one never changes your server's actual permissions.",
                ],
            ),
        ],
        see_also=("logging",),
    ),
    # --------------------------------------------------------------- Community
    topic(
        "welcome", "Welcome & Roles", "Community",
        "Welcome messages, optional welcome card images and an automatic role for new members.",
        page="welcome",
        commands=("welcome",),
        sections=[
            sec(
                "Placeholders",
                [
                    "These work in the welcome message:",
                ],
                [
                    "{mention} or {user}: mentions the member",
                    "{user(name)}: username",
                    "{user(proper)}: display name",
                    "{user(id)}: user ID",
                    "{server}: server name",
                    "{server(id)}: server ID",
                    "{server(members)}: member count",
                ],
            ),
            sec(
                "Notes",
                [
                    "The welcome message and the autorole are independent: if posting fails the member still gets "
                    "the role. For the autorole to work, the bot's own role must sit above it in the role list.",
                ],
            ),
        ],
        see_also=("verification", "stickyroles"),
    ),
    topic(
        "reactionroles", "Reaction Roles", "Community",
        "Give or remove roles when members react to a message.",
        page="reactionroles",
        commands=("reactionrole",),
        sections=[
            sec(
                "Description",
                [
                    "Create a menu message, or bind an emoji on an existing message to a role. The dashboard accepts "
                    "a pasted message link, so Developer Mode is not needed.",
                ],
            ),
        ],
        see_also=("stickyroles",),
    ),
    topic(
        "stickyroles", "Sticky Roles", "Community",
        "Give configured roles back to members who leave and rejoin.",
        page="stickyroles",
        commands=("stickyroles",),
        sections=[
            sec(
                "Description",
                ["Specific roles can be excluded so they are never restored."],
            ),
        ],
        see_also=("welcome", "reactionroles"),
    ),
    topic(
        "suggestions", "Suggestions", "Community",
        "Members submit suggestions; staff review them.",
        page="suggestions",
        commands=("suggestion",),
        sections=[
            sec(
                "Description",
                ["Staff set a suggestion's status from the Suggestions page, which the Staff view includes."],
            ),
        ],
    ),
    topic(
        "birthdays", "Birthdays", "Community",
        "Birthday storage and announcements.",
        page="birthdays",
        commands=("birthday",),
        sections=[
            sec(
                "Description",
                ["Members set month and day only; there is no year. Announcements go to the channel you choose."],
            ),
        ],
    ),
    topic(
        "polls", "Polls", "Community",
        "Button polls with live results.",
        page="polls",
        commands=("poll",),
        sections=[
            sec(
                "Description",
                [
                    "Two to five options. The person who started a poll can close it; so can anyone who can manage "
                    "the server.",
                ],
            ),
        ],
    ),
    topic(
        "starboard", "Starboard", "Community",
        "Highlight messages that get enough star reactions.",
        page="starboard",
        commands=("starboard",),
    ),
    topic(
        "invites", "Invites", "Community",
        "Work out which invite brought each member, with leaderboards and milestone roles.",
        page="invites",
        commands=("invites",),
        sections=[
            sec(
                "Description",
                [
                    "Milestones give a role when someone's invite count reaches a number. /invites sync-milestones "
                    "re-checks and re-applies them for one member.",
                ],
            ),
        ],
    ),
    topic(
        "counting", "Counting game", "Community",
        "Members take turns posting the next whole number.",
        page="counting",
        commands=("counting",),
        sections=[
            sec(
                "Rules",
                bullets=[
                    "Only plain whole numbers count (no maths, no text), up to 18 digits.",
                    "A wrong number, or posting twice in a row, sends the count back to 0.",
                ],
            ),
            sec(
                "Saves",
                [
                    "Optional and off by default. The whole server shares one bank: it earns a save each time the "
                    "count reaches a multiple of the save interval (50 by default). When a mistake happens and a save "
                    "is banked, the save is spent and counting carries on. Turn saves on or tune them with "
                    "/counting save-config or on the Counting page.",
                ],
            ),
        ],
    ),
    # ----------------------------------------------------------- Fun & Economy
    topic(
        "fun", "Fun commands", "Fun & Economy",
        "Lightweight community commands.",
        page="fun-commands",
        commands=("fun",),
        sections=[
            sec(
                "Description",
                ["Commands can be switched on or off per server from the Fun Commands page."],
            ),
        ],
    ),
    topic(
        "leveling", "Leveling", "Fun & Economy",
        "XP for chatting, level-up announcements and role rewards.",
        page="leveling",
        commands=("extras rank", "extras leaderboard", "extras levelconfig", "extras levelrole", "extras noxpchannel", "extras boostrole"),
        sections=[
            sec(
                "Defaults",
                [
                    "A member earns 10 XP per message, at most once every 60 seconds. Level-up messages can use "
                    "{user}, {level} and {server}. Channels can be set to earn no XP, and a role can carry an XP "
                    "multiplier.",
                    "These /extras admin commands check the Manage Server permission only; Bot Manager roles do not "
                    "apply to them.",
                ],
            ),
        ],
        see_also=("economy",),
    ),
    topic(
        "economy", "Economy", "Fun & Economy",
        "Coins, daily rewards and a short-cooldown job.",
        page="economy",
        commands=("extras balance", "extras daily", "extras work", "extras pay", "extras richest"),
        sections=[
            sec(
                "Defaults",
                bullets=[
                    "Daily: 250 coins, plus 25 per day of streak, with the streak bonus capped at 500. Claiming again "
                    "within 48 hours keeps the streak alive.",
                    "Work: 20 to 80 coins, once an hour.",
                ],
            ),
        ],
        see_also=("leveling",),
    ),
    topic(
        "giveaways", "Giveaways", "Fun & Economy",
        "Run giveaways that members enter with a reaction.",
        page="giveaways",
        commands=("extras giveaway",),
        sections=[
            sec(
                "Description",
                [
                    "A giveaway runs from 1 minute up to 10080 minutes (seven days) with 1 to 20 winners. Members "
                    "enter by reacting. Use giveaway-end, or the Giveaways page, to end one early.",
                ],
            ),
        ],
    ),
    topic(
        "music", "Music", "Fun & Economy",
        "Queues, playlists, lyrics, effects, DJ role and 24/7 mode, played through a Lavalink node.",
        page="music",
        commands=("music",),
        sections=[
            sec(
                "Turning it on",
                [
                    "Music is off until LAVALINK_PASSWORD is set in .env. Start the bundled Lavalink with "
                    "'docker compose --profile music up -d'. Without the password the bot runs normally and the boot "
                    "report shows music as off.",
                ],
            ),
            sec(
                "Per-server settings",
                ["The Music page sets the default volume, DJ role, 24/7 mode and whether the bot joins deafened."],
            ),
            sec(
                "No audio?",
                [
                    "If the bot joins voice but nothing plays and the Lavalink log mentions 'Sign in to confirm you're "
                    "not a bot', that is YouTube tightening its bot detection, not a ReedMuhn bug. Updating the "
                    "YouTube client list or enabling the plugin's OAuth option in lavalink/application.yml are the "
                    "usual fixes; see the project README.",
                ],
            ),
        ],
    ),
    # ------------------------------------------------------------ Server Tools
    topic(
        "customcommands", "Custom commands", "Server Tools",
        "Per-server text commands.",
        page="commands", page_label="Custom Commands",
        commands=("customcommand",),
        sections=[
            sec(
                "Description",
                [
                    "A trigger is whatever text you choose, such as !rules. When a message matches it the bot replies "
                    "with your text. Replies never ping anyone, and each member has a 3 second cooldown between "
                    "triggers.",
                ],
            ),
        ],
    ),
    topic(
        "reminders", "Reminders and scheduled tasks", "Server Tools",
        "/remindme, plus the list of things waiting to happen.",
        page="scheduled", page_label="Scheduled Tasks",
        commands=("remindme",),
        sections=[
            sec(
                "Description",
                [
                    "Durations look like 10m, 2h or 3d. A background loop checks for due events every 30 seconds, "
                    "and unbans, unmutes, nickname reverts, reminders and poll closes all survive a restart. If one "
                    "fails it is retried later instead of being dropped.",
                    "The Scheduled Tasks page can cancel a reminder or revert a temporary nickname right away.",
                ],
            ),
        ],
    ),
    topic(
        "tickets", "Tickets", "Server Tools",
        "Private support ticket channels.",
        page="tickets",
        commands=("ticket",),
        sections=[
            sec(
                "Setup",
                [
                    "/ticket setup picks the category tickets are created under and the support role that can see "
                    "them. /ticket setpanel posts the 'Open a Ticket' button. You can choose whether closing a "
                    "ticket deletes its channel.",
                ],
            ),
        ],
        see_also=("modmail",),
    ),
    topic(
        "modmail", "Modmail", "Server Tools",
        "Members message the bot; staff reply in a private thread channel.",
        page="modmail",
        commands=("modmail",),
        sections=[
            sec(
                "Description",
                ["Close the thread from inside its channel. Staff can block a member from opening threads."],
            ),
        ],
        see_also=("tickets",),
    ),
    topic(
        "tempvoice", "Temporary Voice", "Server Tools",
        "Joining a hub channel gives a member their own temporary voice channel.",
        page="tempvoice",
        commands=("voicehub",),
        sections=[
            sec(
                "What the creator can do",
                [
                    "You choose which of these a room's creator gets inside their own room. The defaults are Manage "
                    "Channels, Mute, Deafen and Move Members.",
                ],
                [
                    "Rename, resize and edit the channel",
                    "Manage permissions on the channel",
                    "Create invites to the channel",
                    "Mute people in the channel",
                    "Deafen people in the channel",
                    "Move people out of the channel",
                    "Priority speaker",
                ],
            ),
            sec(
                "Hard limits",
                [
                    "That list is a ceiling enforced in the dashboard, the database and the bot. Administrator, "
                    "Manage Server and server-wide Manage Roles can never be handed out this way.",
                    "Other options: an optional temporary role that is deleted with the room, an optional log "
                    "channel, one room per person, a per-server cap and a short cooldown. /voicehub perms shows "
                    "members what the setup allows.",
                ],
            ),
        ],
    ),
    topic(
        "counters", "Live counters", "Server Tools",
        "Voice channels whose names show live server numbers.",
        page="counters",
        commands=("extras counter",),
        sections=[
            sec(
                "Description",
                ["Counters show things like member, online, bot and channel counts and are refreshed by the bot."],
            ),
        ],
    ),
    topic(
        "info", "Info and utility commands", "Server Tools",
        "Member lookup, server facts, latency and posting as the bot.",
        page="", page_label="Dashboard (server info on the Overview)",
        commands=("whois", "serverinfo", "ping", "say"),
        sections=[
            sec(
                "Notes",
                [
                    "/serverinfo shows the same facts as the dashboard Overview: people and bots, channels, roles, "
                    "boosts, owner and server age. /say can only be used by the server owner or an Administrator.",
                ],
            ),
        ],
    ),
    # ------------------------------------------------------------ Integrations
    topic(
        "ai", "AI assistant", "Integrations",
        "Connect any OpenAI-compatible provider and let members ask it questions.",
        page="ai",
        commands=("ask", "ai"),
        sections=[
            sec(
                "Setup",
                [
                    "Set the provider, base URL, model, API key and system prompt on the AI page. Presets are "
                    "OpenAI-compatible, OpenAI, Groq, Ollama and custom; Ollama needs no API key. The key is stored "
                    "in the private database and never echoed back.",
                ],
            ),
            sec(
                "Limits",
                bullets=[
                    "5 questions per member per minute in each server.",
                    "Prompts up to 4000 characters.",
                    "Channel indexing is opt-in. When it is on, relevant indexed messages are sent to your provider "
                    "as context; /ai clearindex deletes the index.",
                ],
            ),
        ],
    ),
    topic(
        "youtube", "YouTube notifications", "Integrations",
        "Announce new uploads from YouTube channels.",
        page="youtube",
        commands=("youtube",),
        sections=[
            sec(
                "Description",
                ["Uploads are detected through each channel's RSS feed."],
            ),
        ],
        see_also=("twitch-feeds",),
    ),
    topic(
        "twitch-feeds", "Twitch and RSS feeds", "Integrations",
        "Live notifications for Twitch channels and posts from RSS or Atom feeds.",
        page="twitch", page_label="Twitch (and RSS Feeds)",
        commands=("extras twitch", "extras feed"),
        sections=[
            sec(
                "Twitch",
                ["Needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET in .env. Leave them blank to disable Twitch."],
            ),
            sec(
                "RSS and Atom safety",
                [
                    "Feed URLs must be HTTP or HTTPS, may not contain credentials, and may not point at localhost, "
                    "private or link-local addresses or non-standard ports. Redirects are capped at 3, responses at "
                    "2 MiB, and each fetch times out after 10 seconds. One poll announces at most 50 unseen entries.",
                ],
            ),
        ],
        see_also=("youtube",),
    ),
    topic(
        "talk-feed", "Talk and Channel Feed", "Integrations",
        "Send messages through the bot, and watch a channel from the dashboard.",
        page="talk", page_label="Talk (and Channel Feed)",
        sections=[
            sec(
                "Talk",
                ["Relays a message you write in the dashboard through the bot."],
            ),
            sec(
                "Channel Feed",
                [
                    "Mirrors a channel's messages to the dashboard, read-only. Turning it off deletes the stored "
                    "rows for your server. Stored messages are pruned to a short retention window and a per-channel "
                    "row cap, and the FEED_RETENTION_DAYS setting can expire them sooner.",
                ],
            ),
        ],
    ),
]

BY_SLUG = {t["slug"]: t for t in TOPICS}


def commands_for(prefixes) -> list[tuple[str, str, str]]:
    """Every generated command whose qualified name starts with one of the prefixes."""
    out = []
    seen = set()
    for lines in COMMANDS.values():
        for qualified, description, requirement in lines:
            if qualified in seen:
                continue
            if any(qualified.startswith(p) for p in prefixes):
                seen.add(qualified)
                out.append((qualified, description, requirement))
    return out


def grouped_topics() -> list[tuple[str, list[dict]]]:
    return [(group, [t for t in TOPICS if t["group"] == group]) for group in GROUPS]


def search_topics(query: str) -> list[dict]:
    """Topics whose title, summary, text or command names contain every word of the query."""
    words = [w for w in query.lower().split() if w]
    if not words:
        return []
    hits = []
    for t in TOPICS:
        parts = [t["title"], t["summary"], t["page_label"], t["slug"]]
        for s in t["sections"]:
            parts += [s["title"], *s["text"], *s["bullets"]]
        parts += [c[0] + " " + c[1] for c in commands_for(t["commands"])]
        haystack = " ".join(parts).lower()
        if all(w in haystack for w in words):
            hits.append(t)
    return hits
