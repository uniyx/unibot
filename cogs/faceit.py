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

# Replace with exact FACEIT nicknames
ROSTER: List[str] = [
    "uni",
    "bud",
    "hoax",
    "oldfranz",
    "xCaptain",
    "Benjitora",
    "Sham",
    "coza-"
]

# -----------------------
# Helpers
# -----------------------
def _num_or_none(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def _fmt(x: Any, digits: int = 2) -> str:
    v = _num_or_none(x)
    if v is None:
        return str(x) if x not in (None, "") else "n/a"
    if float(v).is_integer():
        return f"{int(v)}"
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
        Uses the official endpoint that returns a player's per-match stats in one call.
        Path shape documented as "Retrieve statistics of a player for a given amount of matches".
        Query: offset, limit, optional from/to (epoch ms). We only need limit.
        Aggregates K/D from summed kills and deaths and ADR as the mean of match ADRs.
        """
        # Example path used by FACEIT docs:
        # /players/{player_id}/games/{game_id}/stats
        url = f"{FACEIT_BASE}/players/{player_id}/games/cs2/stats"
        params = {"offset": 0, "limit": max(1, min(100, int(limit)))}

        data = await self._get_json(url, params=params)
        items = data.get("items") or []

        kills_total = 0
        deaths_total = 0
        adr_values: List[float] = []

        for it in items:
            # Many responses place numbers under it["stats"]
            stats = it.get("stats") or it  # be permissive
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
        description="FACEIT CS2 ELO, K/D, ADR for a user or the roster. Use official batch endpoint for recent stats."
    )
    @app_commands.describe(
        user="Optional FACEIT nickname. If omitted, uses the hardcoded roster.",
        last30="If true, compute K/D and ADR over the last 30 matches via the official batch endpoint."
    )
    async def faceit(self, interaction: discord.Interaction, user: Optional[str] = None, last30: Optional[bool] = False):
        api_key = os.getenv("FACEIT_API_KEY", "").strip()
        if not api_key:
            await interaction.response.send_message(
                "FACEIT_API_KEY is not set on the bot host. Set it and try again.", ephemeral=True
            )
            return

        assert self.session is not None
        api = FaceitAPI(self.session, api_key)

        await interaction.response.defer(thinking=True)

        targets = [user] if user else ROSTER
        rows = []
        errors: List[str] = []

        for nick in targets:
            try:
                pid, name, elo, url, avatar = await api.resolve_player(nick)

                if last30:
                    try:
                        rec = await api.get_recent_stats_batch(pid, limit=30)
                        kd_val = rec["kd"]
                        adr_val = rec["adr"]
                    except Exception as e:
                        # As a safety net, fall back to lifetime if the endpoint is unavailable for this player.
                        life = await api.get_lifetime_stats(pid)
                        kd_val = _num_or_none(api.pick_key(life, KD_KEYS))
                        adr_val = _num_or_none(api.pick_key(life, ADR_KEYS))
                        errors.append(f"{name}: failed recent batch stats, fell back to lifetime ({e})")

                    kd = _fmt(kd_val) if kd_val is not None else "n/a"
                    adr = _fmt(adr_val) if adr_val is not None else "n/a"
                else:
                    life = await api.get_lifetime_stats(pid)
                    kd = api.pick_key(life, KD_KEYS) or "n/a"
                    adr = api.pick_key(life, ADR_KEYS) or "n/a"

                rows.append({
                    "name": name,
                    "elo": elo,                         # may be None
                    "elo_num": _num_or_none(elo),       # numeric for sorting
                    "kd": kd,
                    "adr": adr,
                    "url": url,
                    "avatar": avatar,
                })
            except Exception as e:
                errors.append(f"{nick}: {e}")

        # Sort by ELO descending; None goes last
        rows.sort(key=lambda r: (r["elo_num"] is None, -(r["elo_num"] or -1)))

        # Build monospaced leaderboard
        rank_w = len(str(len(rows))) if rows else 1
        name_w = max(5, max((len(r["name"]) for r in rows), default=5))
        elo_w  = max(3, max((len(_fmt(r["elo"])) for r in rows), default=3))
        kd_w   = max(3, max((len(_fmt(r["kd"])) for r in rows), default=3))
        adr_w  = max(3, max((len(_fmt(r["adr"])) for r in rows), default=3))

        scope_label = "Last 30" if last30 else "Lifetime"
        header = f"{'#':>{rank_w}}  {'Player':<{name_w}}  {'ELO':>{elo_w}}  {'K/D':>{kd_w}}  {'ADR':>{adr_w}}"
        subhdr = f"{'':>{rank_w}}  {'':<{name_w}}  {'':>{elo_w}}  {f'({scope_label})':>{kd_w}}  {f'({scope_label})':>{adr_w}}"
        sep    = f"{'-'*rank_w}  {'-'*name_w}  {'-'*elo_w}  {'-'*kd_w}  {'-'*adr_w}"

        lines = [header, subhdr, sep]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i:>{rank_w}}  {r['name']:<{name_w}}  "
                f"{_fmt(r['elo']):>{elo_w}}  {_fmt(r['kd']):>{kd_w}}  {_fmt(r['adr']):>{adr_w}}"
            )

        links = [f"[{r['name']}]({r['url']})" for r in rows if r.get("url")]
        embed = discord.Embed(
            title="FACEIT CS2 Leaderboard",
            description="```text\n" + "\n".join(lines) + "\n```",
            color=0x2F3136
        )
        if links:
            embed.add_field(name="Profiles", value=" • ".join(links), inline=False)
        if errors:
            embed.add_field(name="Notes", value="\n".join(f"• {e}" for e in errors), inline=False)
        if len(rows) == 1 and rows[0].get("avatar"):
            embed.set_thumbnail(url=rows[0]["avatar"])
        embed.set_footer(text="Source: FACEIT Data API")

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(FaceitStats(bot))
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

# Replace with exact FACEIT nicknames
ROSTER: List[str] = [
    "uni",
    "bud",
    "hoax",
    "oldfranz",
    "xCaptain",
    "Benjitora",
    "Sham",
    "coza-"
]

# -----------------------
# Helpers
# -----------------------
def _num_or_none(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def _fmt(x: Any, digits: int = 2) -> str:
    v = _num_or_none(x)
    if v is None:
        return str(x) if x not in (None, "") else "n/a"
    if float(v).is_integer():
        return f"{int(v)}"
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
        Uses the official endpoint that returns a player's per-match stats in one call.
        Path shape documented as "Retrieve statistics of a player for a given amount of matches".
        Query: offset, limit, optional from/to (epoch ms). We only need limit.
        Aggregates K/D from summed kills and deaths and ADR as the mean of match ADRs.
        """
        # Example path used by FACEIT docs:
        # /players/{player_id}/games/{game_id}/stats
        url = f"{FACEIT_BASE}/players/{player_id}/games/cs2/stats"
        params = {"offset": 0, "limit": max(1, min(100, int(limit)))}

        data = await self._get_json(url, params=params)
        items = data.get("items") or []

        kills_total = 0
        deaths_total = 0
        adr_values: List[float] = []

        for it in items:
            # Many responses place numbers under it["stats"]
            stats = it.get("stats") or it  # be permissive
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
        description="FACEIT CS2 ELO, K/D, ADR for a user or the roster. Use official batch endpoint for recent stats."
    )
    @app_commands.describe(
        user="Optional FACEIT nickname. If omitted, uses the hardcoded roster.",
        last30="If true, compute K/D and ADR over the last 30 matches via the official batch endpoint."
    )
    async def faceit(self, interaction: discord.Interaction, user: Optional[str] = None, last30: Optional[bool] = False):
        api_key = os.getenv("FACEIT_API_KEY", "").strip()
        if not api_key:
            await interaction.response.send_message(
                "FACEIT_API_KEY is not set on the bot host. Set it and try again.", ephemeral=True
            )
            return

        assert self.session is not None
        api = FaceitAPI(self.session, api_key)

        await interaction.response.defer(thinking=True)

        targets = [user] if user else ROSTER
        rows = []
        errors: List[str] = []

        for nick in targets:
            try:
                pid, name, elo, url, avatar = await api.resolve_player(nick)

                if last30:
                    try:
                        rec = await api.get_recent_stats_batch(pid, limit=30)
                        kd_val = rec["kd"]
                        adr_val = rec["adr"]
                    except Exception as e:
                        # As a safety net, fall back to lifetime if the endpoint is unavailable for this player.
                        life = await api.get_lifetime_stats(pid)
                        kd_val = _num_or_none(api.pick_key(life, KD_KEYS))
                        adr_val = _num_or_none(api.pick_key(life, ADR_KEYS))
                        errors.append(f"{name}: failed recent batch stats, fell back to lifetime ({e})")

                    kd = _fmt(kd_val) if kd_val is not None else "n/a"
                    adr = _fmt(adr_val) if adr_val is not None else "n/a"
                else:
                    life = await api.get_lifetime_stats(pid)
                    kd = api.pick_key(life, KD_KEYS) or "n/a"
                    adr = api.pick_key(life, ADR_KEYS) or "n/a"

                rows.append({
                    "name": name,
                    "elo": elo,                         # may be None
                    "elo_num": _num_or_none(elo),       # numeric for sorting
                    "kd": kd,
                    "adr": adr,
                    "url": url,
                    "avatar": avatar,
                })
            except Exception as e:
                errors.append(f"{nick}: {e}")

        # Sort by ELO descending; None goes last
        rows.sort(key=lambda r: (r["elo_num"] is None, -(r["elo_num"] or -1)))

        # Build monospaced leaderboard
        rank_w = len(str(len(rows))) if rows else 1
        name_w = max(5, max((len(r["name"]) for r in rows), default=5))
        elo_w  = max(3, max((len(_fmt(r["elo"])) for r in rows), default=3))
        kd_w   = max(3, max((len(_fmt(r["kd"])) for r in rows), default=3))
        adr_w  = max(3, max((len(_fmt(r["adr"])) for r in rows), default=3))

        # Put scope in the title
        scope_label = "Last 30" if last30 else "Lifetime"

        header = f"{'#':>{rank_w}}  {'Player':<{name_w}}  {'ELO':>{elo_w}}  {'K/D':>{kd_w}}  {'ADR':>{adr_w}}"
        sep    = f"{'-'*rank_w}  {'-'*name_w}  {'-'*elo_w}  {'-'*kd_w}  {'-'*adr_w}"

        lines = [header, sep]
        for i, r in enumerate(rows, 1):
            lines.append(
                f"{i:>{rank_w}}  {r['name']:<{name_w}}  "
                f"{_fmt(r['elo']):>{elo_w}}  {_fmt(r['kd']):>{kd_w}}  {_fmt(r['adr']):>{adr_w}}"
            )

        links = [f"[{r['name']}]({r['url']})" for r in rows if r.get("url")]

        # Title includes scope
        title = f"FACEIT CS2 Leaderboard • {scope_label}"
        color = 0x2F3136

        embed = discord.Embed(
            title=title,
            description="```text\n" + "\n".join(lines) + "\n```",
            color=color,
        )
        if links:
            embed.add_field(name="Profiles", value=" • ".join(links), inline=False)
        if errors:
            embed.add_field(name="Notes", value="\n".join(f"• {e}" for e in errors), inline=False)
        if len(rows) == 1 and rows[0].get("avatar"):
            embed.set_thumbnail(url=rows[0]["avatar"])
        embed.set_footer(text="Source: FACEIT Data API")

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(FaceitStats(bot))
