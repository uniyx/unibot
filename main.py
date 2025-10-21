import logging
import os
from typing import Optional, Iterable

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DEV_GUILD_ID_ENV = os.getenv("DEV_GUILD_ID", "").strip()
DEV_GUILD_ID: Optional[int] = int(DEV_GUILD_ID_ENV) if DEV_GUILD_ID_ENV else None

if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in .env")
if not DEV_GUILD_ID:
    raise RuntimeError("Set DEV_GUILD_ID in .env to use guild-scoped commands")

intents = discord.Intents.default()  # slash only

def slash_only_prefix(_bot: commands.Bot, _msg: discord.Message) -> Iterable[str]:
    # Must return a string or iterable, never None. Empty iterable disables text commands.
    return []

class UniBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=slash_only_prefix, intents=intents)
        self.initial_extensions = [
            "cogs.basic",
            "cogs.uptime",
            "cogs.tweets",
            "cogs.portfolio",
            "cogs.faceit",
        ]

    # Belt and suspenders. Even if something calls process_commands, this never returns None.
    async def get_prefix(self, _message: discord.Message):
        return []

    # Slash only. Do not process message commands.
    async def on_message(self, _message: discord.Message) -> None:
        return

    async def setup_hook(self) -> None:
        # Load cogs with error reporting
        for ext in self.initial_extensions:
            try:
                await self.load_extension(ext)
                logging.info("Loaded extension: %s", ext)
            except Exception:
                logging.exception("Failed to load extension: %s", ext)

        # Guild-scoped sync for instant availability
        guild = discord.Object(id=DEV_GUILD_ID)
        cmds = await self.tree.sync(guild=guild)
        logging.info("Synced %d commands to guild %s: %s",
                     len(cmds), DEV_GUILD_ID, [c.name for c in cmds])

bot = UniBot()

@bot.event
async def on_ready():
    logging.info("Logged in as %s (%s)", bot.user, bot.user.id)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    bot.run(TOKEN)
