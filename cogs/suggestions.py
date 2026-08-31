import discord
from discord import app_commands
from discord.ext import commands
from utils import manager_or_permission

class SuggestionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="suggestion:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, "approved")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, custom_id="suggestion:deny")
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._decide(interaction, "denied")

    async def _decide(self, interaction, status):
        if interaction.guild is None: return
        cfg=interaction.client.db.get_suggestion_config(interaction.guild.id)
        staff_role=cfg[2]
        member=interaction.user
        allowed=member.guild_permissions.manage_guild or member.guild_permissions.administrator or (staff_role and any(r.id==staff_role for r in member.roles))
        if not allowed:
            await interaction.response.send_message("You don't have permission to review suggestions.",ephemeral=True); return
        row=interaction.client.db.conn.execute("SELECT id,content,author_id FROM suggestions WHERE guild_id=? AND message_id=?",(interaction.guild.id,interaction.message.id)).fetchone()
        if not row:
            await interaction.response.send_message("That suggestion no longer exists.",ephemeral=True); return
        sid,content,author_id=row
        interaction.client.db.set_suggestion_status(sid,status,member.id)
        embed=interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(description=content)
        embed.set_field_at(0,name="Status",value=status.title(),inline=True) if embed.fields else embed.add_field(name="Status",value=status.title(),inline=True)
        embed.color=discord.Color.green() if status=="approved" else discord.Color.red()
        for child in self.children: child.disabled=True
        await interaction.response.edit_message(embed=embed,view=self)

class Suggestions(commands.Cog, name="Suggestions"):
    def __init__(self,bot): self.bot=bot
    async def cog_load(self): self.bot.add_view(SuggestionView())

    @app_commands.command(name="suggest",description="Submit a suggestion for the server")
    @app_commands.describe(idea="What should the server add or change?")
    async def suggest(self,interaction,idea:str):
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        cfg=self.bot.db.get_suggestion_config(interaction.guild.id)
        channel_id,enabled,_=cfg
        if not enabled or not channel_id:
            await interaction.response.send_message("Suggestions aren't enabled in this server.",ephemeral=True); return
        channel=interaction.guild.get_channel(channel_id)
        if channel is None:
            await interaction.response.send_message("The suggestion channel no longer exists. Ask a moderator to reconfigure it.",ephemeral=True); return
        if len(idea)>2000: idea=idea[:2000]
        await interaction.response.defer(ephemeral=True)
        embed=discord.Embed(title="💡 New Suggestion",description=idea,color=discord.Color.blurple(),timestamp=discord.utils.utcnow())
        embed.set_author(name=interaction.user.display_name,icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Status",value="Pending",inline=True)
        embed.add_field(name="Suggestion ID",value="pending",inline=True)
        message=await channel.send(embed=embed,view=SuggestionView(),allowed_mentions=discord.AllowedMentions.none())
        sid=self.bot.db.create_suggestion(interaction.guild.id,message.id,interaction.user.id,idea)
        embed.set_field_at(1,name="Suggestion ID",value=f"#{sid}",inline=True)
        await message.edit(embed=embed)
        await interaction.followup.send(f"Your suggestion **#{sid}** was submitted in {channel.mention}.",ephemeral=True)

    @app_commands.command(name="suggestionstatus",description="Show a suggestion")
    @app_commands.describe(suggestion_id="Suggestion number")
    async def suggestionstatus(self,interaction,suggestion_id:int):
        if interaction.guild is None:
            await interaction.response.send_message("This command only works in a server.", ephemeral=True)
            return
        row=self.bot.db.get_suggestion(suggestion_id)
        if not row or row[1]!=interaction.guild.id:
            await interaction.response.send_message("Suggestion not found.",ephemeral=True); return
        _,_,message_id,author_id,content,status,staff_id,reason,created,updated=row
        embed=discord.Embed(title=f"💡 Suggestion #{suggestion_id}",description=content,color=discord.Color.green() if status=="approved" else discord.Color.red() if status=="denied" else discord.Color.blurple())
        embed.add_field(name="Status",value=status.title()); embed.add_field(name="Submitted by",value=f"<@{author_id}>")
        if reason: embed.add_field(name="Staff note",value=reason[:1024],inline=False)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="suggestions",description="List recent server suggestions")
    @manager_or_permission("manage_guild")
    async def suggestions(self,interaction):
        rows=self.bot.db.list_suggestions(interaction.guild.id,10)
        if not rows: await interaction.response.send_message("No suggestions yet."); return
        lines=[f"**#{r[0]}** — {r[4].title()} — {r[3][:90]}" for r in rows]
        await interaction.response.send_message(
            "\n".join(lines), allowed_mentions=discord.AllowedMentions.none()
        )

async def setup(bot): await bot.add_cog(Suggestions(bot))
