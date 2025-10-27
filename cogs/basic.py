# cogs/basic.py
import os
import random
import discord
from discord import app_commands
from discord.ext import commands
from typing import List

DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
GUILDS = app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)

# Directory containing images
BASE_DIR = "/unibot"
IMG_DIR = os.path.join(BASE_DIR, "img")
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

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

    @GUILDS
    @app_commands.command(name="img", description="Send a random image from the shared drive")
    async def img(self, interaction: discord.Interaction) -> None:
        # Immediately acknowledge so Discord doesn't think we stalled
        await interaction.response.defer()

        # 1. Verify directory exists
        if not os.path.isdir(IMG_DIR):
            await interaction.followup.send(
                f"Image directory not found: {IMG_DIR}", ephemeral=True
            )
            return

        # 2. Collect all allowed image files
        all_files: List[str] = []
        for entry in os.listdir(IMG_DIR):
            full_path = os.path.join(IMG_DIR, entry)
            # skip subdirs and weird stuff
            if not os.path.isfile(full_path):
                continue
            _, ext = os.path.splitext(entry)
            if ext.lower() in ALLOWED_EXTENSIONS:
                all_files.append(full_path)

        if not all_files:
            await interaction.followup.send(
                "No valid images found in the directory.", ephemeral=True
            )
            return

        # 3. Pick a random file
        chosen_path = random.choice(all_files)

        # 4. Send it as an attachment
        try:
            file = discord.File(chosen_path)
            filename_only = os.path.basename(chosen_path)
            await interaction.followup.send(
                content=f"Random image: {filename_only}",
                file=file,
            )
        except Exception as e:
            # Failsafe in case of perms / locking / etc.
            await interaction.followup.send(
                f"Failed to send image: {e}", ephemeral=True
            )

async def setup(bot: commands.Bot):
    await bot.add_cog(Basic(bot))
