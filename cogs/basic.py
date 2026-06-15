# cogs/basic.py
import random
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core.discord_utils import guilds_decorator


GUILDS = guilds_decorator()

IMG_BASE_URL = "https://files.uniyx.net/assets/img/"
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class ImageLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return

        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


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
    @app_commands.command(name="img", description="Send a random hosted image")
    async def img(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {"User-Agent": "uniyx-discord-bot/1.0"}
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(IMG_BASE_URL) as response:
                    response.raise_for_status()
                    directory_html = await response.text()
        except (aiohttp.ClientError, TimeoutError) as exc:
            await interaction.followup.send(
                f"Failed to load the image directory: {exc}", ephemeral=True
            )
            return

        parser = ImageLinkParser()
        parser.feed(directory_html)

        image_urls = []
        for href in parser.links:
            path = unquote(urlsplit(href).path)
            extension = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if f".{extension}" in ALLOWED_EXTENSIONS:
                image_urls.append(urljoin(IMG_BASE_URL, href))

        if not image_urls:
            await interaction.followup.send(
                "No valid images found in the hosted directory.", ephemeral=True
            )
            return

        await interaction.followup.send(random.choice(image_urls))

async def setup(bot: commands.Bot):
    await bot.add_cog(Basic(bot))
