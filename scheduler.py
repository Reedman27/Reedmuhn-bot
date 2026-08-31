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

        for event_id, event_name, guild_id, data_json in due:
            retry_at = None
            try:
                await _process_event(bot, db, event_id, event_name, guild_id, json.loads(data_json))
            except Exception as exc:
                logger.exception("scheduled event %s (%s) failed", event_id, event_name)
                status = getattr(exc, "status", None)
                # Don't silently lose work on transient Discord/API failures.
                # Permanent 4xx errors (bad permissions, deleted resources,
                # invalid configuration) still get removed so they don't loop.
                if isinstance(exc, aiohttp.ClientError) or (
                    isinstance(exc, discord.HTTPException)
                    and (status == 429 or (status is not None and status >= 500))
                ):
                    retry_at = int(time.time()) + 60
            finally:
                try:
                    db.delete_scheduled_event(event_id)
                    if retry_at is not None:
                        db.insert_scheduled_event(
                            event_name, guild_id, retry_at, json.loads(data_json)
                        )
                except Exception:
                    logger.exception("failed to finalize scheduled event %s", event_id)


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
    if role is not None:
        try:
            await member.remove_roles(role, reason="Mute duration expired")
        except discord.Forbidden:
            logger.warning("couldn't remove muted role from %s in guild %s - missing permission or role hierarchy", data["user_id"], guild_id)
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
