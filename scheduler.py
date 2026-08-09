"""Background scheduler. One table, one loop, dispatched by event_name - the
same design yagpdb uses for its ScheduledEvents. Adding a new kind of
scheduled thing later means adding one branch in _process_event, not a new
table and a new loop.
"""
import asyncio
import json
import logging
import time

import discord

from cogs.reactionroles import resolve_emoji_key

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
            try:
                await _process_event(bot, db, event_name, guild_id, json.loads(data_json))
            except Exception:
                logger.exception("scheduled event %s (%s) failed", event_id, event_name)
            finally:
                # Delete even on failure - a bad/stale event (e.g. user
                # already unbanned manually) shouldn't retry forever.
                try:
                    db.delete_scheduled_event(event_id)
                except Exception:
                    logger.exception("failed to delete completed scheduled event %s", event_id)


async def _process_event(bot: discord.Client, db, event_name: str, guild_id: int, data: dict) -> None:
    if event_name == "unban":
        await _handle_unban(bot, guild_id, data)
    elif event_name == "reminder":
        await _handle_reminder(bot, data)
    elif event_name == "revert_nick":
        await _handle_revert_nick(bot, guild_id, data)
    elif event_name == "add_reaction_role":
        await _handle_add_reaction_role(bot, db, guild_id, data)
    else:
        logger.warning("unknown scheduled event kind: %s", event_name)


async def _handle_unban(bot: discord.Client, guild_id: int, data: dict) -> None:
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    await guild.unban(discord.Object(id=data["user_id"]))


async def _handle_reminder(bot: discord.Client, data: dict) -> None:
    channel = bot.get_channel(data["channel_id"]) or await bot.fetch_channel(data["channel_id"])
    await channel.send(f"<@{data['user_id']}> reminder: {data['message']}")


async def _handle_revert_nick(bot: discord.Client, guild_id: int, data: dict) -> None:
    guild = bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)
    try:
        member = guild.get_member(data["user_id"]) or await guild.fetch_member(data["user_id"])
    except discord.NotFound:
        return  # they left the server - nothing to revert
    await member.edit(nick=data["original_nick"], reason="Tempnick expired - reverting nickname")


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
