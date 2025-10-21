import asyncio
import logging
import os
import socket
from typing import Optional, Iterable, Dict

import discord
from discord.ext import commands
from dotenv import load_dotenv
from aiohttp import web

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DEV_GUILD_ID_ENV = os.getenv("DEV_GUILD_ID", "").strip()
DEV_GUILD_ID: Optional[int] = int(DEV_GUILD_ID_ENV) if DEV_GUILD_ID_ENV else None

if not TOKEN:
    raise RuntimeError("Set DISCORD_TOKEN in .env")
if not DEV_GUILD_ID:
    raise RuntimeError("Set DEV_GUILD_ID in .env to use guild-scoped commands")

HEALTH_PORT = int(os.getenv("HEALTH_PORT", "6969"))
INSTANCE_TAG = os.getenv("INSTANCE_TAG", "vm")

intents = discord.Intents.default()  # slash only

def slash_only_prefix(_bot: commands.Bot, _msg: discord.Message) -> Iterable[str]:
    # Must return a string or iterable, never None. Empty iterable disables text commands.
    return []

# Shared health state mutated by lifecycle events
_health: Dict[str, object] = {
    "status": "starting",
    "cogs_loaded": 0,
    "latency_ms": None,
    "hostname": socket.gethostname(),
    "instance": INSTANCE_TAG,
}

async def _make_health_app():
    app = web.Application()

    async def health(_req):
        ok = (
            _health.get("status") == "ready"
            and isinstance(_health.get("latency_ms"), (int, float))
        )
        code = 200 if ok else 503
        return web.json_response(_health, status=code)

    async def ready(_req):
        code = 200 if _health.get("status") == "ready" else 503
        return web.json_response(_health, status=code)

    app.router.add_get("/health", health)
    app.router.add_get("/ready", ready)
    return app

async def start_health_server(host: str = "0.0.0.0", port: int = HEALTH_PORT):
    app = await _make_health_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    logging.info("Health server listening on %s:%d", host, port)

class UniBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix=slash_only_prefix, intents=intents)
        self.initial_extensions = [
            "cogs.basic",
            "cogs.tweets",
            "cogs.portfolio",
            "cogs.faceit",
            "cogs.status",
            "cogs.lastfm",
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

        # Start health HTTP server
        asyncio.create_task(start_health_server())

bot = UniBot()

@bot.event
async def on_ready():
    # Update health gates
    _health["status"] = "connected"
    _health["cogs_loaded"] = len(bot.cogs)
    _health["latency_ms"] = int(bot.latency * 1000)
    _health["status"] = "ready"

    # Visible instance tag in presence
    try:
        await bot.change_presence(activity=discord.Game(name=f"unibot [{INSTANCE_TAG}]"))
    except Exception:
        logging.exception("Failed to set presence")

    logging.info("Logged in as %s (%s) | latency=%sms | cogs=%d",
                 bot.user, bot.user.id, _health["latency_ms"], _health["cogs_loaded"])

@bot.event
async def on_resumed():
    _health["status"] = "ready"

@bot.event
async def on_disconnect():
    _health["status"] = "disconnected"

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    bot.run(TOKEN)
