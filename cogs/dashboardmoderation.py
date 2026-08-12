"""WebUI -> Discord moderation action queue.

The dashboard is a separate process, so destructive Discord actions requested
from the WebUI are queued in SQLite and executed by the bot process. This keeps
Discord API access and permission checks in the bot while still making the
feature fully usable from the dashboard.
"""
import logging

import discord
from discord.ext import commands, tasks

logger = logging.getLogger("dashboardmoderation")


class DashboardModeration(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_purge_requests.start()

    def cog_unload(self):
        self.poll_purge_requests.cancel()

    async def _channel(self, guild_id: int, channel_id: int):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None, "The bot is no longer in that server."
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                return None, "The channel no longer exists."
            except discord.Forbidden:
                return None, "The bot cannot access that channel."
            except discord.HTTPException as exc:
                return None, f"Discord could not resolve the channel: {exc}"
        if not isinstance(channel, discord.TextChannel):
            return None, "Target channel is not a standard text channel."
        me = guild.me
        if me is not None and not channel.permissions_for(me).manage_messages:
            return None, "The bot does not have Manage Messages in that channel."
        return channel, None

    @staticmethod
    async def _purge(channel: discord.TextChannel, amount: int, user_id: int | None, reason: str) -> int:
        deleted = 0
        before = None
        fourteen_days = 14 * 86400
        while deleted < amount:
            batch_size = min(100, amount - deleted)
            kwargs = {"limit": batch_size}
            if before is not None:
                kwargs["before"] = before
            messages = [m async for m in channel.history(**kwargs)]
            if not messages:
                break
            targets = messages if user_id is None else [m for m in messages if m.author.id == user_id]
            targets = targets[: amount - deleted]
            recent = [m for m in targets if (discord.utils.utcnow() - m.created_at).total_seconds() < fourteen_days]
            old = [m for m in targets if m not in recent]
            if recent:
                try:
                    deleted += len(await channel.delete_messages(recent))
                except (discord.Forbidden, discord.HTTPException):
                    for message in recent:
                        try:
                            await message.delete(reason=reason)
                            deleted += 1
                        except (discord.NotFound, discord.Forbidden):
                            pass
            for message in old:
                if deleted >= amount:
                    break
                try:
                    await message.delete(reason=reason)
                    deleted += 1
                except (discord.NotFound, discord.Forbidden):
                    pass
            before = messages[-1]
            if len(messages) < batch_size:
                break
        return deleted

    @tasks.loop(seconds=2)
    async def poll_purge_requests(self):
        try:
            requests = self.bot.db.claim_purge_requests(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI purge requests")
            return
        for request_id, guild_id, channel_id, user_id, amount, reason in requests:
            try:
                channel, error = await self._channel(guild_id, channel_id)
                if channel is None:
                    self.bot.db.complete_purge_request(request_id, error)
                    continue
                deleted = await self._purge(channel, amount, user_id, reason)
                self.bot.db.complete_purge_request(request_id)
                logger.info("WebUI purge %s deleted %s messages in %s", request_id, deleted, channel_id)
            except Exception as exc:
                logger.exception("WebUI purge %s failed", request_id)
                self.bot.db.complete_purge_request(request_id, str(exc)[:500])

    @poll_purge_requests.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardModeration(bot))
