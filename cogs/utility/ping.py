"""/ping - a quick health check across the three connections that matter:
how fast Discord acked our response (Roundtrip), the gateway heartbeat
(Gateway), and how fast a trivial query comes back from SQLite (Database).
"""
import time

import discord
from discord import app_commands
from discord.ext import commands


class Ping(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot's current latency")
    async def ping(self, interaction: discord.Interaction):
        # Roundtrip: how long Discord took to ack this interaction - the
        # deferral itself is the HTTP round trip we're timing, so it has to
        # happen before we compute anything else.
        start = time.perf_counter()
        await interaction.response.defer(thinking=True)
        roundtrip_ms = round((time.perf_counter() - start) * 1000)

        # Gateway: the websocket heartbeat latency discord.py already tracks.
        gateway_ms = round(self.bot.latency * 1000)

        # Database: time a trivial query against our own SQLite connection.
        db_start = time.perf_counter()
        try:
            self.bot.db.conn.execute("SELECT 1").fetchone()
            db_value = f"{round((time.perf_counter() - db_start) * 1000)}ms"
        except Exception:
            db_value = "unavailable"

        embed = discord.Embed(
            title="Ping",
            description="Here's a look at the current latency across each connection.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Roundtrip", value=f"{roundtrip_ms}ms", inline=False)
        embed.add_field(name="Gateway", value=f"{gateway_ms}ms", inline=False)
        embed.add_field(name="Database", value=db_value, inline=False)
        # Single self-hosted process, no sharding - shard_id is always 0,
        # but it's still worth showing so the embed matches what people
        # expect from a ping command and stays correct if that ever changes.
        embed.set_footer(text=f"Shard {getattr(self.bot, 'shard_id', None) or 0}")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Ping(bot))
