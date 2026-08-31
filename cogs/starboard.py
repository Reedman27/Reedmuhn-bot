import discord
from discord.ext import commands
from discord import app_commands
from utils import manager_or_permission

STAR = "⭐"

class Starboard(commands.Cog, name="Starboard"):
    def __init__(self, bot): self.bot=bot

    @app_commands.command(name="setstarboard", description="Configure the starboard channel and reaction threshold")
    @app_commands.describe(channel="Where starred messages are posted", threshold="Stars required (1-50)")
    @manager_or_permission("manage_guild")
    async def setstarboard(self, interaction: discord.Interaction, channel: discord.TextChannel, threshold: app_commands.Range[int,1,50]=5):
        self.bot.db.set_starboard_config(interaction.guild.id, channel.id, threshold, True)
        await interaction.response.send_message(f"⭐ Starboard enabled in {channel.mention} at **{threshold}** stars.")

    @app_commands.command(name="starboardoff", description="Disable the starboard")
    @manager_or_permission("manage_guild")
    async def starboardoff(self, interaction):
        ch, threshold, _ = self.bot.db.get_starboard_config(interaction.guild.id)
        self.bot.db.set_starboard_config(interaction.guild.id, ch, threshold, False)
        await interaction.response.send_message("⭐ Starboard is now disabled.")

    @app_commands.command(name="starboardstatus", description="Show the current starboard configuration")
    @manager_or_permission("manage_guild")
    async def starboardstatus(self, interaction):
        ch, threshold, enabled = self.bot.db.get_starboard_config(interaction.guild.id)
        await interaction.response.send_message(f"⭐ **Starboard:** {'on' if enabled else 'off'}\n**Channel:** {f'<#{ch}>' if ch else 'not set'}\n**Threshold:** {threshold}")

    async def _update(self, payload):
        guild=self.bot.get_guild(payload.guild_id)
        if guild is None: return
        channel_id, threshold, enabled=self.bot.db.get_starboard_config(guild.id)
        if not enabled or not channel_id or payload.emoji.name != STAR: return
        if payload.channel_id == channel_id: return
        try:
            source=guild.get_channel(payload.channel_id)
            if source is None: source=await self.bot.fetch_channel(payload.channel_id)
            message=await source.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException): return
        if message.author.bot: return
        reaction=next((r for r in message.reactions if str(r.emoji)==STAR), None)
        count=reaction.count if reaction else 0
        existing=self.bot.db.get_starboard_message(guild.id,message.id)
        target=guild.get_channel(channel_id)
        if target is None: return
        if count < threshold:
            if existing:
                try:
                    old=await target.fetch_message(existing[0]); await old.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException): pass
                self.bot.db.delete_starboard_message(guild.id,message.id)
            return
        embed=discord.Embed(description=message.content[:3900] if message.content else "*(no text)*", color=discord.Color.gold(), timestamp=message.created_at)
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.add_field(name="Jump to message", value=f"[Open in Discord]({message.jump_url})", inline=False)
        if message.attachments:
            first=message.attachments[0]
            if first.content_type and first.content_type.startswith("image/"): embed.set_image(url=first.url)
            elif not message.content: embed.description=f"[Attachment]({first.url})"
        content=f"⭐ **{count}** • <#{message.channel.id}>"
        try:
            if existing:
                old=await target.fetch_message(existing[0]); await old.edit(content=content,embed=embed)
                self.bot.db.upsert_starboard_message(guild.id,message.id,old.id,target.id,count)
            else:
                posted=await target.send(content=content,embed=embed,allowed_mentions=discord.AllowedMentions.none())
                self.bot.db.upsert_starboard_message(guild.id,message.id,posted.id,target.id,count)
        except discord.HTTPException: return

    @commands.Cog.listener()
    async def on_raw_reaction_add(self,payload): await self._update(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self,payload): await self._update(payload)

async def setup(bot): await bot.add_cog(Starboard(bot))
