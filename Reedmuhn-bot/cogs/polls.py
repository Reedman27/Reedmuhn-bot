"""Button-based polls with live results. Each poll gets its own dynamic
view (one button per option, custom_id encodes the poll id and option
index) - unlike the codebase's other persistent views, this one can't be a
single static view registered once, since the buttons differ per poll. So
on every startup this cog re-registers a fresh view bound to each still-open
poll's message (bot.add_view(view, message_id=...)), which is discord.py's
supported way to make a per-message dynamic view survive a restart.
"""
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import scheduler

logger = logging.getLogger("polls")

MAX_OPTIONS = 5
MIN_OPTIONS = 2
OPTION_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
BAR_LENGTH = 12


def _build_poll_lines(options, counts, voters, per_option):
    total = sum(counts)
    lines = []
    for i, option in enumerate(options):
        count = counts[i] if i < len(counts) else 0
        pct = (count / total * 100) if total else 0
        filled = round(BAR_LENGTH * pct / 100)
        bar = "█" * filled + "░" * (BAR_LENGTH - filled)
        entry = f"{OPTION_EMOJI[i]} **{option}**\n`{bar}` {pct:.0f}% ({count})"
        if voters is not None and per_option > 0:
            ids = voters.get(i, [])
            if ids:
                shown = ", ".join(f"<@{user_id}>" for user_id in ids[:per_option])
                if len(ids) > per_option:
                    shown += f" and {len(ids) - per_option} more"
                entry += f"\nVoted: {shown}"
            else:
                entry += "\nVoted: —"
        lines.append(entry)
    return lines


def render_results_embed(question: str, options: list[str], counts: list[int], closed: bool, voters: dict[int, list[int]] | None = None) -> discord.Embed:
    total = sum(counts)
    # Discord caps embed descriptions at 4096 characters; shrink the voter
    # lists (never the results) until the embed fits.
    for per_option in (10, 5, 2, 0):
        description = "\n\n".join(_build_poll_lines(options, counts, voters, per_option))
        if len(description) <= 4000:
            break
    embed = discord.Embed(
        title=("🔒 " if closed else "📊 ") + question,
        description=description,
        color=discord.Color.light_gray() if closed else discord.Color.blurple(),
    )
    embed.set_footer(text=f"{total} vote{'s' if total != 1 else ''}" + (" - poll closed" if closed else ""))
    return embed


class PollView(discord.ui.View):
    def __init__(self, bot: commands.Bot, poll_id: int, options: list[str]):
        super().__init__(timeout=None)
        self.bot = bot
        self.poll_id = poll_id
        for i, option in enumerate(options[:MAX_OPTIONS]):
            label = option if len(option) <= 80 else option[:79] + "…"
            button = discord.ui.Button(
                label=label, style=discord.ButtonStyle.secondary,
                emoji=OPTION_EMOJI[i], custom_id=f"polls:vote:{poll_id}:{i}",
            )
            button.callback = self._make_callback(i)
            self.add_item(button)

    def _make_callback(self, option_index: int):
        async def callback(interaction: discord.Interaction):
            await self._vote(interaction, option_index)
        return callback

    async def _vote(self, interaction: discord.Interaction, option_index: int) -> None:
        poll = self.bot.db.get_poll(self.poll_id)
        if poll is None:
            await interaction.response.send_message("This poll no longer exists.", ephemeral=True)
            return
        if poll["closed"]:
            await interaction.response.send_message("This poll is closed.", ephemeral=True)
            return
        if not self.bot.db.cast_poll_vote(self.poll_id, interaction.user.id, option_index):
            latest = self.bot.db.get_poll(self.poll_id)
            message = "This poll is closed." if latest and latest["closed"] else "This poll is no longer available."
            await interaction.response.send_message(message, ephemeral=True)
            return
        counts = self.bot.db.poll_results(self.poll_id, len(poll["options"]))
        voters = self.bot.db.poll_voters(self.poll_id, len(poll["options"]))
        embed = render_results_embed(poll["question"], poll["options"], counts, closed=False, voters=voters)
        await interaction.response.edit_message(embed=embed)
        try:
            await interaction.followup.send(f"Vote recorded for **{poll['options'][option_index]}**.", ephemeral=True)
        except discord.HTTPException:
            pass


class Polls(commands.Cog, name="Polls"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll_close_requests.start()
        self.bot.register_webui_wake("poll_close", self.poll_close_requests)

    def cog_unload(self):
        self.poll_close_requests.cancel()

    async def cog_load(self):
        # Re-bind a fresh view to every still-open poll's message so voting
        # keeps working after a restart (see module docstring).
        try:
            for poll in self.bot.db.list_open_polls():
                view = PollView(self.bot, poll["id"], poll["options"])
                self.bot.add_view(view, message_id=poll["message_id"])
        except Exception:
            logger.exception("failed to re-register open poll views on startup")

    poll_group = app_commands.Group(name="poll", description="Button polls")

    @poll_group.command(name="start", description="Start a button poll (2-5 options)")
    @app_commands.describe(
        question="The poll question", option1="First option", option2="Second option",
        option3="Third option (optional)", option4="Fourth option (optional)", option5="Fifth option (optional)",
        duration_minutes="Auto-close after this many minutes (0 = stays open until manually closed)",
    )
    async def poll(
        self, interaction: discord.Interaction, question: str, option1: str, option2: str,
        option3: str = "", option4: str = "", option5: str = "", duration_minutes: int = 0,
    ):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        options = [o.strip() for o in (option1, option2, option3, option4, option5) if o.strip()]
        if len(options) < MIN_OPTIONS:
            await interaction.response.send_message("Give at least 2 options.", ephemeral=True)
            return
        if duration_minutes < 0:
            await interaction.response.send_message("Duration can't be negative.", ephemeral=True)
            return

        ends_at = int(time.time()) + duration_minutes * 60 if duration_minutes > 0 else None
        poll_id = self.bot.db.create_poll(interaction.guild.id, interaction.channel_id, question, options, interaction.user.id, ends_at)

        counts = [0] * len(options)
        embed = render_results_embed(question, options, counts, closed=False)
        view = PollView(self.bot, poll_id, options)
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()
        self.bot.db.set_poll_message_id(poll_id, message.id)

        if ends_at:
            scheduler.schedule_poll_close(self.bot.db, interaction.guild.id, ends_at, poll_id)

    @poll_group.command(name="close", description="Close a poll you started (or any poll, if you can manage the server)")
    @app_commands.describe(poll_id="The poll's ID (shown nowhere obvious yet - ask whoever ran /poll start, or use the dashboard)")
    async def closepoll(self, interaction: discord.Interaction, poll_id: int):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        poll = self.bot.db.get_poll(poll_id)
        if poll is None or poll["guild_id"] != interaction.guild.id:
            await interaction.response.send_message("No poll with that ID in this server.", ephemeral=True)
            return
        is_owner = interaction.user.id == poll["created_by"]
        is_manager = getattr(interaction.user.guild_permissions, "manage_guild", False) or getattr(interaction.user.guild_permissions, "administrator", False)
        if not (is_owner or is_manager):
            await interaction.response.send_message("Only the person who started this poll (or a server manager) can close it.", ephemeral=True)
            return
        error = await self._close_poll(interaction.guild, poll_id)
        if error:
            await interaction.response.send_message(
                f"The poll is closed, but its Discord message could not be updated: {error}",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message("Poll closed.", ephemeral=True)

    async def _close_poll(
        self, guild: discord.Guild, poll_id: int, *, propagate_http_errors: bool = False,
    ) -> str | None:
        poll = self.bot.db.get_poll(poll_id)
        if poll is None:
            return "That poll no longer exists."

        # Closing is idempotent, but updating the Discord message must still be
        # retried when the database was closed successfully and the API edit
        # failed. Returning early for an already-closed poll stranded enabled
        # vote buttons and a stale results embed forever.
        if not poll["closed"]:
            self.bot.db.close_poll(poll_id)

        counts = self.bot.db.poll_results(poll_id, len(poll["options"]))
        voters = self.bot.db.poll_voters(poll_id, len(poll["options"]))
        embed = render_results_embed(poll["question"], poll["options"], counts, closed=True, voters=voters)

        channel = guild.get_channel(poll["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(poll["channel_id"])
            except discord.NotFound:
                # A deleted channel leaves no message/buttons that need editing.
                logger.info("poll %s: channel %s no longer exists", poll_id, poll["channel_id"])
                return None
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.warning("poll %s: couldn't fetch channel %s: %s", poll_id, poll["channel_id"], exc)
                if propagate_http_errors:
                    raise
                return "Discord refused access to the poll channel."

        if not poll["message_id"]:
            # The command may have persisted the poll before Discord returned
            # the message ID. There is no message to edit in this state.
            logger.warning("poll %s is closed but has no Discord message ID", poll_id)
            return None

        try:
            message = await channel.fetch_message(poll["message_id"])
            await message.edit(embed=embed, view=None)
        except discord.NotFound:
            # Deleted poll messages don't need further attempts.
            logger.info("poll %s: original message no longer exists", poll_id)
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("poll %s: couldn't edit final results into its message: %s", poll_id, exc)
            if propagate_http_errors:
                raise
            return "Discord refused to update the poll message. Try closing it again after checking bot permissions."
        return None

    @tasks.loop(seconds=2)
    async def poll_close_requests(self):
        try:
            requests = self.bot.db.claim_poll_close_requests(limit=5)
        except Exception:
            logger.exception("failed to claim WebUI poll-close requests")
            return
        for request_id, guild_id, poll_id in requests:
            try:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    self.bot.db.complete_poll_close(request_id, "The bot is no longer in that server.")
                    continue
                error = await self._close_poll(guild, poll_id)
                self.bot.db.complete_poll_close(request_id, error)
            except Exception as exc:
                logger.exception("WebUI poll-close request %s failed", request_id)
                self.bot.db.complete_poll_close(request_id, str(exc)[:500])

    @poll_close_requests.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(Polls(bot))
