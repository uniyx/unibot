# cogs/clip.py

import random
import datetime as dt
from typing import Any, Dict, List, Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core.config import env_str
from core.discord_utils import guilds_decorator
from core.faceit_utils import FaceitApiError, fetch_json, resolve_player_id_async

# =========================
# CONFIG
# =========================

ALLSTAR_BASE = "https://www.faceit.com/api/allstar/v1/games/cs2"

FACEIT_API_KEY = env_str("FACEIT_API_KEY")


async def get_player_id(session: aiohttp.ClientSession, nickname: str) -> str:
    if not FACEIT_API_KEY:
        raise FaceitApiError("FACEIT_API_KEY is not configured")
    return await resolve_player_id_async(
        session,
        FACEIT_API_KEY,
        nickname,
        error_factory=FaceitApiError,
    )


def clip_mp4_from_thumbnail(clip: Dict[str, Any]) -> Optional[str]:
    """
    Convert Allstar thumbnail URL to mp4 URL.

    Expected structure:
    thumb: https://mediacdn.allstar.gg/<bucket>/thumbs/<id>_thumb.jpg
    mp4:   https://mediacdn.allstar.gg/<bucket>/clips/<id>.mp4
    """
    thumb = clip.get("thumbnail_url")
    clip_id = clip.get("id")
    if not thumb or not clip_id:
        return None

    if "/thumbs/" not in thumb:
        return None

    prefix, _ = thumb.split("/thumbs/", 1)
    return f"{prefix}/clips/{clip_id}.mp4"


def parse_iso_datetime(ts: Optional[str]) -> Optional[dt.datetime]:
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return dt.datetime.fromisoformat(ts)
    except Exception:
        return None


# =========================
# COG
# =========================

class Clip(commands.Cog):
    """
    /clip: fetch a random FACEIT Allstar highlight for a player.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _fetch_random_clip(
        self,
        session: aiohttp.ClientSession,
        player_id: str,
        limit: int = 50,
        sort: str = "latest",
    ) -> Dict[str, Any]:
        """
        Hit the Allstar clips endpoint and pick a random processed clip.

        sort: "latest" or "best" as supported by the Allstar API.
        """
        url = f"{ALLSTAR_BASE}/users/{player_id}/clips"
        params = {
            "sort": sort,
            "offset": 0,
            "limit": limit,
        }

        data = await fetch_json(session, url, params=params)
        clips: List[Dict[str, Any]] = data.get("clips") or []

        processed = [c for c in clips if c.get("status") == "CLIP_STATUS_PROCESSED"]
        candidates = processed or clips

        if not candidates:
            raise FaceitApiError("No clips available for this player")

        return random.choice(candidates)

    @guilds_decorator()
    @app_commands.command(
        name="clip",
        description="Get a random FACEIT Allstar highlight for a player",
    )
    @app_commands.describe(
        nickname="FACEIT nickname (for example: uni)",
        sort="Sort mode for selecting clips",
    )
    @app_commands.choices(
        sort=[
            app_commands.Choice(name="Latest", value="latest"),
            app_commands.Choice(name="Best", value="best"),
        ]
    )
    async def clip_command(
        self,
        interaction,
        nickname: str,
        sort: Optional[app_commands.Choice[str]] = None,
    ) -> None:
        """
        sort:
            Latest  -> Allstar sort=latest (default)
            Best    -> Allstar sort=best
        """
        await interaction.response.defer(thinking=True)

        try:
            sort_clean = sort.value if sort is not None else "latest"

            async with aiohttp.ClientSession() as session:
                player_id = await get_player_id(session, nickname)
                clip = await self._fetch_random_clip(
                    session,
                    player_id,
                    limit=50,
                    sort=sort_clean,
                )

            title = clip.get("title") or f"{nickname}'s highlight"
            mp4_url = clip_mp4_from_thumbnail(clip)

            created_at = parse_iso_datetime(clip.get("created_at"))
            if created_at:
                created_field = (
                    f"{discord.utils.format_dt(created_at, style='R')} "
                    f"({discord.utils.format_dt(created_at, style='f')})"
                )
            else:
                created_field = "Unknown"

            lines: List[str] = []
            lines.append(f"**{title}**")
            lines.append(f"Player: `{nickname}`")
            lines.append(f"Sort: `{sort_clean}`")
            lines.append(f"Created: {created_field}")

            if mp4_url:
                lines.append("")
                lines.append(mp4_url)
            else:
                lines.append("")
                lines.append("No direct video URL could be constructed for this clip.")

            content = "\n".join(lines)
            await interaction.followup.send(content)

        except FaceitApiError as e:
            await interaction.followup.send(
                f"Failed to fetch clip: {e}",
                ephemeral=True,
            )
        except Exception:
            await interaction.followup.send(
                "Unexpected error while fetching clip. Check logs on the bot side.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Clip(bot))
