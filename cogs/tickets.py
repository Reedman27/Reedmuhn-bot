"""Support tickets. Two ways in: /ticket, or clicking the button on a
persistent panel message posted in one designated channel (/setticketpanel)
- the panel is the "ticket tool" replacement: members don't need to know
any slash command, they just go to that one channel and click the button.
Either way opens a private text channel for that member; /closeticket (or
the button on the ticket itself) locks it.

The WebUI mirrors ticket closing and panel posting, but since actually
touching Discord (creating/locking/renaming channels, posting/editing the
panel message) needs a live gateway connection the dashboard process
doesn't have, both queue a request here and this cog's pollers pick them
up (same queue/claim/complete pattern as dashboardmoderation.py).
"""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import manager_or_permission

logger = logging.getLogger("tickets")


def _channel_safe_name(name: str) -> str:
    slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    slug = "-".join(filter(None, slug.split("-")))
    return slug or "member"


class TicketCloseView(discord.ui.View):
    """Persistent 'Close Ticket' button posted in every ticket channel.
    Static custom_id - which ticket it belongs to is looked up from the
    channel it's clicked in, not encoded in the button itself."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.danger, custom_id="tickets:close_click", emoji="🔒")
    async def close_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog = self.bot.get_cog("Tickets")
        if cog is None:
            await interaction.response.send_message("Tickets aren't available right now.", ephemeral=True)
            return
        await cog.handle_close_interaction(interaction, reason="Closed via button")


class TicketSubjectModal(discord.ui.Modal, title="Open a Ticket"):
    """Asks for a one-line subject when someone opens a ticket from the
    panel button - the slash command gets this as a regular parameter, but
    a button click has no room for extra arguments without a modal."""

    subject = discord.ui.TextInput(label="What's this about?", required=False, max_length=200, placeholder="Optional, but helps staff triage faster")

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        cog = self.bot.get_cog("Tickets")
        if cog is None:
            await interaction.response.send_message("Tickets aren't available right now.", ephemeral=True)
            return
        await cog.open_ticket(interaction, str(self.subject))


class TicketPanelView(discord.ui.View):
    """Persistent 'Open a Ticket' button for the designated panel channel.
    Same static-custom_id approach as TicketCloseView - one view instance
    handles every guild's panel, config is looked up per-interaction."""

    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Open a Ticket", style=discord.ButtonStyle.success, custom_id="tickets:panel_open", emoji="🎫")
    async def open_click(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketSubjectModal(self.bot))


class Tickets(commands.Cog, name="Tickets"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_ticket_close_requests.start()
        self.poll_ticket_panel_requests.start()

    def cog_unload(self):
        self.poll_ticket_close_requests.cancel()
        self.poll_ticket_panel_requests.cancel()

    async def cog_load(self):
        self.bot.add_view(TicketCloseView(self.bot))
        self.bot.add_view(TicketPanelView(self.bot))

    @app_commands.command(name="setuptickets", description="Configure where ticket channels are created and who can see them")
    @app_commands.describe(category="Category tickets are created under", support_role="Role that can see and manage tickets")
    @manager_or_permission("manage_guild")
    async def setuptickets(self, interaction: discord.Interaction, category: discord.CategoryChannel, support_role: discord.Role):
        self.bot.db.set_ticket_config(interaction.guild.id, category.id, support_role.id)
        await interaction.response.send_message(
            f"Tickets will now open under **{category.name}**, visible to {support_role.mention} and whoever opened them."
        )

    @app_commands.command(name="setticketpanel", description="Post (or move/update) the 'Open a Ticket' button in a channel")
    @app_commands.describe(channel="Where to post the panel", title="Embed title", description="Embed body text")
    @manager_or_permission("manage_guild")
    async def setticketpanel(
        self, interaction: discord.Interaction, channel: discord.TextChannel,
        title: str = "Support", description: str = "Click the button below to open a private ticket with the support team.",
    ):
        self.bot.db.set_ticket_panel_config(interaction.guild.id, channel.id, title, description)
        await interaction.response.defer(ephemeral=True)
        error = await self._post_or_update_panel(interaction.guild.id)
        if error:
            await interaction.followup.send(f"Saved, but couldn't post the panel: {error}", ephemeral=True)
        else:
            await interaction.followup.send(f"Ticket panel is up in {channel.mention}.", ephemeral=True)

    @app_commands.command(name="ticketdeleteonclose", description="Choose whether closing a ticket deletes its channel")
    @app_commands.describe(
        enabled="If on, closing a ticket deletes the channel instead of just locking it",
        delay="Seconds to wait after closing before deleting (gives everyone a moment to see it closed)",
    )
    @manager_or_permission("manage_guild")
    async def ticketdeleteonclose(
        self, interaction: discord.Interaction, enabled: bool, delay: app_commands.Range[int, 3, 300] = 10
    ):
        self.bot.db.set_ticket_delete_on_close(interaction.guild.id, enabled, delay)
        if enabled:
            await interaction.response.send_message(
                f"Closing a ticket will now delete its channel **{delay}s** after it's closed."
            )
        else:
            await interaction.response.send_message(
                "Closing a ticket will now just lock and rename the channel, same as before - it won't be deleted."
            )

    @app_commands.command(name="ticket", description="Open a private support ticket")
    @app_commands.describe(subject="What's this about?")
    async def ticket(self, interaction: discord.Interaction, subject: str = ""):
        await self.open_ticket(interaction, subject)

    async def open_ticket(self, interaction: discord.Interaction, subject: str) -> None:
        """Shared by /ticket and the panel button's modal submission."""
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        cfg = self.bot.db.get_ticket_config(interaction.guild.id)
        if not cfg["category_id"]:
            await interaction.response.send_message(
                "Tickets aren't set up on this server yet - a mod needs to run /setuptickets first.", ephemeral=True
            )
            return
        category = interaction.guild.get_channel(cfg["category_id"])
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message("The configured ticket category no longer exists.", ephemeral=True)
            return
        support_role = interaction.guild.get_role(cfg["support_role_id"]) if cfg["support_role_id"] else None

        await interaction.response.defer(ephemeral=True)

        existing = self.bot.db.get_open_ticket_by_opener(interaction.guild.id, interaction.user.id)
        if existing is not None:
            _existing_id, existing_channel_id = existing
            existing_channel = interaction.guild.get_channel(existing_channel_id)
            if existing_channel is not None:
                await interaction.followup.send(f"You already have an open ticket: {existing_channel.mention}", ephemeral=True)
                return
            # The channel's gone (manually deleted outside the bot) but the
            # DB row never got closed - don't let a stale row block new
            # tickets forever; let this one through instead.

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        me = interaction.guild.me
        if me is not None:
            overwrites[me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)
        if support_role is not None:
            overwrites[support_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        # A placeholder name, immediately replaced below once the ticket's
        # real id is known - avoids the collision-prone "scan existing
        # channel names and pick a free suffix" approach, which had a race
        # if two people opened tickets in the same instant. A ticket id is
        # unique by construction (autoincrement), so renaming to include it
        # guarantees a unique channel name with no scanning needed.
        try:
            channel = await category.create_text_channel(
                name=f"ticket-{_channel_safe_name(interaction.user.name)}",
                overwrites=overwrites, reason=f"Ticket opened by {interaction.user}",
            )
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to create channels in that category.", ephemeral=True)
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(f"Discord rejected that: {exc}", ephemeral=True)
            return

        ticket_id = self.bot.db.create_ticket(interaction.guild.id, channel.id, interaction.user.id, subject)
        try:
            await channel.edit(name=f"ticket-{ticket_id}-{_channel_safe_name(interaction.user.name)}"[:100])
        except (discord.Forbidden, discord.HTTPException):
            pass  # cosmetic only - the ticket is already usable under its placeholder name

        embed = discord.Embed(
            title=f"Ticket #{ticket_id}",
            description=subject or "*(no subject given)*",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Opened by", value=interaction.user.mention, inline=True)
        mention_text = support_role.mention if support_role is not None else ""
        await channel.send(content=f"{interaction.user.mention} {mention_text}".strip(), embed=embed, view=TicketCloseView(self.bot))
        await interaction.followup.send(f"Opened {channel.mention}.", ephemeral=True)
        await self._log(interaction.guild, f"ticket #{ticket_id} opened", interaction.user.id, channel)

    @app_commands.command(name="closeticket", description="Close the ticket in this channel")
    @app_commands.describe(reason="Why this ticket is being closed")
    async def closeticket(self, interaction: discord.Interaction, reason: str = ""):
        await self.handle_close_interaction(interaction, reason=reason or "No reason given")

    async def handle_close_interaction(self, interaction: discord.Interaction, reason: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        row = self.bot.db.get_ticket_by_channel(interaction.channel_id)
        if row is None:
            await interaction.response.send_message("This channel isn't an open ticket.", ephemeral=True)
            return
        ticket_id, guild_id, channel_id, opener_id, subject, status = row

        cfg = self.bot.db.get_ticket_config(interaction.guild.id)
        is_opener = interaction.user.id == opener_id
        is_support = cfg["support_role_id"] and any(r.id == cfg["support_role_id"] for r in getattr(interaction.user, "roles", []))
        is_admin = getattr(interaction.user.guild_permissions, "administrator", False) or getattr(interaction.user.guild_permissions, "manage_guild", False)
        if not (is_opener or is_support or is_admin):
            await interaction.response.send_message("Only the person who opened this ticket or a support team member can close it.", ephemeral=True)
            return

        await interaction.response.defer()
        error = await self._close_ticket(interaction.guild, ticket_id, interaction.user.id, reason)
        if error:
            await interaction.followup.send(error)
        else:
            await interaction.followup.send(f"Ticket closed by {interaction.user.mention}. Reason: {reason}")

    async def _close_ticket(self, guild: discord.Guild, ticket_id: int, closed_by: int, reason: str) -> str | None:
        """Locks and renames the ticket channel and marks it closed in the
        db. Returns a message to show the user on failure/no-op, None on
        success. Shared by the in-Discord close path and the WebUI queue
        poller below.

        db.close_ticket() is an atomic UPDATE ... WHERE status='open', so
        if two close attempts land at nearly the same time (double-clicking
        the button, or /closeticket racing the button), only the first one
        actually closes it - the second sees rowcount 0 and stops here
        without touching the Discord channel a second time."""
        row = self.bot.db.get_ticket(ticket_id)
        if row is None:
            return "That ticket no longer exists."
        _id, ticket_guild_id, channel_id, opener_id, subject, status = row
        if status != "open":
            return None  # already closed elsewhere - not an error, just nothing to do

        closed = self.bot.db.close_ticket(ticket_id, closed_by, reason)
        if not closed:
            return None  # lost the race to another close attempt - the other one will finish the job

        channel = guild.get_channel(channel_id)
        cfg = self.bot.db.get_ticket_config(guild.id)
        if channel is not None and isinstance(channel, discord.TextChannel):
            if cfg["delete_on_close"]:
                delay = cfg["delete_delay_seconds"]
                try:
                    await channel.send(f"🔒 Ticket closed. This channel will be deleted in **{delay}s**.")
                except (discord.Forbidden, discord.HTTPException):
                    pass
                # Scheduled rather than awaited here so the close command/
                # WebUI poll returns immediately instead of blocking on the
                # delay - the actual deletion happens in the background.
                asyncio.create_task(self._delete_ticket_channel(channel, delay, ticket_id))
            else:
                try:
                    opener = guild.get_member(opener_id)
                    if opener is not None:
                        await channel.set_permissions(opener, view_channel=True, send_messages=False, reason="Ticket closed")
                    if not channel.name.startswith("closed-"):
                        await channel.edit(name=f"closed-{channel.name}"[:100], reason="Ticket closed")
                except (discord.Forbidden, discord.HTTPException):
                    logger.warning("ticket %s: couldn't fully lock/rename channel %s", ticket_id, channel_id)

        await self._log(guild, f"ticket #{ticket_id} closed", closed_by, channel, reason=reason)
        return None

    async def _delete_ticket_channel(self, channel: discord.TextChannel, delay: int, ticket_id: int) -> None:
        await asyncio.sleep(delay)
        try:
            await channel.delete(reason=f"Ticket #{ticket_id} closed (auto-delete)")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            logger.warning("ticket %s: couldn't delete channel %s after close", ticket_id, channel.id)

    async def _post_or_update_panel(self, guild_id: int) -> str | None:
        """Posts the ticket-panel embed+button, or edits the existing one
        in place if this guild already has one. Returns an error string on
        failure, None on success - mirrors verification.py's
        _post_or_update_message for the same reason (WebUI-triggered posts
        need this on a poll tick since the dashboard has no live connection)."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return "The bot is no longer in that server."
        cfg = self.bot.db.get_ticket_config(guild_id)
        if not cfg["panel_channel_id"]:
            return "Set a panel channel first."
        channel = guild.get_channel(cfg["panel_channel_id"])
        if channel is None or not isinstance(channel, discord.TextChannel):
            return "The configured panel channel no longer exists."
        me = guild.me
        if me is not None and not channel.permissions_for(me).send_messages:
            return "The bot can't send messages in that channel."

        embed = discord.Embed(title=cfg["panel_title"], description=cfg["panel_description"], color=discord.Color.blurple())
        view = TicketPanelView(self.bot)

        if cfg["panel_message_id"]:
            try:
                message = await channel.fetch_message(cfg["panel_message_id"])
                await message.edit(embed=embed, view=view)
                return None
            except (discord.NotFound, discord.Forbidden):
                pass  # original message is gone - fall through and post a new one

        try:
            message = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            return "The bot can't send messages in that channel."
        except discord.HTTPException as exc:
            return f"Discord rejected the message: {exc}"
        self.bot.db.set_ticket_panel_message_id(guild_id, message.id)
        return None

    async def _log(self, guild: discord.Guild, action: str, actor_id: int, channel, reason: str | None = None) -> None:
        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is None:
            return
        # actor_id 0 is the dashboard sentinel (see poll_ticket_close_requests
        # below) - there's no Discord user to @mention for it, so show the
        # same "Dashboard" label dashboardmoderation.py uses for the same case.
        actor_desc = "Dashboard" if actor_id == 0 else f"<@{actor_id}>"
        desc = f"**{action}** - {actor_desc}"
        if channel is not None:
            desc += f" ({channel.mention})"
        embed = discord.Embed(description=desc, color=discord.Color.orange(), timestamp=discord.utils.utcnow())
        if reason:
            embed.add_field(name="Reason", value=reason, inline=True)
        await logging_cog.log_event(guild, "tickets", embed)

    @tasks.loop(seconds=2)
    async def poll_ticket_close_requests(self):
        try:
            requests = self.bot.db.claim_ticket_close_requests(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI ticket-close requests")
            return
        for request_id, guild_id, ticket_id, reason in requests:
            try:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    self.bot.db.complete_ticket_close(request_id, "The bot is no longer in that server.")
                    continue
                # actor_id 0: dashboard-issued, same sentinel used elsewhere
                # in this codebase for actions with no per-admin login to
                # attribute them to (see dashboardmoderation.py's _history).
                error = await self._close_ticket(guild, ticket_id, 0, reason)
                self.bot.db.complete_ticket_close(request_id, error)
            except Exception as exc:
                logger.exception("WebUI ticket-close request %s failed", request_id)
                self.bot.db.complete_ticket_close(request_id, str(exc)[:500])

    @poll_ticket_close_requests.before_loop
    async def before_poll_close(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=2)
    async def poll_ticket_panel_requests(self):
        try:
            requests = self.bot.db.claim_ticket_panel_requests(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI ticket-panel requests")
            return
        for request_id, guild_id in requests:
            try:
                error = await self._post_or_update_panel(guild_id)
                self.bot.db.complete_ticket_panel_post(request_id, error)
            except Exception as exc:
                logger.exception("WebUI ticket-panel request %s failed", request_id)
                self.bot.db.complete_ticket_panel_post(request_id, str(exc)[:500])

    @poll_ticket_panel_requests.before_loop
    async def before_poll_panel(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
