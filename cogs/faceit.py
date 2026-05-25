# cogs/faceit.py
import os
import asyncio
from statistics import mean
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from curl_cffi import requests as curl_requests

from core.config import env_str
from core.discord_utils import guilds_decorator
from core.faceit_utils import FACEIT_BASE_V4

FACEIT_BASE = FACEIT_BASE_V4
FACEIT_STATS_BASE = "https://www.faceit.com/api/statistics/v1"

KD_KEYS = ["Average K/D Ratio", "K/D Ratio", "K/D"]
ADR_KEYS = ["Average Damage/Round", "ADR", "Average Damage per Round"]

THEME_COLOR = 0xFF5500

# -----------------------
# Roster from environment
# -----------------------
FACEIT_ROSTER_RAW = env_str("FACEIT_ROSTER")
ROSTER: List[str] = [nick.strip() for nick in FACEIT_ROSTER_RAW.split(",") if nick.strip()]

# -----------------------
# Helpers
# -----------------------
def _num_or_none(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None

def _fmt_int(x: Any) -> str:
    v = _num_or_none(x)
    if v is None:
        return "n/a"
    return f"{int(round(v))}"

def _fmt_fixed(x: Any, digits: int = 2) -> str:
    v = _num_or_none(x)
    if v is None:
        return "n/a"
    return f"{v:.{digits}f}"

def _fmt_rating_triplet(rating: Any, rating_t: Any, rating_ct: Any) -> str:
    values = [_num_or_none(rating), _num_or_none(rating_t), _num_or_none(rating_ct)]
    if all(v is None for v in values):
        return "n/a"
    return "/".join(f"{v:.2f}" if v is not None else "-" for v in values)

def _fmt_rating(rating: Any, rating_t: Any, rating_ct: Any, *, extended: bool = False) -> str:
    if extended:
        return _fmt_rating_triplet(rating, rating_t, rating_ct)
    return _fmt_fixed(rating)

def _clip_text(value: Any, max_len: int) -> str:
    text = str(value or "")
    if len(text) <= max_len:
        return text
    return text[:max(0, max_len - 1)] + "."

def _safe_url(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    return u.replace("{lang}", "en").rstrip("/")

# -----------------------
# API call accounting
# -----------------------
class ApiCounter:
    """
    Counts attempted HTTP calls, including retries.
    Tracks totals by label (endpoint class).
    """
    def __init__(self) -> None:
        self.total = 0
        self.by_label: Dict[str, int] = defaultdict(int)

    def inc(self, label: str) -> None:
        self.total += 1
        self.by_label[label] += 1

    def summary_lines(self) -> List[str]:
        items = sorted(self.by_label.items(), key=lambda kv: (-kv[1], kv[0]))
        return [f"{k}: {v}" for k, v in items]

# -----------------------
# FACEIT API
# -----------------------
class FaceitAPI:
    def __init__(self, session: aiohttp.ClientSession, api_key: str, *, concurrency: int = 5):
        self.session = session
        self.headers = {"Authorization": f"Bearer {api_key}"}

        self.counter = ApiCounter()
        self.sem = asyncio.Semaphore(max(1, int(concurrency)))

        # Per-command in-memory caches
        self._cache_resolve: Dict[str, Tuple[str, str, Optional[int], Optional[str], Optional[str]]] = {}
        self._cache_lifetime: Dict[str, Dict[str, str]] = {}
        self._cache_recent_batch: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._cache_recent_ratings: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._cache_ranking: Dict[Tuple[str, str, str, Optional[str]], Optional[int]] = {}

    async def _get_json(
        self,
        url: str,
        params: Optional[dict] = None,
        *,
        retries: int = 3,
        label: str = "unknown",
    ) -> dict:
        backoff = 0.75

        for attempt in range(retries):
            async with self.sem:
                self.counter.inc(label)
                async with self.session.get(url, headers=self.headers, params=params, timeout=30) as r:
                    if r.status == 429 and attempt < retries - 1:
                        retry_after = r.headers.get("Retry-After")
                        if retry_after:
                            try:
                                wait = float(retry_after)
                            except Exception:
                                wait = backoff
                        else:
                            wait = backoff

                        await asyncio.sleep(wait)
                        backoff *= 2
                        continue

                    if r.status >= 400:
                        text = await r.text()
                        raise RuntimeError(f"FACEIT GET {url} failed [{r.status}]: {text[:200]}")

                    return await r.json()

        raise RuntimeError("Exhausted retries to FACEIT API")

    async def _get_public_json(
        self,
        url: str,
        params: Optional[dict] = None,
        *,
        retries: int = 3,
        label: str = "unknown",
    ) -> dict:
        backoff = 0.75
        headers = {
            "Accept": "application/json",
            "Referer": "https://www.faceit.com/",
            "Origin": "https://www.faceit.com",
        }

        for attempt in range(retries):
            async with self.sem:
                self.counter.inc(label)

                def fetch() -> dict:
                    response = curl_requests.get(
                        url,
                        params=params,
                        headers=headers,
                        impersonate="chrome136",
                        timeout=30,
                    )
                    if response.status_code >= 400:
                        raise RuntimeError(
                            f"FACEIT GET {url} failed [{response.status_code}]: {response.text[:200]}"
                        )
                    return response.json()

                try:
                    return await asyncio.to_thread(fetch)
                except Exception:
                    if attempt >= retries - 1:
                        raise

            await asyncio.sleep(backoff)
            backoff *= 2

        raise RuntimeError("Exhausted retries to FACEIT public API")

    async def resolve_player(self, nickname: str) -> Tuple[str, str, Optional[int], Optional[str], Optional[str]]:
        if nickname in self._cache_resolve:
            return self._cache_resolve[nickname]

        data = await self._get_json(
            f"{FACEIT_BASE}/players",
            params={"nickname": nickname},
            label="players.resolve",
        )

        pid = data.get("player_id")
        if not pid:
            raise RuntimeError(f"Could not resolve player_id for '{nickname}'")

        nick = data.get("nickname", nickname)
        games = data.get("games") or {}
        cs2 = games.get("cs2") or games.get("csgo")
        elo = cs2.get("faceit_elo") if isinstance(cs2, dict) else None

        out = (pid, nick, elo, _safe_url(data.get("faceit_url")), data.get("avatar"))
        self._cache_resolve[nickname] = out
        return out

    async def get_lifetime_stats(self, player_id: str) -> Dict[str, str]:
        if player_id in self._cache_lifetime:
            return self._cache_lifetime[player_id]

        data = await self._get_json(
            f"{FACEIT_BASE}/players/{player_id}/stats/cs2",
            label="players.stats.lifetime",
        )
        lifetime = data.get("lifetime") or {}
        out = {k.strip(): v for k, v in lifetime.items()} if isinstance(lifetime, dict) else {}
        self._cache_lifetime[player_id] = out
        return out

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

    async def get_recent_stats_batch(self, player_id: str, limit: int = 30) -> Dict[str, Any]:
        limit = max(1, min(100, int(limit)))
        cache_key = (player_id, limit)
        if cache_key in self._cache_recent_batch:
            return self._cache_recent_batch[cache_key]

        url = f"{FACEIT_BASE}/players/{player_id}/games/cs2/stats"
        params = {"offset": 0, "limit": limit}

        data = await self._get_json(url, params=params, label="players.stats.recent_batch")
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
        out = {"kd": kd_recent, "adr": adr_recent, "matches_count": len(items)}
        self._cache_recent_batch[cache_key] = out
        return out

    async def get_recent_ratings_batch(self, player_id: str, limit: int = 30) -> Dict[str, Any]:
        limit = max(1, min(100, int(limit)))
        cache_key = (player_id, limit)
        if cache_key in self._cache_recent_ratings:
            return self._cache_recent_ratings[cache_key]

        url = f"{FACEIT_STATS_BASE}/cs2/players/{player_id}/match-rounds"
        data = await self._get_public_json(
            url,
            params={"limit": limit},
            label="statistics.match_rounds",
        )
        rounds = (
            data.get("payload", {})
                .get("cs2", {})
                .get("match_rounds", [])
        )
        rounds = rounds[:limit]

        def avg_field(field: str) -> Optional[float]:
            values = [_num_or_none(item.get(field)) for item in rounds if isinstance(item, dict)]
            values = [v for v in values if v is not None]
            return mean(values) if values else None

        out = {
            "faceit_rating": avg_field("faceit_rating"),
            "faceit_rating_t": avg_field("faceit_rating_t"),
            "faceit_rating_ct": avg_field("faceit_rating_ct"),
            "matches_count": len(rounds),
        }
        self._cache_recent_ratings[cache_key] = out
        return out

    async def get_global_ranking(
        self,
        player_id: str,
        region: str = "NA",
        game: str = "cs2",
        country: Optional[str] = None,
    ) -> Optional[int]:
        cache_key = (player_id, region, game, country)
        if cache_key in self._cache_ranking:
            return self._cache_ranking[cache_key]

        url = f"{FACEIT_BASE}/rankings/games/{game}/regions/{region}/players/{player_id}"
        params: Dict[str, Any] = {}
        if country:
            params["country"] = country

        data = await self._get_json(url, params=params, label="rankings.global")
        pos = data.get("position")

        out: Optional[int]
        if isinstance(pos, int):
            out = pos
        else:
            try:
                out = int(pos)
            except Exception:
                items = data.get("items") or []
                if items and isinstance(items[0], dict):
                    item_pos = items[0].get("position")
                    try:
                        out = int(item_pos)
                    except Exception:
                        out = None
                else:
                    out = None

        self._cache_ranking[cache_key] = out
        return out

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
        description="FACEIT CS2 ELO, FACEIT rating, ADR, and global ranking for a user or the roster."
    )
    @app_commands.describe(
        user="Optional FACEIT nickname. If omitted, uses the FACEIT_ROSTER from the environment.",
        lifetime="If true, show lifetime stats instead of recent matches.",
        last_matches="If set, show stats over the last N matches (1 to 100). Overrides the default 30.",
        extended="If true, include K/D and show FACEIT rating as overall/T/CT.",
        call="If true, append API call breakdown (includes retries)."
    )
    async def faceit(
        self,
        interaction: discord.Interaction,
        user: Optional[str] = None,
        lifetime: Optional[bool] = False,
        last_matches: Optional[int] = None,
        extended: Optional[bool] = False,
        call: Optional[bool] = False,
    ):
        api_key = env_str("FACEIT_API_KEY")
        if not api_key:
            await interaction.response.send_message(
                "FACEIT_API_KEY is not set on the bot host. Set it and try again.",
                ephemeral=True,
            )
            return

        if user is None and not ROSTER:
            await interaction.response.send_message(
                "FACEIT_ROSTER is not configured in the environment and no user was provided.\n"
                "Set FACEIT_ROSTER in your .env (comma separated nicknames) or specify a user.",
                ephemeral=True,
            )
            return

        assert self.session is not None
        api = FaceitAPI(self.session, api_key, concurrency=5)

        await interaction.response.defer(thinking=True)

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
        errors: List[str] = []

        async def fetch_one(nick: str) -> Optional[Dict[str, Any]]:
            try:
                pid, name, elo_raw, url, avatar = await api.resolve_player(nick)
                elo_num = _num_or_none(elo_raw)

                if use_recent:
                    rating_val = None
                    rating_t_val = None
                    rating_ct_val = None
                    rating_matches = None

                    try:
                        rec = await api.get_recent_stats_batch(pid, limit=last_matches or 30)
                        kd_val = rec["kd"]
                        adr_val = rec["adr"]
                    except Exception as e:
                        life = await api.get_lifetime_stats(pid)
                        kd_val = _num_or_none(api.pick_key(life, KD_KEYS))
                        adr_val = _num_or_none(api.pick_key(life, ADR_KEYS))
                        errors.append(
                            f"{name}: failed recent batch stats for last {last_matches} matches, fell back to lifetime ({e})"
                        )

                    try:
                        ratings = await api.get_recent_ratings_batch(pid, limit=last_matches or 30)
                        rating_val = ratings.get("faceit_rating")
                        rating_t_val = ratings.get("faceit_rating_t")
                        rating_ct_val = ratings.get("faceit_rating_ct")
                        rating_matches = int(ratings.get("matches_count") or 0)
                    except Exception as e:
                        errors.append(f"{name}: failed recent FACEIT rating lookup ({e})")
                else:
                    life = await api.get_lifetime_stats(pid)
                    kd_val = _num_or_none(api.pick_key(life, KD_KEYS))
                    adr_val = _num_or_none(api.pick_key(life, ADR_KEYS))
                    rating_val = None
                    rating_t_val = None
                    rating_ct_val = None
                    rating_matches = None

                ranking: Optional[int] = None
                try:
                    ranking = await api.get_global_ranking(pid, region="NA", game="cs2")
                except Exception as e:
                    errors.append(f"{name}: failed global ranking lookup ({e})")

                return {
                    "name": name,
                    "elo": elo_num,
                    "elo_num": elo_num,
                    "kd": kd_val,
                    "adr": adr_val,
                    "rating": rating_val,
                    "rating_t": rating_t_val,
                    "rating_ct": rating_ct_val,
                    "rating_matches": rating_matches,
                    "ranking": ranking,
                    "url": url,
                    "avatar": avatar,
                }
            except Exception as e:
                errors.append(f"{nick}: {e}")
                return None

        results = await asyncio.gather(*(fetch_one(n) for n in targets))
        rows = [r for r in results if r is not None]

        rows.sort(key=lambda r: (r["elo_num"] is None, -(r["elo_num"] or -1)))

        rating_match_counts = [
            int(r["rating_matches"]) for r in rows
            if _num_or_none(r.get("rating_matches")) is not None and int(r["rating_matches"]) > 0
        ]
        rating_scope = None
        if use_recent and last_matches and rating_match_counts:
            max_rating_matches = max(rating_match_counts)
            if last_matches > max_rating_matches:
                rating_scope = max_rating_matches

        rating_label = f"FR{rating_scope}" if rating_scope else "FR"
        extended_rating_label = f"{rating_label}/T/CT"

        name_w = max(6, min(10, max((len(r["name"]) for r in rows), default=6)))
        elo_w = max(3, max((len(_fmt_int(r["elo"])) for r in rows), default=3))
        kd_w = max(3, max((len(_fmt_fixed(r["kd"])) for r in rows), default=3))
        adr_w = max(3, max((len(_fmt_fixed(r["adr"], 1)) for r in rows), default=3))
        rating_header = extended_rating_label if extended else rating_label
        rating_w = max(len(rating_header), max((
            len(_fmt_rating(r["rating"], r["rating_t"], r["rating_ct"], extended=bool(extended))) for r in rows
        ), default=0))
        ranking_w = max(2, max((len(_fmt_int(r["ranking"])) for r in rows), default=2))

        scope_label = f"Last {last_matches}" if use_recent else "Lifetime"

        if extended:
            header = (
                f"{'Player':<{name_w}} "
                f"{'ELO':>{elo_w}} {'K/D':>{kd_w}} {'ADR':>{adr_w}} "
                f"{rating_header:>{rating_w}} {'NA':>{ranking_w}}"
            )
            sep = (
                f"{'-' * name_w} "
                f"{'-' * elo_w} {'-' * kd_w} {'-' * adr_w} "
                f"{'-' * rating_w} {'-' * ranking_w}"
            )
        else:
            header = (
                f"{'Player':<{name_w}} "
                f"{'ELO':>{elo_w}} {rating_header:>{rating_w}} {'ADR':>{adr_w}} "
                f"{'NA':>{ranking_w}}"
            )
            sep = (
                f"{'-' * name_w} "
                f"{'-' * elo_w} {'-' * rating_w} {'-' * adr_w} "
                f"{'-' * ranking_w}"
            )

        lines = [header, sep]
        for r in rows:
            name_text = _clip_text(r["name"], name_w)
            rating_text = _fmt_rating(r["rating"], r["rating_t"], r["rating_ct"], extended=bool(extended))
            if extended:
                lines.append(
                    f"{name_text:<{name_w}} "
                    f"{_fmt_int(r['elo']):>{elo_w}} {_fmt_fixed(r['kd']):>{kd_w}} "
                    f"{_fmt_fixed(r['adr'], 1):>{adr_w}} "
                    f"{rating_text:>{rating_w}} "
                    f"{_fmt_int(r['ranking']):>{ranking_w}}"
                )
            else:
                lines.append(
                    f"{name_text:<{name_w}} "
                    f"{_fmt_int(r['elo']):>{elo_w}} {rating_text:>{rating_w}} "
                    f"{_fmt_fixed(r['adr'], 1):>{adr_w}} {_fmt_int(r['ranking']):>{ranking_w}}"
                )

        links = [f"[{r['name']}]({r['url']})" for r in rows if r.get("url")]

        embed = discord.Embed(
            title=f"FACEIT CS2 Leaderboard • {scope_label}",
            description="```text\n" + "\n".join(lines) + "\n```",
            color=THEME_COLOR,
        )

        if links:
            embed.add_field(name="Profiles", value=" • ".join(links), inline=False)

        if errors:
            notes_text = "\n".join(f"• {e}" for e in errors)
            if len(notes_text) > 1000:
                notes_text = notes_text[:997] + "..."
            embed.add_field(name="Notes", value=notes_text, inline=False)

        # Only include call breakdown when explicitly requested
        if bool(call):
            call_lines = api.counter.summary_lines()
            call_text = "\n".join(call_lines)
            if len(call_text) > 1000:
                call_text = call_text[:997] + "..."
            embed.add_field(
                name="API Calls",
                value=f"Total: {api.counter.total}\n{call_text}",
                inline=False,
            )

        if len(rows) == 1 and rows[0].get("avatar"):
            embed.set_thumbnail(url=rows[0]["avatar"])

        embed.set_footer(text="Source: FACEIT Data API")

        await interaction.followup.send(embed=embed)

async def setup(bot: commands.Bot):
    await bot.add_cog(FaceitStats(bot))
