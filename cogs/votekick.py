import logging
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import can_moderate, manager_or_permission

logger = logging.getLogger("votekick")


def render_votekick_embed(vote: dict, yes: int, no: int, required: int, closed: bool = False, result_text: str = "") -> discord.Embed:
    title = "🔒 Vote Kick Closed" if closed else "🗳️ Vote Kick"
    status = "Closed" if closed else f"Need **{required} yes votes** to kick"
    desc = (
        f"**Target:** <@{vote['target_id']}>\n"
        f"**Reason:** {vote['reason']}\n\n"
        f"✅ Yes: **{yes}**\n"
        f"❌ No: **{no}**\n\n"
        f"{status}"
    )
    if vote.get("expires_at") and not closed:
        remaining = max(0, vote["expires_at"] - int(time.time()))
        desc += f"\n⏱️ Expires in about **{remaining // 60}m {remaining % 60}s**."
    if result_text:
        desc += f"\n\n**Result:** {result_text}"
    return discord.Embed(
        title=title,
        description=desc,
        color=discord.Color.red() if not closed else discord.Color.light_grey(),
    )


class VoteKickView(discord.ui.View):
    def __init__(self, cog: "VoteKick", vote_id: int, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.vote_id = vote_id
        for label, emoji, vote, style in (
            ("Kick", "✅", "yes", discord.ButtonStyle.danger),
            ("Do not kick", "❌", "no", discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=f"votekick:{vote_id}:{vote}",
                disabled=disabled,
            )
            button.callback = self._make_callback(vote)
            self.add_item(button)

    def _make_callback(self, vote: str):
        async def callback(interaction: discord.Interaction):
            await self.cog.handle_vote(interaction, self.vote_id, vote)

        return callback


class VoteKick(commands.Cog, name="VoteKick"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.expire_votes.start()

    def cog_unload(self):
        self.expire_votes.cancel()

    async def cog_load(self):
        try:
            for vote in self.bot.db.list_open_votekicks():
                if vote["message_id"]:
                    self.bot.add_view(VoteKickView(self, vote["id"]), message_id=vote["message_id"])
        except Exception:
            logger.exception("failed to re-register open vote-kick views on startup")

    votekick_group = app_commands.Group(name="votekick", description="Community vote-kick")

    @votekick_group.command(name="start", description="Start a community vote to kick a member")
    @app_commands.describe(user="Member the server may vote to kick", reason="Why you want the member kicked")
    async def votekick(self, interaction: discord.Interaction, user: discord.Member, reason: str = "No reason given"):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        cfg = self.bot.db.get_votekick_config(interaction.guild.id)
        if not cfg["enabled"]:
            await interaction.response.send_message("Vote Kick is disabled in this server.", ephemeral=True)
            return
        if user.bot or user.id == interaction.user.id:
            await interaction.response.send_message("You can't start a vote kick against yourself or a bot.", ephemeral=True)
            return
        if interaction.guild.me and user.top_role >= interaction.guild.me.top_role:
            await interaction.response.send_message("I can't kick that member because their role is at or above mine.", ephemeral=True)
            return
        if not can_moderate(interaction.user, user):
            await interaction.response.send_message(
                "You can't start a vote against someone with an equal or higher role than you.", ephemeral=True
            )
            return
        now = int(time.time())
        vote_id = self.bot.db.create_votekick(
            interaction.guild.id,
            interaction.channel_id,
            interaction.user.id,
            user.id,
            reason[:500],
            now,
            now + cfg["duration_seconds"],
        )
        if vote_id is None:
            await interaction.response.send_message("There is already an active vote kick for that member.", ephemeral=True)
            return

        vote = self.bot.db.get_votekick(vote_id)
        embed = render_votekick_embed(vote, 0, 0, cfg["required_votes"])
        view = VoteKickView(self, vote_id)
        await interaction.response.send_message(embed=embed, view=view)
        message = await interaction.original_response()
        self.bot.db.set_votekick_message_id(vote_id, message.id)

    @votekick_group.command(name="toggle", description="Enable or disable Vote Kick for this server")
    @app_commands.describe(enabled="Whether community vote kicks are allowed")
    @manager_or_permission("manage_guild")
    async def votekicktoggle(self, interaction: discord.Interaction, enabled: bool):
        if interaction.guild is None:
            await interaction.response.send_message("This only works in a server.", ephemeral=True)
            return
        self.bot.db.set_votekick_config(interaction.guild.id, enabled)
        await interaction.response.send_message(f"Vote Kick is now {'enabled' if enabled else 'disabled'}.")

    async def handle_vote(self, interaction: discord.Interaction, vote_id: int, choice: str):
        vote = self.bot.db.get_votekick(vote_id)
        if vote is None or vote["status"] != "open":
            await interaction.response.send_message("That vote is closed or no longer exists.", ephemeral=True)
            return
        if interaction.guild is None or interaction.guild.id != vote["guild_id"]:
            await interaction.response.send_message("That vote belongs to another server.", ephemeral=True)
            return
        if interaction.user.bot or interaction.user.id == vote["target_id"]:
            await interaction.response.send_message("You cannot vote in this vote kick.", ephemeral=True)
            return
        if int(time.time()) >= vote["expires_at"]:
            await self._resolve_vote(vote, "expired")
            await interaction.response.send_message("That vote has expired.", ephemeral=True)
            return

        ok, yes, no = self.bot.db.cast_votekick_vote(vote_id, interaction.user.id, choice)
        if not ok:
            await interaction.response.send_message("That vote is already closed.", ephemeral=True)
            return

        cfg = self.bot.db.get_votekick_config(vote["guild_id"])
        if yes >= cfg["required_votes"]:
            await self._resolve_vote(vote, "passed")
            await interaction.response.send_message(
                f"Vote recorded. The vote passed with {yes} yes vote(s).", ephemeral=True
            )
            return

        await interaction.response.edit_message(
            embed=render_votekick_embed(vote, yes, no, cfg["required_votes"]),
            view=VoteKickView(self, vote_id),
        )

    async def _resolve_vote(self, vote: dict, result: str):
        if not self.bot.db.close_votekick(vote["id"], result):
            return
        guild = self.bot.get_guild(vote["guild_id"])
        if guild is None:
            return
        yes = self.bot.db.count_votekick_votes(vote["id"], "yes")
        no = self.bot.db.count_votekick_votes(vote["id"], "no")
        cfg = self.bot.db.get_votekick_config(vote["guild_id"])
        result_text = ""

        if result == "passed":
            member = guild.get_member(vote["target_id"])
            error = None
            if member is None:
                error = "member is no longer in the server"
            elif guild.me and member.top_role >= guild.me.top_role:
                error = "member's role is at or above the bot's role"
            else:
                try:
                    await member.kick(reason=f"Vote Kick: {vote['reason']}")
                except discord.Forbidden:
                    error = "bot lacks Kick Members permission or role hierarchy"
                except discord.HTTPException as exc:
                    error = f"Discord rejected the kick: {exc}"
            if error is None:
                self.bot.db.record_member_history(
                    guild.id,
                    vote["target_id"],
                    "vote_kick",
                    vote["initiator_id"],
                    f"{vote['reason']} (Vote Kick: {yes} yes / {no} no)",
                    is_case=True,
                )
                result_text = f"Vote passed and the member was kicked. {yes} yes / {no} no."
            else:
                result_text = f"Vote passed, but the kick failed: {error}. {yes} yes / {no} no."
        else:
            result_text = f"Vote expired without reaching the required yes votes. {yes} yes / {no} no."

        channel = guild.get_channel(vote["channel_id"])
        if channel is not None and vote["message_id"]:
            try:
                message = await channel.fetch_message(vote["message_id"])
                closed_vote = dict(vote)
                closed_vote["expires_at"] = int(time.time())
                await message.edit(
                    embed=render_votekick_embed(
                        closed_vote,
                        yes,
                        no,
                        cfg["required_votes"],
                        closed=True,
                        result_text=result_text,
                    ),
                    view=VoteKickView(self, vote["id"], disabled=True),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        logging_cog = self.bot.get_cog("Logging")
        if logging_cog is not None:
            embed = discord.Embed(
                description=f"**Vote Kick {result}** - <@{vote['target_id']}>",
                color=discord.Color.red() if result == "passed" else discord.Color.orange(),
                timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="Reason", value=vote["reason"], inline=False)
            embed.add_field(name="Result", value=result_text, inline=False)
            await logging_cog.log_event(guild, "moderation", embed)

    @tasks.loop(seconds=5)
    async def expire_votes(self):
        now = int(time.time())
        for vote in self.bot.db.list_open_votekicks():
            if now >= vote["expires_at"]:
                await self._resolve_vote(vote, "expired")

    @expire_votes.before_loop
    async def before_expire_votes(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(VoteKick(bot))
