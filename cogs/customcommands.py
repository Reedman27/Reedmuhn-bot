import time

import discord
from discord import app_commands
from discord.ext import commands


from utils import manager_or_permission

class CustomCommands(commands.Cog):
    TRIGGER_COOLDOWN_SECONDS = 3

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory per (guild, user) cooldown so someone can't flood a
        # channel by rapidly repeating a trigger message. Not persisted -
        # losing this on restart just means a very brief cold start.
        self._trigger_cooldowns: dict = {}

    customcommand = app_commands.Group(name="customcommand", description="Manage per-server custom text commands")

    @customcommand.command(name="add", description="Add a custom command")
    @app_commands.describe(trigger="e.g. !rules", response="What the bot replies with")
    @manager_or_permission("manage_guild")
    async def addcommand(self, interaction: discord.Interaction, trigger: str, response: str):
        if interaction.guild is None:
            await interaction.response.send_message("Only works in a server.", ephemeral=True)
            return
        self.bot.db.add_custom_command(interaction.guild.id, trigger, response)
        await interaction.response.send_message(f"Added `{trigger}` -> `{response}`")

    @customcommand.command(name="remove", description="Remove a custom command")
    @app_commands.describe(trigger="The trigger to remove")
    @manager_or_permission("manage_guild")
    async def removecommand(self, interaction: discord.Interaction, trigger: str):
        if interaction.guild is None:
            await interaction.response.send_message("Only works in a server.", ephemeral=True)
            return
        removed = self.bot.db.remove_custom_command(interaction.guild.id, trigger)
        msg = f"Removed `{trigger}`" if removed else f"No custom command called `{trigger}`"
        await interaction.response.send_message(msg)

    @customcommand.command(name="list", description="List this server's custom commands")
    async def listcommands(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("Only works in a server.", ephemeral=True)
            return
        rows = self.bot.db.list_custom_commands(interaction.guild.id)
        if not rows:
            content = "No custom commands set up yet. Add one with `/customcommand add`."
        else:
            content = "\n".join(f"`{trigger}` -> {response}" for trigger, response in rows)
        await interaction.response.send_message(content)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        response = self.bot.db.lookup_custom_command(message.guild.id, message.content)
        if not response:
            return

        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        if now - self._trigger_cooldowns.get(key, 0) < self.TRIGGER_COOLDOWN_SECONDS:
            return  # cooldown active - ignore silently rather than reply with an error, to avoid adding to the spam
        self._trigger_cooldowns[key] = now

        await message.channel.send(
            response,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.bot.db.record_bot_event(
            "command.completed",
            message.guild.id,
            message.author.id,
            message.id,
            {"command": message.content.split(maxsplit=1)[0], "kind": "custom"},
            source="custom_command",
            status="success",
            correlation_id=f"msg_{message.id}",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomCommands(bot))
