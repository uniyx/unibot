# cogs/lastfm.py

import os
import io
import math
import asyncio
from typing import Dict, List, Optional, Tuple

import aiohttp
from PIL import Image, ImageDraw, ImageFont

import discord
from discord import app_commands
from discord.ext import commands

LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "").strip()
if not LASTFM_API_KEY:
    raise RuntimeError("Set LASTFM_API_KEY in environment to use the Last.fm cog")

DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
GUILDS = app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)

# Map Last.fm API periods
_PERIOD_MAP: Dict[str, str] = {
    "Last 7 days": "7day",
    "Last 30 days": "1month",
    "Last 90 days": "3month",
    "Last 180 days": "6month",
    "Last 365 days": "12month",
    "All time": "overall",
}

# Discord choices shown in the slash command. Keys must match _PERIOD_MAP.
_PERIOD_CHOICES = [
    app_commands.Choice(name="Last 7 days", value="Last 7 days"),
    app_commands.Choice(name="Last 30 days", value="Last 30 days"),
    app_commands.Choice(name="Last 90 days", value="Last 90 days"),
    app_commands.Choice(name="Last 180 days", value="Last 180 days"),
    app_commands.Choice(name="Last 365 days", value="Last 365 days"),
    app_commands.Choice(name="All time", value="All time"),
]

async def _fetch_json(session: aiohttp.ClientSession, url: str, params: Dict[str, str]) -> Dict:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
        if r.status != 200:
            text = await r.text()
            raise RuntimeError(f"HTTP {r.status} from Last.fm: {text[:200]}")
        return await r.json()

async def _get_top_albums(
    session: aiohttp.ClientSession,
    username: str,
    period_api_value: str,
    limit: int,
) -> List[Dict]:
    """
    Returns a list of album dicts from Last.fm user.getTopAlbums
    Each item includes 'name', 'artist': {'name': ...}, and 'image': [{'size': 'extralarge', '#text': '...'}, ...]
    """
    base = "https://ws.audioscrobbler.com/2.0/"
    params = {
        "method": "user.gettopalbums",
        "user": username,
        "period": period_api_value,
        "api_key": LASTFM_API_KEY,
        "format": "json",
        "limit": str(limit),
    }
    data = await _fetch_json(session, base, params)
    if "error" in data:
        raise RuntimeError(f"Last.fm API error {data.get('error')}: {data.get('message')}")
    top = data.get("topalbums", {}).get("album", [])
    # Last.fm sometimes returns a dict when only one item exists
    if isinstance(top, dict):
        top = [top]
    return top

def _pick_image_url(album: Dict) -> Optional[str]:
    """
    Choose the best available image URL from the album dict. Prefer 'extralarge', then 'large', then last non-empty.
    """
    images = album.get("image", []) or []
    by_size = {im.get("size"): im.get("#text") for im in images if im.get("#text")}
    for wanted in ("extralarge", "large", "medium"):
        if by_size.get(wanted):
            return by_size[wanted]
    # Fall back to any non-empty URL
    for im in images:
        if im.get("#text"):
            return im["#text"]
    return None

async def _fetch_image(session: aiohttp.ClientSession, url: str) -> Optional[Image.Image]:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
            if r.status != 200:
                return None
            content = await r.read()
        im = Image.open(io.BytesIO(content))
        # Convert to RGB to avoid mode headaches later
        return im.convert("RGB")
    except Exception:
        return None

def _placeholder_tile(size: int) -> Image.Image:
    """Neutral checker placeholder when art is missing or failed."""
    tile = Image.new("RGB", (size, size), (24, 24, 24))
    draw = ImageDraw.Draw(tile)
    step = max(4, size // 10)
    for y in range(0, size, step):
        for x in range(0, size, step):
            if (x // step + y // step) % 2 == 0:
                draw.rectangle([x, y, x + step - 1, y + step - 1], fill=(36, 36, 36))
    return tile

def _compose_grid(tiles: List[Image.Image], cols: int, cell: int) -> Image.Image:
    """
    Compose a grid from tiles. All tiles will be center-cropped to square and resized to cell x cell.
    """
    if cols < 1:
        cols = 1
    rows = math.ceil(len(tiles) / cols)
    canvas = Image.new("RGB", (cols * cell, rows * cell), (18, 18, 18))

    def square(im: Image.Image) -> Image.Image:
        w, h = im.size
        if w == h:
            return im.resize((cell, cell), Image.LANCZOS)
        # center crop to square
        if w > h:
            left = (w - h) // 2
            box = (left, 0, left + h, h)
        else:
            top = (h - w) // 2
            box = (0, top, w, top + w)
        return im.crop(box).resize((cell, cell), Image.LANCZOS)

    for idx, tile in enumerate(tiles):
        tile_sq = square(tile)
        r = idx // cols
        c = idx % cols
        canvas.paste(tile_sq, (c * cell, r * cell))
    return canvas

async def _make_album_grid(
    username: str,
    period_label: str,
    limit: int,
    cols: int,
    cell_px: int,
) -> Tuple[Image.Image, List[str]]:
    """
    Builds the grid image and returns it along with a list of album titles for the footer.
    """
    period = _PERIOD_MAP[period_label]
    async with aiohttp.ClientSession(headers={"User-Agent": "uniyx-lastfm-cog/1.0"}) as session:
        albums = await _get_top_albums(session, username, period, limit)
        if not albums:
            raise RuntimeError("No top albums returned. The user may have no scrobbles for that period.")

        # Choose cover URLs and fetch concurrently
        urls: List[Optional[str]] = [_pick_image_url(a) for a in albums]
        tasks = [(_fetch_image(session, u) if u else None) for u in urls]

        # Launch only actual tasks
        fetched: List[Optional[Image.Image]] = []
        if any(t is not None for t in tasks):
            results = await asyncio.gather(*[t for t in tasks if t is not None], return_exceptions=True)
            # Reinterleave results back into original positions
            it = iter(results)
            for t in tasks:
                if t is None:
                    fetched.append(None)
                else:
                    val = next(it)
                    fetched.append(val if isinstance(val, Image.Image) else None)
        else:
            fetched = [None] * len(urls)

        tiles: List[Image.Image] = [img if img is not None else _placeholder_tile(cell_px) for img in fetched]
        grid = _compose_grid(tiles, cols=cols, cell=cell_px)

        # Build a list of album labels for the embed footer or caption
        labels: List[str] = []
        for a in albums:
            name = str(a.get("name", "")).strip() or "Unknown Album"
            artist = str(a.get("artist", {}).get("name", "")).strip() or "Unknown Artist"
            labels.append(f"{name} — {artist}")
        return grid, labels

class LastFMCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @GUILDS
    @app_commands.command(name="lastfm", description="Top albums grid from Last.fm")
    @app_commands.describe(
        username="Last.fm username",
        timespan="Time span window",
        limit="How many albums to include in the grid (1–100). Defaults to 25.",
        columns="Grid columns. Defaults to 5.",
        cell_size="Pixel size of each tile. Defaults to 240.",
    )
    @app_commands.choices(timespan=_PERIOD_CHOICES)
    async def lastfm(
        self,
        interaction: discord.Interaction,
        username: str,
        timespan: app_commands.Choice[str],
        limit: Optional[int] = 25,
        columns: Optional[int] = 5,
        cell_size: Optional[int] = 240,
    ):
        """
        Example:
          /lastfm username:uniyx timespan:"Last 90 days" limit:25 columns:5 cell_size:240
        """
        await interaction.response.defer(thinking=True)

        # Sanity checks
        limit = max(1, min(int(limit or 25), 100))
        columns = max(1, min(int(columns or 5), 10))
        cell_size = max(100, min(int(cell_size or 240), 512))

        try:
            grid, labels = await _make_album_grid(
                username=username,
                period_label=timespan.value,
                limit=limit,
                cols=columns,
                cell_px=cell_size,
            )
        except Exception as e:
            msg = f"Failed to build album grid: {e}"
            return await interaction.followup.send(msg, ephemeral=True)

        # Encode to PNG for Discord
        buf = io.BytesIO()
        grid.save(buf, format="PNG", optimize=True)
        buf.seek(0)

        # Build a compact caption
        profile_url = f"https://www.last.fm/user/{username}"

        file = discord.File(buf, filename="lastfm_grid.png")
        embed = discord.Embed(title=f"{timespan.value} • Top {limit}", color=discord.Color.blurple())
        embed.set_author(name=username, url=profile_url)
        embed.set_image(url="attachment://lastfm_grid.png")

        await interaction.followup.send(file=file, embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(LastFMCog(bot))