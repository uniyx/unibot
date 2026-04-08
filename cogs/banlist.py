import asyncio
import os
from typing import List, Tuple

import cloudscraper
import discord
import requests
from discord import app_commands
from discord.ext import commands


DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None


def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)


OPEN_FACEIT_BASE = "https://open.faceit.com/data/v4"
THEME_COLOR = 0xFF5500


def get_banlist() -> List[str]:
    raw = os.getenv("FACEIT_BANLIST", "").strip()
    return [nickname.strip() for nickname in raw.split(",") if nickname.strip()]


def resolve_player_id(nickname: str, api_key: str) -> Tuple[str, str]:
    response = requests.get(
        f"{OPEN_FACEIT_BASE}/players",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"nickname": nickname},
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    player_id = data.get("player_id")
    resolved_name = data.get("nickname") or nickname
    if not player_id:
        raise RuntimeError(f"Could not resolve player_id for '{nickname}'")
    return str(player_id), str(resolved_name)


def is_in_live_match(player_id: str) -> bool:
    scraper = cloudscraper.create_scraper()
    response = scraper.get(
        "https://www.faceit.com/api/match/v4/matches/groupByState",
        params={"userId": player_id},
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json().get("payload", {})
    return bool(payload.get("ONGOING"))


def check_player_status(nickname: str, api_key: str) -> Tuple[str, str, bool]:
    player_id, resolved_name = resolve_player_id(nickname, api_key)
    return nickname, resolved_name, is_in_live_match(player_id)


class Banlist(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @guilds_decorator()
    @app_commands.command(name="banlist", description="Check live FACEIT status for the configured banlist")
    async def banlist(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        api_key = os.getenv("FACEIT_API_KEY", "").strip()
        if not api_key:
            await interaction.followup.send(
                "FACEIT_API_KEY is not configured in `.env`.",
                ephemeral=True,
            )
            return

        nicknames = get_banlist()
        if not nicknames:
            await interaction.followup.send(
                "FACEIT_BANLIST is empty or missing in `.env`.",
                ephemeral=True,
            )
            return

        tasks = [
            asyncio.to_thread(check_player_status, nickname, api_key)
            for nickname in nicknames
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        live_lines: List[str] = []
        idle_lines: List[str] = []
        error_lines: List[str] = []

        for result in results:
            if isinstance(result, Exception):
                continue

            nickname, resolved_name, in_live_match = result
            line = f"`{resolved_name}`"
            if in_live_match:
                live_lines.append(line)
            else:
                idle_lines.append(line)

        embed = discord.Embed(
            title="FACEIT Banlist",
            color=THEME_COLOR,
        )
        embed.add_field(
            name=f"Live ({len(live_lines)})",
            value="\n".join(live_lines) if live_lines else "None",
            inline=False,
        )
        embed.add_field(
            name=f"Not Queueing ({len(idle_lines)})",
            value="\n".join(idle_lines) if idle_lines else "None",
            inline=False,
        )

        failures = 0
        for nickname, result in zip(nicknames, results):
            if isinstance(result, Exception):
                failures += 1
                error_lines.append(f"`{nickname}`: {result}")

        if error_lines:
            trimmed = "\n".join(error_lines[:8])
            if len(error_lines) > 8:
                trimmed += f"\n...and {len(error_lines) - 8} more"
            embed.add_field(
                name=f"Errors ({failures})",
                value=trimmed,
                inline=False,
            )

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Banlist(bot))
