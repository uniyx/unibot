# cogs/esea2.py
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands
import aiohttp

from core.config import env_str
from core.discord_utils import guilds_decorator
from cogs.faceit import FaceitAPI, FaceitRatingsUnavailable
from cogs.esea import FaceitV1Client, compute_record_from_fixtures

# =========================
# FIXED IDS (ESEA Main • crescent)
# =========================
TEAM_ID         = "15c9a36f-8169-49eb-a41b-0a0e7567ed37"      # crescent
CHAMPIONSHIP_ID = "33c94aa7-6909-4b03-a8d8-cac136e7274e"      # ESEA S58 NA Main B - Regular Season

# =========================
# ENDPOINTS
# =========================
V4_BASE = "https://open.faceit.com/data/v4"

# =========================
# DISCORD SCOPING / STYLE
# =========================
THEME_COLOR = 0x0c9547
TITLE_BASE  = "crescent <:crescent:855175620891508736>"

# =========================
# CONSTANTS
# =========================
REQUEST_TIMEOUT = 20.0

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

def _v4_headers() -> Dict[str, str]:
    api_key = env_str("FACEIT_API_KEY")
    if not api_key:
        raise RuntimeError("FACEIT_API_KEY is required for v4 stats calls.")
    return {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}

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

    # Pre-format numeric fields with fixed decimals so we can size columns correctly
    formatted_rows = []
    for r in rows:
        kd_str  = f"{r['kd_overall']:.3f}"
        adr_str = f"{r['adr_overall']:.2f}"
        hs_str  = f"{r['hs_pct_overall']:.2f}"
        kpr_str = f"{r['kpr_overall']:.3f}"
        rating = r.get("faceit_rating")
        fr_str = f"{rating:.2f}" if isinstance(rating, (int, float)) else "n/a"
        formatted_rows.append((r, kd_str, adr_str, hs_str, kpr_str, fr_str))

    name_w = max(5, max((len(r["nickname"]) for r in rows), default=5))
    mp_w   = max(2, len("MP"))
    kd_w   = max(4, len("KD"), *(len(fr[1]) for fr in formatted_rows) or [4])
    adr_w  = max(5, len("ADR"), *(len(fr[2]) for fr in formatted_rows) or [5])
    hs_w   = max(4, len("HS%"), *(len(fr[3]) for fr in formatted_rows) or [4])
    kpr_w  = max(3, len("KPR"), *(len(fr[4]) for fr in formatted_rows) or [3])
    fr_w   = max(2, len("FR"), *(len(fr[5]) for fr in formatted_rows) or [2])
    rnd_w  = max(4, len("Rnds"))

    header = (
        f"{'Player':<{name_w}}  "
        f"{'MP':>{mp_w}}  "
        f"{'KD':>{kd_w}}  "
        f"{'ADR':>{adr_w}}  "
        f"{'HS%':>{hs_w}}  "
        f"{'KPR':>{kpr_w}}  "
        f"{'FR':>{fr_w}}  "
        f"{'Rnds':>{rnd_w}}"
    )
    sep = (
        f"{'-'*name_w}  "
        f"{'-'*mp_w}  "
        f"{'-'*kd_w}  "
        f"{'-'*adr_w}  "
        f"{'-'*hs_w}  "
        f"{'-'*kpr_w}  "
        f"{'-'*fr_w}  "
        f"{'-'*rnd_w}"
    )

    lines = [header, sep]
    for r, kd_str, adr_str, hs_str, kpr_str, fr_str in formatted_rows:
        lines.append(
            f"{r['nickname']:<{name_w}}  "
            f"{r['matches_played']:>{mp_w}}  "
            f"{kd_str:>{kd_w}}  "
            f"{adr_str:>{adr_w}}  "
            f"{hs_str:>{hs_w}}  "
            f"{kpr_str:>{kpr_w}}  "
            f"{fr_str:>{fr_w}}  "
            f"{r['rounds']:>{rnd_w}}"
        )

    return "```text\n" + "\n".join(lines) + "\n```"

# =========================
# The Cog
# =========================
class EseaStats(commands.Cog):
    """
    /esea -> Aggregated season totals for crescent. Title includes placement and W - L.
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
        description="crescent ESEA season stats (ADR, KD, HS%, KPR) with placement and record in title"
    )
    async def esea(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        # v4 stats are required
        if not env_str("FACEIT_API_KEY"):
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
            fixtures_api = FaceitV1Client(self.session)
            fixtures = await fixtures_api.fetch_team_fixtures(
                TEAM_ID,
                CHAMPIONSHIP_ID,
                finished_only=True,
            )
        except Exception as e:
            await interaction.followup.send(f"Error fetching fixtures: {e}", ephemeral=True)
            return

        crescent_w, crescent_l = compute_record_from_fixtures(fixtures, TEAM_ID)

        def build_title(w: int, l: int) -> str:
            inside = f"{w}W - {l}L"
            return f"{TITLE_BASE} ({inside}) • ESEA Main stats"

        if not fixtures:
            embed = discord.Embed(
                title=build_title(crescent_w, crescent_l),
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
                title=build_title(crescent_w, crescent_l),
                description="No season stats available yet.",
                color=THEME_COLOR,
            )
            await interaction.followup.send(embed=embed)
            return

        # FACEIT Rating uses the same recent-30 scope and endpoint as /faceit.
        # The rating response does not provide usable ESEA room IDs for an
        # exact season-only filter, so keep its scope explicit in the footer.
        ratings_api = FaceitAPI(self.session, env_str("FACEIT_API_KEY"))
        try:
            for player_id, player_totals in totals.items():
                try:
                    ratings = await ratings_api.get_recent_ratings_batch(player_id, limit=30)
                    player_totals["faceit_rating"] = ratings.get("faceit_rating")
                except FaceitRatingsUnavailable:
                    break
                except Exception:
                    continue
        finally:
            await ratings_api.close()

        table = _render_table(totals)
        embed = discord.Embed(
            title=build_title(crescent_w, crescent_l),
            description=table,
            color=THEME_COLOR
        )
        embed.set_footer(text="Source: FACEIT • ESEA stats: season only • FR: up to 30 recent matches")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(EseaStats(bot))
