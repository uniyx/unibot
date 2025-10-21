# cogs/uptime.py
import os
import time
import discord
from discord import app_commands
from discord.ext import commands

DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
if DEV_GUILD_ID:
    GUILDS = app_commands.guilds(discord.Object(id=DEV_GUILD_ID))
else:
    # Fallback: no-op decorator if env is missing (main.py already enforces it)
    GUILDS = lambda f: f

class Uptime(commands.Cog):
    """
    Minimal uptime: capture UNIX start time once, report on demand.
    Guild-scoped, so it shows instantly in your dev server.
    """
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.started_unix: int = int(time.time())

    @GUILDS
    @app_commands.command(name="uptime", description="Show how long the bot has been running")
    async def uptime(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Started <t:{self.started_unix}:R>")

    @GUILDS
    @app_commands.command(name="started", description="Show the exact start timestamp")
    async def started(self, interaction: discord.Interaction):
        msg = (
            f"Absolute: <t:{self.started_unix}:T>\n"
            f"Short:    <t:{self.started_unix}:f>\n"
            f"Relative: <t:{self.started_unix}:R>"
        )
        await interaction.response.send_message(msg, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(Uptime(bot))
