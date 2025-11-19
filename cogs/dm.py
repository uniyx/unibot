# cogs/dm.py
import os
import asyncio
import re
from typing import Dict, List, Optional, Tuple
import datetime as dt
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

# =========================
# DEV GUILD DECORATOR
# =========================
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)

# =========================
# CONFIG
# =========================
REGION = "na"

# Support multiple servers
SERVERS: List[str] = [
    "na_na_chi_mirage21rifles",
    "na_na_chi_dust2rifles",
]

RANK_START = 1
PLAYERS = 100

# Column mapping for rankings.php rows:
# 0: rank | 1: player cell | 2: points | 3: monthly_kills | 4: last_active | 5: status
MONTHLY_KILLS_COL_IDX = 3

MONTHLY_GOAL_KILLS = 6000
DAILY_TARGET_KILLS = 200
ASSUMED_DAYS_PER_MONTH = 30
OUTPUT_TZ = ZoneInfo("America/New_York")
EMBED_COLOR = 0x0C9547

# Hardcoded players with fallback names, profile URL -> nickname
PROFILE_URLS: Dict[str, str] = {
    "https://steamcommunity.com/profiles/76561198989623289": "uni",
    "https://steamcommunity.com/profiles/76561198209992732": "xCaptain",
    "https://steamcommunity.com/profiles/76561198855633260": "hoax",
    "https://steamcommunity.com/profiles/76561198097413054": "bud",
}

# Derived: SteamID64 -> nickname (and display order)
STEAM_ID_TO_NAME: Dict[str, str] = {
    url.rstrip("/").split("/")[-1]: nick for url, nick in PROFILE_URLS.items()
}
STEAM_IDS: List[str] = list(STEAM_ID_TO_NAME.keys())

# HTTP headers
FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:144.0) Gecko/20100101 Firefox/144.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.warmupserver.net/rankings.php",
    "Connection": "keep-alive",
}

# =========================
# LIGHTWEIGHT HTML PARSING
# =========================
TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)

# Inside player cell (td index 1)
A_STEAM_RE = re.compile(
    r"<a\b[^>]*href=['\"]\s*https?://steamcommunity\.com/profiles/(\d+)['\"][^>]*>",
    re.S | re.I,
)
NAME_ANCHOR_RE = re.compile(
    r"<a\b[^>]*id=['\"]tablePlayerName['\"][^>]*>(.*?)</a>",
    re.S | re.I,
)

TAG_STRIP_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")

def _strip_tags(html: str) -> str:
    return TAG_STRIP_RE.sub("", html)

def _clean_text(s: str) -> str:
    return WS_RE.sub(" ", s).strip()

def _parse_int_with_spaces(s: str) -> Optional[int]:
    try:
        return int(s.replace(" ", "").replace(",", "").strip())
    except Exception:
        return None

def parse_rankings_html(html: str) -> List[Dict]:
    """
    Returns rows with:
      {
        'rank': int,
        'steamid64': Optional[str],
        'nickname': Optional[str],
        'kills_month': Optional[int],
      }
    """
    rows: List[Dict] = []
    for tr_html in TR_RE.findall(html):
        tds = TD_RE.findall(tr_html)
        if len(tds) < 6:
            continue

        rank_text = _clean_text(_strip_tags(tds[0]))
        try:
            rank = int(rank_text)
        except Exception:
            continue

        player_cell = tds[1]
        steamid = None
        m = A_STEAM_RE.search(player_cell)
        if m:
            steamid = m.group(1)

        nickname = None
        n = NAME_ANCHOR_RE.search(player_cell)
        if n:
            nickname = _clean_text(_strip_tags(n.group(1)))

        km = _parse_int_with_spaces(_clean_text(_strip_tags(tds[MONTHLY_KILLS_COL_IDX])))

        rows.append(
            {
                "rank": rank,
                "steamid64": steamid,
                "nickname": nickname,
                "kills_month": km,
            }
        )
    return rows

# =========================
# FETCH (CONCURRENT, MULTI-SERVER)
# =========================
async def fetch_rankings_for_server(session: aiohttp.ClientSession, server: str) -> str:
    url = (
        "https://www.warmupserver.net/rankings.php"
        f"?region={REGION}&server={server}&rank={RANK_START}&players={PLAYERS}"
    )
    for attempt in range(5):
        try:
            async with session.get(url, headers=FETCH_HEADERS, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                text = await resp.text()
                if resp.status == 200 and "<td" in text.lower():
                    return text
        except Exception:
            pass
        await asyncio.sleep(min(1.5 * (2 ** attempt), 8.0))
    raise RuntimeError(f"Failed to fetch rankings for {server} after retries")

# =========================
# COMPUTE
# =========================
def compute_today_target(now_local: dt.datetime) -> int:
    target = DAILY_TARGET_KILLS * now_local.day
    return target if target < MONTHLY_GOAL_KILLS else MONTHLY_GOAL_KILLS

def _progress_bar(current: int, goal: int, width: int = 16) -> str:
    ratio = 0.0 if goal <= 0 else min(max(current / goal, 0.0), 1.0)
    filled = int(round(ratio * width))
    if filled > width:
        filled = width
    return "█" * filled + "░" * (width - filled)

def _delta_emoji(delta: int) -> str:
    if delta > 0:
        return "🟢"
    if delta < 0:
        return "🔴"
    return "🟡"

def _medal(rank: int) -> str:
    return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "🏁")

def format_table_pretty(rows: List[Tuple[str, int, int, int]], server_count: int, now: dt.datetime) -> str:
    """
    rows: list of (label, kills, pct_done, delta_vs_today)
    """
    sorted_by_kills = sorted(rows, key=lambda r: r[1], reverse=True)
    label_to_medal: Dict[str, str] = {name: _medal(i + 1) for i, (name, _, _, _) in enumerate(sorted_by_kills)}

    name_w = max(5, max(len(r[0]) for r in rows) if rows else 5)
    header = (
        f"**🏆 Warmup Monthly Progress**\n"
        f"Servers: **{server_count}** • Goal: **{MONTHLY_GOAL_KILLS}** • Target/day: **{DAILY_TARGET_KILLS}** • "
        f"{now.strftime('%a %b %d, %Y %H:%M %Z')}\n\n"
    )
    title_line = f"{'Player':{name_w}}  {'Kills':>6}  {'%':>3}  {'Δ':>5}  {'Progress':<{16}}"
    sep_line = "-" * len(title_line)
    lines = [title_line, sep_line]
    for name, kills, pct_done, delta in rows:
        medal = label_to_medal.get(name, "🏁")
        delta_mark = _delta_emoji(delta)
        bar = _progress_bar(kills, MONTHLY_GOAL_KILLS, width=16)
        lines.append(f"{medal} {name:{name_w}}  {kills:6d}  {pct_done:3d}  {delta_mark}{delta:+4d}  {bar}")
    return "```\n" + "\n".join(lines) + "\n```"

# =========================
# COG
# =========================
class dm(commands.Cog):
    """
    /dm: aggregate monthly kills for configured SteamIDs across multiple servers,
    vs a 6,000 goal and day-of-month schedule. Prefers nickname from HTML; falls back to hardcoded names; then ID tail.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @guilds_decorator()
    @app_commands.command(name="dm", description="Aggregated monthly WarmupServer kill progress for configured Steam profiles.")
    async def dm(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        now = dt.datetime.now(OUTPUT_TZ)
        today_target = compute_today_target(now)

        async with aiohttp.ClientSession() as session:
            html_pages = await asyncio.gather(
                *[fetch_rankings_for_server(session, server) for server in SERVERS],
                return_exceptions=True,
            )

        kills_agg: Dict[str, int] = {}
        nickname_html: Dict[str, str] = {}

        for page in html_pages:
            if isinstance(page, Exception):
                continue
            rows = parse_rankings_html(page)
            for row in rows:
                sid = row.get("steamid64")
                if not sid:
                    continue
                km = row.get("kills_month")
                if km is None:
                    continue
                kills_agg[sid] = kills_agg.get(sid, 0) + km
                nick = row.get("nickname")
                if nick and sid not in nickname_html:
                    nickname_html[sid] = nick

        # Build rows, then optionally sort by kills
        table_rows: List[Tuple[str, int, int, int]] = []
        for sid in STEAM_IDS:
            kills = kills_agg.get(sid, 0)
            label = nickname_html.get(sid) or STEAM_ID_TO_NAME.get(sid) or sid[-6:]
            pct_done = int(round((kills / MONTHLY_GOAL_KILLS) * 100))
            delta_vs_today = kills - today_target
            table_rows.append((label, kills, pct_done, delta_vs_today))
            # Sort primarily by kills desc, then by label to keep output stable on ties
            table_rows.sort(key=lambda r: (-r[1], r[0].lower()))

        embed = discord.Embed(
            title="crescent <:crescent:855175620891508736> • warmup monthly progress",
            description=format_table_pretty(table_rows, server_count=len(SERVERS), now=now),
            color=EMBED_COLOR,
        )
        embed.set_footer(text=f"Server {SERVERS} • Region {REGION} • {now.strftime('%a %b %d, %Y %H:%M %Z')}")
        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(dm(bot))
