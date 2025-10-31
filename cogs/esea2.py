# cogs/esea.py
import os
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

# =========================
# FIXED IDS (ESEA Main • crescent)
# =========================
OUTPUT_TZ       = "America/New_York"
TEAM_ID         = "15c9a36f-8169-49eb-a41b-0a0e7567ed37"      # crescent
CHAMPIONSHIP_ID = "c5749517-d0b9-4d12-aec1-329393db934b"      # ESEA S55 NA Main Central

# =========================
# ENDPOINTS
# =========================
V1_BASE = "https://www.faceit.com/api"
V4_BASE = "https://open.faceit.com/data/v4"

# =========================
# DISCORD SCOPING / STYLE
# =========================
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)

THEME_COLOR = 0x0c9547
TITLE_BASE  = "crescent <:crescent:855175620891508736>"

# =========================
# CONSTANTS
# =========================
REQUEST_TIMEOUT = 20.0
PAGE_LIMIT_V1   = 70

# =========================
# HELPERS
# =========================
def _to_int(x: Any) -> int:
    if x is None:
        return 0
    if isinstance(x, int):
        return x
    s = str(x).replace("%", "").strip()
    try:
        return int(float(s))
    except Exception:
        return 0

def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).replace("%", "").strip()
    try:
        return float(s)
    except Exception:
        return 0.0

def _fmt_float(x: float, places: int) -> str:
    s = f"{x:.{places}f}".rstrip("0").rstrip(".")
    return s if s else "0"

def _v1_headers() -> Dict[str, str]:
    return {"Accept": "application/json"}

def _v4_headers() -> Dict[str, str]:
    api_key = os.getenv("FACEIT_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("FACEIT_API_KEY is required for v4 stats calls.")
    return {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}

# ---------------- Public v1 fixtures (finished only) ----------------
async def _fetch_finished_fixtures(session: aiohttp.ClientSession, team_id: str, champ_id: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    offset = 0
    while True:
        params = {
            "participantId": team_id,
            "participantType": "TEAM",
            "championshipId": champ_id,
            "limit": str(PAGE_LIMIT_V1),
            "offset": str(offset),
            "sort": "ASC",
        }
        url = f"{V1_BASE}/championships/v1/matches"
        async with session.get(url, params=params, headers=_v1_headers(), timeout=REQUEST_TIMEOUT) as resp:
            if resp.status == 404:
                break
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"fixtures HTTP {resp.status}: {text[:200]}")
            data = await resp.json()
            page = (data.get("payload") or {}).get("items") or []
            if not page:
                break
            items.extend([x for x in page if str(x.get("status", "")).lower() == "finished"])
            if len(page) < PAGE_LIMIT_V1:
                break
            offset += PAGE_LIMIT_V1

    items.sort(key=lambda m: (m.get("origin", {}).get("schedule", 0)))
    return items

def _compute_record_from_fixtures(fixtures: List[Dict[str, Any]], team_id: str) -> Tuple[int, int]:
    w = l = 0
    for m in fixtures:
        winner = str(m.get("winner") or "")
        if not winner:
            continue
        if winner == team_id:
            w += 1
        else:
            l += 1
    return w, l

# ---------------- Open v4 match stats ----------------
async def _fetch_match_stats(session: aiohttp.ClientSession, match_id: str) -> Dict[str, Any]:
    url = f"{V4_BASE}/matches/{match_id}/stats"
    async with session.get(url, headers=_v4_headers(), timeout=REQUEST_TIMEOUT) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"/matches/{match_id}/stats HTTP {resp.status}: {text[:200]}")
        return await resp.json()

def _extract_players_for_team(stats_json: Dict[str, Any], team_id: str) -> List[Dict[str, Any]]:
    per_player: Dict[str, Dict[str, Any]] = {}
    for mp in (stats_json.get("rounds") or []):
        map_rounds = _to_int((mp.get("round_stats") or {}).get("Rounds"))
        for t in (mp.get("teams", []) or []):
            tid = t.get("team_id") or t.get("faction_id") or t.get("id")
            if str(tid) != str(team_id):
                continue
            for p in (t.get("players", []) or []):
                pid = p.get("player_id") or p.get("guid") or "unknown"
                nick = p.get("nickname") or p.get("name") or pid
                stats = p.get("player_stats") or p

                kills     = _to_int(stats.get("Kills") or stats.get("kills"))
                deaths    = _to_int(stats.get("Deaths") or stats.get("deaths"))
                headshots = _to_int(stats.get("Headshots") or stats.get("headshots") or stats.get("HS"))
                adr_val   = _to_float(stats.get("ADR") or stats.get("Average Damage per Round") or stats.get("Damage") or 0)

                slot = per_player.setdefault(pid, {
                    "player_id": pid,
                    "nickname": nick,
                    "kills": 0,
                    "deaths": 0,
                    "headshots": 0,
                    "rounds": 0,
                    "adr_weighted_sum": 0.0,
                })
                slot["nickname"] = nick
                slot["kills"] += kills
                slot["deaths"] += deaths
                slot["headshots"] += headshots
                slot["rounds"] += map_rounds
                if map_rounds > 0:
                    slot["adr_weighted_sum"] += adr_val * map_rounds

    out: List[Dict[str, Any]] = []
    for pid, slot in per_player.items():
        k = slot["kills"]; d = slot["deaths"]; hs = slot["headshots"]; r = slot["rounds"]
        adr_avg = round(slot["adr_weighted_sum"] / r, 2) if r > 0 else 0.0
        kd_val = float(k) if d == 0 else round(k / d, 3)
        hs_pct_val = round((hs / k) * 100.0, 2) if k > 0 else 0.0
        kpr_val = round(k / r, 3) if r > 0 else 0.0
        out.append({
            "player_id": pid,
            "nickname": slot["nickname"],
            "kills": k,
            "deaths": d,
            "headshots": hs,
            "rounds": r,
            "adr": adr_avg,
            "kd": kd_val,
            "hs_pct": hs_pct_val,
            "kpr": kpr_val,
        })
    out.sort(key=lambda r: (-r["kills"], r["nickname"].lower()))
    return out

def _aggregate_totals(per_match: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    agg: Dict[str, Dict[str, Any]] = {}
    for entry in per_match:
        for p in entry.get("players", []):
            pid = p["player_id"]; nick = p["nickname"]
            slot = agg.setdefault(pid, {
                "player_id": pid,
                "nickname": nick,
                "matches_played": 0,
                "kills": 0,
                "deaths": 0,
                "headshots": 0,
                "rounds": 0,
                "adr_weighted_sum": 0.0,
            })
            slot["nickname"] = nick
            slot["matches_played"] += 1

            k = int(p.get("kills", 0)); d = int(p.get("deaths", 0))
            hs = int(p.get("headshots", 0)); r = int(p.get("rounds", 0))
            adr_match = _to_float(p.get("adr", 0.0))

            slot["kills"] += k
            slot["deaths"] += d
            slot["headshots"] += hs
            slot["rounds"] += r
            if r > 0:
                slot["adr_weighted_sum"] += adr_match * r

    for slot in agg.values():
        k = slot["kills"]; d = slot["deaths"]; hs = slot["headshots"]; r = slot["rounds"]
        slot["kd_overall"] = float(k) if d == 0 else round(k / d, 3)
        slot["hs_pct_overall"] = round((hs / k) * 100.0, 2) if k > 0 else 0.0
        slot["adr_overall"] = round(slot["adr_weighted_sum"] / r, 2) if r > 0 else 0.0
        slot["kpr_overall"] = round(k / r, 3) if r > 0 else 0.0
        del slot["adr_weighted_sum"]

    return dict(sorted(agg.items(), key=lambda kv: (-kv[1]["adr_overall"], kv[1]["nickname"].lower())))

def _render_table(agg: Dict[str, Dict[str, Any]]) -> str:
    rows: List[Dict[str, Any]] = list(agg.values())[:20]  # safety cap for embed size

    name_w = max(5, max((len(r["nickname"]) for r in rows), default=5))
    mp_w   = max(2, len("MP"))
    kd_w   = max(4, len("KD"))
    adr_w  = max(5, len("ADR"))
    hs_w   = max(4, len("HS%"))
    kpr_w  = max(3, len("KPR"))
    rnd_w  = max(4, len("Rnds"))

    header = (
        f"{'Player':<{name_w}}  "
        f"{'MP':>{mp_w}}  "
        f"{'KD':>{kd_w}}  "
        f"{'ADR':>{adr_w}}  "
        f"{'HS%':>{hs_w}}  "
        f"{'KPR':>{kpr_w}}  "
        f"{'Rnds':>{rnd_w}}"
    )
    sep = (
        f"{'-'*name_w}  "
        f"{'-'*mp_w}  "
        f"{'-'*kd_w}  "
        f"{'-'*adr_w}  "
        f"{'-'*hs_w}  "
        f"{'-'*kpr_w}  "
        f"{'-'*rnd_w}"
    )

    lines = [header, sep]
    for r in rows:
        lines.append(
            f"{r['nickname']:<{name_w}}  "
            f"{r['matches_played']:>{mp_w}}  "
            f"{_fmt_float(r['kd_overall'], 3):>{kd_w}}  "
            f"{_fmt_float(r['adr_overall'], 2):>{adr_w}}  "
            f"{_fmt_float(r['hs_pct_overall'], 2):>{hs_w}}  "
            f"{_fmt_float(r['kpr_overall'], 3):>{kpr_w}}  "
            f"{r['rounds']:>{rnd_w}}"
        )

    return "```text\n" + "\n".join(lines) + "\n```"

# =========================
# The Cog
# =========================
class EseaStats(commands.Cog):
    """
    /esea -> Aggregated season totals for crescent. Title includes W - L.
    """

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
        name="esea",
        description="crescent ESEA season stats (ADR, KD, HS%, KPR) with record in title"
    )
    async def esea(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        # v4 stats are required
        if not os.getenv("FACEIT_API_KEY", "").strip():
            await interaction.followup.send(
                "FACEIT_API_KEY is not set. The /esea stats path uses the Open v4 endpoints.",
                ephemeral=True,
            )
            return
        if self.session is None:
            await interaction.followup.send("Internal error: HTTP session not initialized.", ephemeral=True)
            return

        # 1) finished fixtures for record and match ids
        try:
            fixtures = await _fetch_finished_fixtures(self.session, TEAM_ID, CHAMPIONSHIP_ID)
        except Exception as e:
            await interaction.followup.send(f"Error fetching fixtures: {e}", ephemeral=True)
            return

        crescent_w, crescent_l = _compute_record_from_fixtures(fixtures, TEAM_ID)

        if not fixtures:
            embed = discord.Embed(
                title=f"{TITLE_BASE} ({crescent_w}W - {crescent_l}L) • ESEA Main stats",
                description="No finished matches found for this season.",
                color=THEME_COLOR,
            )
            await interaction.followup.send(embed=embed)
            return

        # 2) for each finished match, pull team box-score and accumulate
        per_match: List[Dict[str, Any]] = []
        for m in fixtures:
            room_id = str(m.get("origin", {}).get("id") or "")
            if not room_id:
                continue
            try:
                stats_json = await _fetch_match_stats(self.session, room_id)
            except Exception:
                continue
            players = _extract_players_for_team(stats_json, TEAM_ID)
            if players:
                per_match.append({"match_id": room_id, "players": players})

        totals = _aggregate_totals(per_match)
        if not totals:
            embed = discord.Embed(
                title=f"{TITLE_BASE} ({crescent_w}W - {crescent_l}L) • ESEA Main stats",
                description="No season stats available yet.",
                color=THEME_COLOR,
            )
            await interaction.followup.send(embed=embed)
            return

        table = _render_table(totals)
        embed = discord.Embed(
            title=f"{TITLE_BASE} ({crescent_w}W - {crescent_l}L) • ESEA Main stats",
            description=table,
            color=THEME_COLOR
        )
        embed.set_footer(text="Source: FACEIT Data API (ESEA season only)")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(EseaStats(bot))
