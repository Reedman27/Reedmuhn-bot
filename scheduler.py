"""Background scheduler. One table, one loop, dispatched by event_name - the
same design yagpdb uses for its ScheduledEvents. Adding a new kind of
scheduled thing later means adding one branch in _process_event, not a new
table and a new loop.
"""
import asyncio
import json
import logging
import time

import aiohttp
import discord

from cogs.reactionroles import parse_menu_pairs, resolve_emoji_key
from cogs.youtube import resolve_youtube_channel
from utils import restore_stripped_roles

logger = logging.getLogger("scheduler")

CHECK_INTERVAL_SECONDS = 30

# Retry policy for scheduled events whose handler raised. A scheduled
# moderation action (unban, unmute, tempnick revert) is supposed to survive
# restarts and eventually execute, so a failure reschedules the event instead
# of deleting it - see _handle_event_failure.
RETRY_BASE_DELAY = 60          # transient failures: network, 429, Discord 5xx
PERMANENT_RETRY_DELAY = 600    # permission/config failures: needs a human
MAX_RETRY_DELAY = 3600
MAX_RETRY_ATTEMPTS = 10        # transient budget (~hours with backoff)
MAX_PERMANENT_ATTEMPTS = 5     # permanent budget, then give up loudly


def schedule_unban(db, guild_id: int, user_id: int, run_at: int) -> None:
    db.insert_scheduled_event("unban", guild_id, run_at, {"user_id": user_id})


def schedule_reminder(db, guild_id: int, run_at: int, user_id: int, channel_id: int, message: str) -> None:
    db.insert_scheduled_event(
        "reminder", guild_id, run_at,
        {"user_id": user_id, "channel_id": channel_id, "message": message},
    )


def schedule_nick_revert(db, guild_id: int, run_at: int, user_id: int, original_nick) -> None:
    """original_nick is the member's nickname *before* the tempnick change -
    None means they had no nickname override (were showing their username).
    """
    db.insert_scheduled_event("revert_nick", guild_id, run_at, {"user_id": user_id, "original_nick": original_nick})


def schedule_role_unmute(db, guild_id: int, run_at: int, user_id: int, role_id: int) -> None:
    # Replacing an existing expiry prevents a second mute from being undone
    # by the first mute's older timer.
    db.replace_role_unmute_event(guild_id, user_id, role_id, run_at)


def schedule_poll_close(db, guild_id: int, run_at: int, poll_id: int) -> None:
    db.insert_scheduled_event("close_poll", guild_id, run_at, {"poll_id": poll_id})


async def run_loop(bot: discord.Client, db) -> None:
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)

        try:
            due = db.due_events(int(time.time()))
        except Exception:
            logger.exception("failed to fetch due scheduled events")
            continue

        for event_id, event_name, guild_id, data_json, attempts in due:
            try:
                await _process_event(bot, db, event_id, event_name, guild_id, json.loads(data_json))
            except Exception as exc:
                logger.exception("scheduled event %s (%s) failed", event_id, event_name)
                _handle_event_failure(db, event_id, event_name, guild_id, attempts, exc)
                continue
            # Success - and only success - removes the event.
            try:
                db.delete_scheduled_event(event_id)
            except Exception:
                logger.exception("failed to delete completed scheduled event %s", event_id)


def _is_transient(exc: Exception) -> bool:
    """Network blips, rate limits and Discord 5xx are worth retrying.
    Everything else (missing permissions, deleted channel/role, bad config)
    won't get better on its own."""
    if isinstance(exc, aiohttp.ClientError) or isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, discord.HTTPException):
        status = getattr(exc, "status", None)
        return status == 429 or (status is not None and status >= 500)
    return False


def _handle_event_failure(db, event_id: int, event_name: str, guild_id: int, attempts: int, exc: Exception) -> None:
    """Decide what happens to a scheduled event whose handler raised.

    TRANSIENT  -> keep the row, retry with a growing backoff.
    PERMANENT  -> keep retrying a few times anyway (a 403 is often a role
                  hierarchy an admin is about to fix), then give up loudly.
    Nothing is deleted just because the handler raised - the old
    delete-in-a-finally-block meant a temporary Discord error could silently
    lose a scheduled unban/unmute/tempnick revert forever.
    """
    transient = _is_transient(exc)
    budget = MAX_RETRY_ATTEMPTS if transient else MAX_PERMANENT_ATTEMPTS
    detail = f"{type(exc).__name__}: {exc}"
    if attempts + 1 >= budget:
        logger.error(
            "giving up on scheduled event %s (%s) for guild %s after %s attempts: %s",
            event_id, event_name, guild_id, attempts + 1, detail,
        )
        try:
            db.record_bot_event(
                "scheduler.event_failed", guild_id, None, None,
                f"event={event_name};attempts={attempts + 1};error={detail}", status="failure",
            )
        except Exception:
            logger.exception("failed to record scheduler failure for event %s", event_id)
        try:
            db.delete_scheduled_event(event_id)
        except Exception:
            logger.exception("failed to delete exhausted scheduled event %s", event_id)
        return
    delay = min(MAX_RETRY_DELAY, (RETRY_BASE_DELAY if transient else PERMANENT_RETRY_DELAY) * (2 ** min(attempts, 6)))
    try:
        db.reschedule_scheduled_event(event_id, int(time.time()) + delay, detail)
    except Exception:
        logger.exception("failed to reschedule scheduled event %s", event_id)
    else:
        logger.warning(
            "scheduled event %s (%s) failed (%s attempt %s) - retrying in %ss: %s",
            event_id, event_name, "transient" if transient else "permanent", attempts + 1, delay, detail,
        )


async def _process_event(bot: discord.Client, db, event_id: int, event_name: str, guild_id: int, data: dict) -> None:
    if event_name == "unban":
        await _handle_unban(bot, guild_id, data)
    elif event_name == "reminder":
        await _handle_reminder(bot, data)
    elif event_name == "revert_nick":
        await _handle_revert_nick(bot, guild_id, data)
    elif event_name == "unmute_role":
        await _handle_unmute_role(bot, db, event_id, guild_id, data)
    elif event_name == "add_reaction_role":
        await _handle_add_reaction_role(bot, db, guild_id, data)
    elif event_name == "create_reaction_role_menu":
        await _handle_create_reaction_role_menu(bot, db, guild_id, data)
    elif event_name == "add_youtube_watch":
        await _handle_add_youtube_watch(bot, db, guild_id, data)
    elif event_name == "close_poll":
        await _handle_close_poll(bot, db, guild_id, data)
    elif event_name == "end_giveaway":
        await _handle_end_giveaway(bot, data)
    elif event_name == "sync_level_rewards":
        await _handle_sync_level_rewards(bot, guild_id, data)
    elif event_name == "ticket_channel_delete":
        await _handle_ticket_channel_delete(bot, guild_id, data)
    elif event_name == "ticket_channel_lock":
        await _handle_ticket_channel_lock(bot, guild_id, data)
    else:
        logger.warning("unknown scheduled event kind: %s", event_name)


async def _handle_unban(bot: discord.Client, guild_id: int, data: dict) -> None:
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    await guild.unban(discord.Object(id=data["user_id"]))


async def _handle_reminder(bot: discord.Client, data: dict) -> None:
    channel = bot.get_channel(data["channel_id"]) or await bot.fetch_channel(data["channel_id"])
    await channel.send(
        f"<@{data['user_id']}> reminder: {data['message']}",
        allowed_mentions=discord.AllowedMentions(users=[discord.Object(id=int(data["user_id"]))]),
    )


async def _handle_revert_nick(bot: discord.Client, guild_id: int, data: dict) -> None:
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    try:
        member = guild.get_member(data["user_id"]) or await guild.fetch_member(data["user_id"])
    except discord.NotFound:
        return  # they left the server - nothing to revert
    await member.edit(nick=data["original_nick"], reason="Tempnick expired - reverting nickname")


async def _handle_unmute_role(bot: discord.Client, db, event_id: int, guild_id: int, data: dict) -> None:
    # A re-mute can replace an older expiry after the scheduler has already
    # fetched its due rows. Verify this exact event still exists before acting;
    # otherwise the stale timer could unmute someone early.
    if db.get_scheduled_event(event_id, guild_id, "unmute_role") is None:
        return
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    try:
        member = guild.get_member(data["user_id"]) or await guild.fetch_member(data["user_id"])
    except discord.NotFound:
        return  # they left - nothing to unmute
    role = guild.get_role(data["role_id"])
    if role is not None and role in member.roles:
        # Deliberately not caught here: letting the error reach the scheduler
        # is what keeps the event alive for a retry. Swallowing a Forbidden
        # (or a 429) meant the event was then deleted and the member stayed
        # muted forever with only a log line to show for it.
        await member.remove_roles(role, reason="Mute duration expired")
    await restore_stripped_roles(db, guild, member, reason="Mute duration expired")


async def _handle_add_reaction_role(bot: discord.Client, db, guild_id: int, data: dict) -> None:
    """Fulfills a reaction-role binding queued from the web dashboard. The
    dashboard has no Discord connection of its own (it only shares bot.db
    over a mounted volume), so it can't place the actual reaction on the
    message itself - it queues the request here instead, and the bot (which
    does have a live connection) does the real work on its next scheduler
    tick. Same reasoning, same mechanism as tempban/reminder/tempnick, just
    with a near-immediate run_at instead of a future one.
    """
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)

    role = guild.get_role(data["role_id"])
    if role is None:
        logger.warning("dashboard reaction role add: role %s no longer exists in guild %s", data["role_id"], guild_id)
        return
    if role.is_default() or role.managed or role.position >= guild.me.top_role.position:
        logger.warning("dashboard reaction role add: role %s isn't assignable in guild %s", data["role_id"], guild_id)
        return

    channel = guild.get_channel(data["channel_id"]) or await bot.fetch_channel(data["channel_id"])
    try:
        message = await channel.fetch_message(data["message_id"])
    except (discord.NotFound, discord.Forbidden):
        logger.warning(
            "dashboard reaction role add: couldn't fetch message %s in channel %s (guild %s)",
            data["message_id"], data["channel_id"], guild_id,
        )
        return

    emoji_key = resolve_emoji_key(data["emoji"])
    if emoji_key is None:
        logger.warning("dashboard reaction role add: %r isn't a valid emoji", data["emoji"])
        return

    try:
        await message.add_reaction(emoji_key)
    except discord.HTTPException:
        logger.warning("dashboard reaction role add: couldn't react with %s (guild %s)", emoji_key, guild_id)
        return

    db.add_reaction_role(guild_id, message.id, channel.id, emoji_key, role.id)


async def _handle_create_reaction_role_menu(bot: discord.Client, db, guild_id: int, data: dict) -> None:
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    channel = guild.get_channel(data["channel_id"]) or await bot.fetch_channel(data["channel_id"])
    if not isinstance(channel, discord.TextChannel):
        logger.warning("dashboard reaction-role menu: channel %s is not text in guild %s", data["channel_id"], guild_id)
        return
    try:
        parsed = parse_menu_pairs(data["pairs"])
    except ValueError as exc:
        logger.warning("dashboard reaction-role menu: invalid pairs in guild %s: %s", guild_id, exc)
        return
    valid = []
    for emoji_key, role_id in parsed:
        role = guild.get_role(role_id)
        if role is None or role.is_default() or role.managed or role.position >= guild.me.top_role.position:
            logger.warning("dashboard reaction-role menu: role %s isn't assignable in guild %s", role_id, guild_id)
            return
        valid.append((emoji_key, role))
    embed = discord.Embed(
        title=(data.get("title") or "Reaction Roles")[:256],
        description=(data.get("description") or "React below to add/remove a role.")[:4096],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Roles", value="\n".join(f"{emoji}  <@&{role.id}>" for emoji, role in valid), inline=False)
    message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    for emoji_key, role in valid:
        await message.add_reaction(emoji_key)
        db.add_reaction_role(guild_id, message.id, channel.id, emoji_key, role.id)



async def _handle_add_youtube_watch(bot: discord.Client, db, guild_id: int, data: dict) -> None:
    """Fulfills a "watch this channel" request queued from the web
    dashboard. Resolving a pasted URL/handle/ID into the real YouTube
    channel ID (and its display name) requires an HTTP fetch, and the
    dashboard process doesn't keep a client session around for that - it
    queues the raw input here, and the bot (which already polls YouTube on
    a timer, so already owns a session for this) resolves and stores it on
    its next scheduler tick.
    """
    cog = bot.get_cog("YouTube")
    if cog is None:
        logger.warning("dashboard youtube watch add: YouTube cog isn't loaded")
        return
    if cog.session is None:
        cog.session = aiohttp.ClientSession()

    resolved = await resolve_youtube_channel(cog.session, data["channel"])
    if resolved is None:
        logger.warning("dashboard youtube watch add: couldn't resolve %r in guild %s", data["channel"], guild_id)
        return
    channel_id, channel_name = resolved

    db.add_youtube_watch(guild_id, channel_id, data["announce_channel_id"])
    if channel_name:
        db.set_youtube_channel_name(guild_id, channel_id, channel_name)


async def _handle_close_poll(bot: discord.Client, db, guild_id: int, data: dict) -> None:
    """Auto-closes a poll that was started with a duration. Delegates to
    the Polls cog so the message-editing logic (and its error handling)
    lives in exactly one place."""
    cog = bot.get_cog("Polls")
    if cog is None:
        logger.warning("scheduled poll close: Polls cog isn't loaded")
        return
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    await cog._close_poll(guild, data["poll_id"])


async def _handle_ticket_channel_delete(bot: discord.Client, guild_id: int, data: dict) -> None:
    """Durable half of ticket delete-on-close.

    The cog deletes the channel itself after its countdown; this event is the
    backstop for the cases that used to lose the deletion entirely - the bot
    restarting mid-countdown, or Discord refusing the delete. Anything that
    isn't already gone and isn't deletable raises, so the scheduler retries.
    """
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    channel = guild.get_channel(data["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(data["channel_id"])
        except discord.NotFound:
            return  # already deleted - nothing to do
    try:
        await channel.delete(reason=f"Ticket #{data.get('ticket_id')} closed (auto-delete)")
    except discord.NotFound:
        return


async def _handle_ticket_channel_lock(bot: discord.Client, guild_id: int, data: dict) -> None:
    """Retries locking/renaming a closed ticket channel. Without this, a
    Discord failure at close time left the database saying closed while the
    channel stayed fully usable."""
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    channel = guild.get_channel(data["channel_id"])
    if channel is None:
        return  # channel is gone; nothing left to lock
    opener_id = data.get("opener_id")
    if opener_id:
        opener = guild.get_member(int(opener_id))
        if opener is not None:
            await channel.set_permissions(opener, view_channel=True, send_messages=False, reason="Ticket closed")
    if not channel.name.startswith("closed-"):
        await channel.edit(name=f"closed-{channel.name}"[:100], reason="Ticket closed")


async def _handle_sync_level_rewards(bot: discord.Client, guild_id: int, data: dict) -> None:
    """Applies level-role rewards after the WebUI changed someone's XP.

    The dashboard process has no Discord connection, so it can only write the
    new XP/level to SQLite and queue this. Without it, an admin setting a
    member from level 4 to level 10 left the database saying level 10 while
    the member never received the level 5-10 reward roles - normal message XP
    applied them, the dashboard path didn't. Both now end up in the same
    Extras cog code path.
    """
    cog = bot.get_cog("Extras")
    if cog is None:
        logger.warning("level reward sync: Extras cog isn't loaded")
        return
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    user_id = int(data["user_id"])
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return  # they left the server
    await cog.sync_level_state(member)


async def _handle_end_giveaway(bot: discord.Client, data: dict) -> None:
    """Ends a giveaway queued from the WebUI (the "End now" button on the
    Extras page). Delegates to the Extras cog so the winner-picking logic
    (and its error handling) lives in exactly one place."""
    cog = bot.get_cog("Extras")
    if cog is None:
        logger.warning("scheduled giveaway end: Extras cog isn't loaded")
        return
    await cog._end_giveaway(data["message_id"])
