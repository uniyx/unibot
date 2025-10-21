# cogs/basic.py
import os
import random
import discord
from discord import app_commands
from discord.ext import commands

DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
GUILDS = app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)

class Basic(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @GUILDS
    @app_commands.command(name="ping", description="Latency check")
    async def ping(self, interaction: discord.Interaction) -> None:
        ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"Pong {ms} ms")

    @GUILDS
    @app_commands.command(name="echo", description="Repeat text back to you")
    @app_commands.describe(text="What should I repeat")
    async def echo(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.send_message(text)

    @GUILDS
    @app_commands.command(name="roll", description="Roll NdM dice, e.g., 2d6")
    @app_commands.describe(spec="Format NdM, e.g., 1d20 or 3d6")
    async def roll(self, interaction: discord.Interaction, spec: str) -> None:
        try:
            n_str, m_str = spec.lower().split("d", 1)
            n = int(n_str)
            m = int(m_str)
            if not (1 <= n <= 100 and 2 <= m <= 1000):
                raise ValueError
        except Exception:
            await interaction.response.send_message(
                "Invalid format. Use NdM, like 2d6.", ephemeral=True
            )
            return
        rolls = [random.randint(1, m) for _ in range(n)]
        await interaction.response.send_message(f"{spec} → {rolls} = **{sum(rolls)}**")

async def setup(bot: commands.Bot):
    await bot.add_cog(Basic(bot))
