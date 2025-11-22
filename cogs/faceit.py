# cogs/faceit.py
import os
import asyncio
from statistics import mean
from typing import Dict, List, Optional, Tuple, Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)

FACEIT_BASE = "https://open.faceit.com/data/v4"

KD_KEYS = ["Average K/D Ratio", "K/D Ratio", "K/D"]
ADR_KEYS = ["Average Damage/Round", "ADR", "Average Damage per Round"]

THEME_COLOR = 0xFF5500

# -----------------------
# Roster from environment
# -----------------------
FACEIT_ROSTER_RAW = os.getenv("FACEIT_ROSTER", "").strip()
ROSTER: List[str] = [
    nick.strip()
    for nick in FACEIT_ROSTER_RAW.split(",")
    if nick.strip()
]

# -----------------------
# Helpers
# -----------------------
def _num_or_none(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def _fmt_int(x: Any) -> str:
    """
    Format as integer (for ELO, ranking). Returns 'n/a' if None/invalid.
    """
    v = _num_or_none(x)
    if v is None:
        return "n/a"
    return f"{int(round(v))}"

def _fmt_fixed(x: Any, digits: int = 2) -> str:
    """
    Format as fixed-decimal float (for KD, ADR). Returns 'n/a' if None/invalid.
    """
    v = _num_or_none(x)
    if v is None:
        return "n/a"
    return f"{v:.{digits}f}"

def _safe_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    return u.replace("{lang}", "en").rstrip("/")

# -----------------------
# FACEIT API
# -----------------------
class FaceitAPI:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self.session = session
        self.headers = {"Authorization": f"Bearer {api_key}"}

    async def _get_json(self, url: str, params: Optional[dict] = None, *, retries: int = 3) -> dict:
        backoff = 0.75
        for attempt in range(retries):
            async with self.session.get(url, headers=self.headers, params=params, timeout=30) as r:
                if r.status == 429 and attempt < retries - 1:
                    await asyncio.sleep(backoff)
                    backoff *= 2
                    continue
                if r.status >= 400:
                    text = await r.text()
                    raise RuntimeError(f"FACEIT GET {url} failed [{r.status}]: {text[:200]}")
                return await r.json()
        raise RuntimeError("Exhausted retries to FACEIT API")

    # ---- Players and lifetime stats
    async def resolve_player(self, nickname: str) -> Tuple[str, str, Optional[int], Optional[str], Optional[str]]:
        """
        Return (player_id, canonical_nickname, elo, faceit_url, avatar_url).
        """
        data = await self._get_json(f"{FACEIT_BASE}/players", params={"nickname": nickname})
        pid = data.get("player_id")
        if not pid:
            raise RuntimeError(f"Could not resolve player_id for '{nickname}'")
        nick = data.get("nickname", nickname)
        games = data.get("games") or {}
        cs2 = games.get("cs2") or games.get("csgo")
        elo = cs2.get("faceit_elo") if isinstance(cs2, dict) else None
        return pid, nick, elo, _safe_url(data.get("faceit_url")), data.get("avatar")

    async def get_lifetime_stats(self, player_id: str) -> Dict[str, str]:
        data = await self._get_json(f"{FACEIT_BASE}/players/{player_id}/stats/cs2")
        lifetime = data.get("lifetime") or {}
        return {k.strip(): v for k, v in lifetime.items()} if isinstance(lifetime, dict) else {}

    @staticmethod
    def pick_key(d: Dict[str, str], candidates: List[str]) -> Optional[str]:
        for k in candidates:
            if k in d:
                return str(d[k])
        lower = {k.lower(): k for k in d}
        for k in candidates:
            if k.lower() in lower:
                return str(d[lower[k.lower()]])
        return None

    # ---- Recent stats via official "player stats over matches" endpoint
    async def get_recent_stats_batch(self, player_id: str, limit: int = 30) -> Dict[str, Any]:
        """
        Uses the official endpoint that returns a player's per match stats in one call.
        Aggregates K/D from summed kills and deaths and ADR as the mean of match ADRs.
        """
        url = f"{FACEIT_BASE}/players/{player_id}/games/cs2/stats"
        params = {"offset": 0, "limit": max(1, min(100, int(limit)))}

        data = await self._get_json(url, params=params)
        items = data.get("items") or []

        kills_total = 0
        deaths_total = 0
        adr_values: List[float] = []

        for it in items:
            stats = it.get("stats") or it
            k = _num_or_none(stats.get("Kills"))
            d = _num_or_none(stats.get("Deaths"))
            adr = _num_or_none(stats.get("ADR") or stats.get("Average Damage/Round"))

            if k is not None:
                kills_total += int(k)
            if d is not None:
                deaths_total += int(d)
            if adr is not None:
                adr_values.append(float(adr))

        if kills_total == 0 and deaths_total == 0:
            kd_recent = None
        elif deaths_total == 0:
            kd_recent = float(kills_total)
        else:
            kd_recent = float(kills_total) / float(deaths_total)

        adr_recent = mean(adr_values) if adr_values else None
        return {"kd": kd_recent, "adr": adr_recent, "matches_count": len(items)}

    # ---- Global ranking via Data API
    async def get_global_ranking(
        self,
        player_id: str,
        region: str = "NA",
        game: str = "cs2",
        country: Optional[str] = None,
    ) -> Optional[int]:
        """
        Return the player's global ranking in the given region and game, or None.

        Uses the official Data API endpoint:
        /rankings/games/{game}/regions/{region}/players/{player_id}
        """
        url = f"{FACEIT_BASE}/rankings/games/{game}/regions/{region}/players/{player_id}"
        params: Dict[str, Any] = {}
        if country:
            params["country"] = country

        data = await self._get_json(url, params=params)
        pos = data.get("position")
        if isinstance(pos, int):
            return pos
        try:
            return int(pos)
        except Exception:
            items = data.get("items") or []
            if items and isinstance(items[0], dict):
                item_pos = items[0].get("position")
                try:
                    return int(item_pos)
                except Exception:
                    return None
            return None

# -----------------------
# Cog
# -----------------------
class FaceitStats(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()

    @guilds_decorator()
    @app_commands.command(
        name="faceit",
        description="FACEIT CS2 ELO, K/D, ADR, and global ranking for a user or the roster (default: last 30 matches)."
    )
    @app_commands.describe(
        user="Optional FACEIT nickname. If omitted, uses the FACEIT_ROSTER from the environment.",
        lifetime="If true, show lifetime stats instead of recent matches.",
        last_matches="If set, show stats over the last N matches (1 to 100). Overrides the default 30."
    )
    async def faceit(
        self,
        interaction: discord.Interaction,
        user: Optional[str] = None,
        lifetime: Optional[bool] = False,
        last_matches: Optional[int] = None,
    ):
        api_key = os.getenv("FACEIT_API_KEY", "").strip()
        if not api_key:
            await interaction.response.send_message(
                "FACEIT_API_KEY is not set on the bot host. Set it and try again.",
                ephemeral=True,
            )
            return

        # Require FACEIT_ROSTER if no explicit user is provided
        if user is None and not ROSTER:
            await interaction.response.send_message(
                "FACEIT_ROSTER is not configured in the environment and no user was provided.\n"
                "Set FACEIT_ROSTER in your .env (comma separated nicknames) or specify a user.",
                ephemeral=True,
            )
            return

        assert self.session is not None
        api = FaceitAPI(self.session, api_key)

        await interaction.response.defer(thinking=True)

        # Decide scope:
        # 1. If last_matches is provided, use that.
        # 2. Else if lifetime is true, use lifetime.
        # 3. Else default to last 30 matches.
        if last_matches is not None:
            use_recent = True
            try:
                last_matches = int(last_matches)
            except (TypeError, ValueError):
                last_matches = 30
            last_matches = max(1, min(100, last_matches))
        elif lifetime:
            use_recent = False
            last_matches = None
        else:
            use_recent = True
            last_matches = 30

        targets = [user] if user else ROSTER
        rows = []
        errors: List[str] = []

        for nick in targets:
            try:
                pid, name, elo_raw, url, avatar = await api.resolve_player(nick)
                elo_num = _num_or_none(elo_raw)

                # Recent or lifetime stats
                kd_val: Optional[float]
                adr_val: Optional[float]

                if use_recent:
                    try:
                        rec = await api.get_recent_stats_batch(pid, limit=last_matches)
                        kd_val = rec["kd"]
                        adr_val = rec["adr"]
                    except Exception as e:
                        life = await api.get_lifetime_stats(pid)
                        kd_val = _num_or_none(api.pick_key(life, KD_KEYS))
                        adr_val = _num_or_none(api.pick_key(life, ADR_KEYS))
                        errors.append(
                            f"{name}: failed recent batch stats for last {last_matches} matches, "
                            f"fell back to lifetime ({e})"
                        )
                else:
                    life = await api.get_lifetime_stats(pid)
                    kd_val = _num_or_none(api.pick_key(life, KD_KEYS))
                    adr_val = _num_or_none(api.pick_key(life, ADR_KEYS))

                # Global ranking
                ranking: Optional[int] = None
                try:
                    ranking = await api.get_global_ranking(pid, region="NA", game="cs2")
                except Exception as e:
                    errors.append(f"{name}: failed global ranking lookup ({e})")

                rows.append(
                    {
                        "name": name,
                        "elo": elo_num,
                        "elo_num": elo_num,
                        "kd": kd_val,
                        "adr": adr_val,
                        "ranking": ranking,
                        "url": url,
                        "avatar": avatar,
                    }
                )
            except Exception as e:
                errors.append(f"{nick}: {e}")

        # Sort by ELO descending; None goes last
        rows.sort(key=lambda r: (r["elo_num"] is None, -(r["elo_num"] or -1)))

        # Build monospaced leaderboard
        idx_w = len(str(len(rows))) if rows else 1
        name_w = max(5, max((len(r["name"]) for r in rows), default=5))
        elo_w = max(3, max((len(_fmt_int(r["elo"])) for r in rows), default=3))
        kd_w = max(3, max((len(_fmt_fixed(r["kd"])) for r in rows), default=3))
        adr_w = max(3, max((len(_fmt_fixed(r["adr"])) for r in rows), default=3))
        ranking_w = max(7, max((len(_fmt_int(r["ranking"])) for r in rows), default=7))

        if use_recent:
            scope_label = f"Last {last_matches}"
        else:
            scope_label = "Lifetime"

        header = (
            f"{'#':>{idx_w}}  {'Player':<{name_w}}  "
            f"{'ELO':>{elo_w}}  {'K/D':>{kd_w}}  {'ADR':>{adr_w}}  {'Ranking':>{ranking_w}}"
        )
        sep = (
            f"{'-' * idx_w}  {'-' * name_w}  "
            f"{'-' * elo_w}  {'-' * kd_w}  {'-' * adr_w}  {'-' * ranking_w}"
        )

        lines = [header, sep]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i:>{idx_w}}  {r['name']:<{name_w}}  "
                f"{_fmt_int(r['elo']):>{elo_w}}  {_fmt_fixed(r['kd']):>{kd_w}}  {_fmt_fixed(r['adr']):>{adr_w}}  {_fmt_int(r['ranking']):>{ranking_w}}"
            )

        links = [f"[{r['name']}]({r['url']})" for r in rows if r.get("url")]

        title = f"FACEIT CS2 Leaderboard • {scope_label}"

        embed = discord.Embed(
            title=title,
            description="```text\n" + "\n".join(lines) + "\n```",
            color=THEME_COLOR,
        )
        if links:
            embed.add_field(name="Profiles", value=" • ".join(links), inline=False)

        if errors:
            # Clamp Notes to satisfy Discord's 1024 character limit per field
            notes_text = "\n".join(f"• {e}" for e in errors)
            if len(notes_text) > 1000:
                notes_text = notes_text[:997] + "..."
            embed.add_field(
                name="Notes",
                value=notes_text,
                inline=False,
            )

        if len(rows) == 1 and rows[0].get("avatar"):
            embed.set_thumbnail(url=rows[0]["avatar"])
        embed.set_footer(text="Source: FACEIT Data API")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(FaceitStats(bot))
