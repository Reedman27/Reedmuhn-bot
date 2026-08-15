"""Reliable WebUI -> Discord outbound messaging.

The WebUI and bot are separate processes, so Talk uses the shared SQLite DB as
an outbox. Messages are claimed with a short lease, sent only after claiming,
and then marked sent only after Discord confirms delivery. Transient failures
are retried; permanent failures remain visible in the dashboard.
"""
import logging
import time

import discord
from discord.ext import commands, tasks

logger = logging.getLogger("dashboardtalk")

POLL_INTERVAL_SECONDS = 2
CLAIM_BATCH_SIZE = 10
LEASE_SECONDS = 120


class DashboardTalk(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_outbound_messages.start()
        self.poll_delete_requests.start()

    def cog_unload(self):
        self.poll_outbound_messages.cancel()
        self.poll_delete_requests.cancel()

    async def _get_channel(self, guild_id: int, channel_id: int, *, require_send: bool = True):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return None, "The bot is no longer in that server."

        channel = guild.get_channel(channel_id)
        if channel is None:
            # The cache can lag behind Discord. Fetching here also handles a
            # channel that was created after the last cache synchronization.
            try:
                channel = await guild.fetch_channel(channel_id)
            except discord.NotFound:
                return None, "The Discord channel no longer exists."
            except discord.Forbidden:
                return None, "The bot cannot access that Discord channel."
            except discord.HTTPException as exc:
                return None, f"Discord could not resolve the channel: {exc}"

        if not isinstance(channel, discord.abc.Messageable):
            return None, "That channel cannot receive messages."

        # Check permissions before attempting the API call when Discord gives
        # us a guild channel with permission information. This produces a much
        # clearer failure than a generic HTTP 403. Deleting the bot's own
        # message only needs view_channel, not send_messages, so callers that
        # are only deleting pass require_send=False.
        me = guild.me
        permissions_for = getattr(channel, "permissions_for", None)
        if me is not None and permissions_for is not None:
            perms = permissions_for(me)
            if not perms.view_channel:
                return None, "The bot cannot view that channel."
            if require_send and not perms.send_messages:
                return None, "The bot does not have Send Messages permission there."

        return channel, None

    @staticmethod
    def _is_transient_http_error(error: discord.HTTPException) -> bool:
        status = getattr(error, "status", None)
        # 429 and 5xx are normally recoverable. Other HTTP errors generally
        # need a human/configuration fix and should not loop forever.
        return status == 429 or (status is not None and status >= 500)

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_outbound_messages(self):
        try:
            messages = self.bot.db.claim_outbound_messages(
                limit=CLAIM_BATCH_SIZE, lease_seconds=LEASE_SECONDS
            )
        except Exception:
            logger.exception("dashboard talk: failed to claim outbound messages")
            return

        for message_id, guild_id, channel_id, content, _attempts in messages:
            channel, lookup_error = await self._get_channel(guild_id, channel_id)
            if channel is None:
                status = self.bot.db.mark_outbound_message_failed(
                    message_id, lookup_error or "Unable to resolve channel", retry=False
                )
                logger.warning(
                    "dashboard talk: message %s failed permanently: %s (status=%s)",
                    message_id, lookup_error, status,
                )
                self._record_event(
                    "dashboard.talk.failed", guild_id, channel_id,
                    f"message_id={message_id} error={lookup_error}", status="failed"
                )
                continue

            try:
                sent_message = await channel.send(
                    content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.Forbidden as exc:
                error = "Discord denied sending in that channel (check Send Messages permission)."
                self.bot.db.mark_outbound_message_failed(message_id, error, retry=False)
                logger.warning("dashboard talk: message %s forbidden in channel %s", message_id, channel_id)
                self._record_event("dashboard.talk.failed", guild_id, channel_id,
                                   f"message_id={message_id} error={error}", status="failed")
            except discord.NotFound as exc:
                error = "The Discord channel or destination no longer exists."
                self.bot.db.mark_outbound_message_failed(message_id, error, retry=False)
                logger.warning("dashboard talk: message %s destination disappeared", message_id)
                self._record_event("dashboard.talk.failed", guild_id, channel_id,
                                   f"message_id={message_id} error={error}", status="failed")
            except discord.HTTPException as exc:
                retry = self._is_transient_http_error(exc)
                error = f"Discord HTTP {getattr(exc, 'status', 'error')}: {exc}"
                status = self.bot.db.mark_outbound_message_failed(message_id, error, retry=retry)
                logger.warning(
                    "dashboard talk: message %s HTTP failure in channel %s: %s (status=%s)",
                    message_id, channel_id, error, status,
                )
                self._record_event("dashboard.talk.failed", guild_id, channel_id,
                                   f"message_id={message_id} retry={retry} status={status} error={error}", status="failed")
            except Exception as exc:
                # Unknown exceptions are retried a limited number of times.
                # They are still recorded so a broken adapter does not make
                # the WebUI falsely report success.
                error = f"Unexpected send error: {type(exc).__name__}: {exc}"
                status = self.bot.db.mark_outbound_message_failed(message_id, error, retry=True)
                logger.exception("dashboard talk: unexpected failure for message %s", message_id)
                self._record_event("dashboard.talk.failed", guild_id, channel_id,
                                   f"message_id={message_id} status={status} error={error}", status="failed")
            else:
                self.bot.db.mark_outbound_message_sent(message_id, int(sent_message.id))
                logger.info(
                    "dashboard talk: message %s sent to guild=%s channel=%s discord_message=%s",
                    message_id, guild_id, channel_id, sent_message.id,
                )
                self._record_event(
                    "dashboard.talk.sent", guild_id, channel_id,
                    f"message_id={message_id} discord_message_id={sent_message.id}"
                )

    def _record_event(self, event_type: str, guild_id: int, target_id: int, details: str, *, status: str = "success"):
        try:
            self.bot.db.record_bot_event(
                event_type, guild_id, None, target_id, details,
                source="dashboard_talk", status=status,
            )
        except Exception:
            logger.exception("dashboard talk: failed to write audit event")

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_delete_requests(self):
        try:
            requests = self.bot.db.claim_message_delete_requests(limit=CLAIM_BATCH_SIZE)
        except Exception:
            logger.exception("dashboard talk: failed to claim delete requests")
            return

        for message_id, guild_id, channel_id, discord_message_id in requests:
            channel, lookup_error = await self._get_channel(guild_id, channel_id, require_send=False)
            if channel is None:
                self.bot.db.mark_message_delete_failed(message_id, lookup_error or "Unable to resolve channel")
                logger.warning("dashboard talk: delete %s failed to resolve channel: %s", message_id, lookup_error)
                self._record_event(
                    "dashboard.talk.delete_failed", guild_id, channel_id,
                    f"message_id={message_id} error={lookup_error}", status="failed"
                )
                continue

            try:
                discord_message = await channel.fetch_message(discord_message_id)
            except discord.NotFound:
                # Already gone on Discord's side (deleted manually, channel
                # purged, etc.) - that's a success from the dashboard's
                # point of view, nothing left to do.
                self.bot.db.mark_message_deleted(message_id)
                self._record_event(
                    "dashboard.talk.deleted", guild_id, channel_id,
                    f"message_id={message_id} discord_message_id={discord_message_id} already_gone=true"
                )
                continue
            except discord.Forbidden:
                error = "The bot cannot view that channel anymore."
                self.bot.db.mark_message_delete_failed(message_id, error)
                self._record_event(
                    "dashboard.talk.delete_failed", guild_id, channel_id,
                    f"message_id={message_id} error={error}", status="failed"
                )
                continue
            except discord.HTTPException as exc:
                error = f"Discord HTTP {getattr(exc, 'status', 'error')}: {exc}"
                self.bot.db.mark_message_delete_failed(message_id, error)
                self._record_event(
                    "dashboard.talk.delete_failed", guild_id, channel_id,
                    f"message_id={message_id} error={error}", status="failed"
                )
                continue

            try:
                await discord_message.delete()
            except discord.NotFound:
                pass  # Deleted between the fetch and the delete - fine either way.
            except discord.Forbidden:
                error = "Discord denied deleting that message (check Manage Messages permission)."
                self.bot.db.mark_message_delete_failed(message_id, error)
                logger.warning("dashboard talk: delete %s forbidden in channel %s", message_id, channel_id)
                self._record_event(
                    "dashboard.talk.delete_failed", guild_id, channel_id,
                    f"message_id={message_id} error={error}", status="failed"
                )
                continue
            except discord.HTTPException as exc:
                error = f"Discord HTTP {getattr(exc, 'status', 'error')}: {exc}"
                self.bot.db.mark_message_delete_failed(message_id, error)
                logger.warning("dashboard talk: delete %s HTTP failure: %s", message_id, error)
                self._record_event(
                    "dashboard.talk.delete_failed", guild_id, channel_id,
                    f"message_id={message_id} error={error}", status="failed"
                )
                continue

            self.bot.db.mark_message_deleted(message_id)
            logger.info("dashboard talk: message %s (discord %s) deleted from channel %s", message_id, discord_message_id, channel_id)
            self._record_event(
                "dashboard.talk.deleted", guild_id, channel_id,
                f"message_id={message_id} discord_message_id={discord_message_id}"
            )

    @poll_outbound_messages.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    @poll_delete_requests.before_loop
    async def before_poll_delete(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(DashboardTalk(bot))
