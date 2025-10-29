import os
import datetime as dt
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple, Set

import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp


# =========================
# CONFIG / CONSTANTS
# =========================

CHAMPIONSHIP_ID = "c5749517-d0b9-4d12-aec1-329393db934b"  # ESEA S55 NA Main Central - Regular Season
TEAM_ID = "15c9a36f-8169-49eb-a41b-0a0e7567ed37"          # crescent

OUTPUT_TZ = "America/New_York"
FACEIT_API_BASE = "https://open.faceit.com/data/v4"
FACEIT_ROOM_BASE = "https://www.faceit.com/en/cs2/room"

PAGE_LIMIT = 100
MAX_OFFSET_SAFETY = 600

REQUEST_TIMEOUT = 15.0
REQUEST_DELAY_SEC = 0.0  # throttle if needed

# Guild scoping
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)

############ ALERT CONFIG ############
ROLE_ID = 1432855602224304258  # role to ping
ALERT_CHANNEL_ID = 1430384084802211952  # channel
ALERT_LEAD_MINUTES = 30               # how many minutes before match start to ping
ALERT_LOOP_SECONDS = 60               # how often to poll upcoming matches
######################################


# =========================
# UTILS
# =========================

def _fmt_safe(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        s = f"{v:.2f}"
        s = s.rstrip("0").rstrip(".")
        return s
    return str(v)


def _to_int(x: Any) -> int:
    if x is None:
        return 0
    if isinstance(x, int):
        return x
    s = str(x).strip().replace("%", "").strip()
    try:
        return int(float(s))
    except Exception:
        return 0


def _to_float(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace("%", "").strip()
    try:
        return float(s)
    except Exception:
        return 0.0


def _ts_to_local(ts_unix: Optional[int]) -> str:
    if ts_unix is None:
        return "N/A"
    try:
        dt_utc = dt.datetime.fromtimestamp(int(ts_unix), dt.timezone.utc)
    except Exception:
        return "N/A"
    try:
        local_dt = dt_utc.astimezone(ZoneInfo(OUTPUT_TZ))
    except Exception:
        local_dt = dt_utc
    return local_dt.strftime("%Y-%m-%d %H:%M %Z")


def _unix_to_dt_local(ts_unix: Optional[int]) -> Optional[dt.datetime]:
    """
    Convert unix timestamp to aware datetime in OUTPUT_TZ.
    """
    if ts_unix is None:
        return None
    base = dt.datetime.fromtimestamp(int(ts_unix), dt.timezone.utc)
    return base.astimezone(ZoneInfo(OUTPUT_TZ))


# =========================
# FACEIT API CLIENT
# =========================

class EseaAPI:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self.session = session
        self.api_key = api_key.strip()

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def _get_json(
        self,
        url: str,
        params: Optional[Dict[str, str]] = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> Dict[str, Any]:
        async with self.session.get(url, headers=self._headers(), params=params, timeout=timeout) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"GET {url} [{resp.status}]: {text[:200]}")
            return await resp.json()

    async def fetch_matches_page(
        self,
        match_type: str,
        limit: int,
        offset: int,
    ) -> Dict[str, Any]:
        url = f"{FACEIT_API_BASE}/championships/{CHAMPIONSHIP_ID}/matches"
        params = {
            "type": match_type,
            "limit": str(limit),
            "offset": str(offset),
        }
        return await self._get_json(url, params=params)

    async def collect_all_for_type(self, match_type: str) -> List[Dict[str, Any]]:
        all_items: List[Dict[str, Any]] = []
        offset = 0

        while True:
            if offset > MAX_OFFSET_SAFETY:
                break

            try:
                page = await self.fetch_matches_page(match_type, PAGE_LIMIT, offset)
            except Exception:
                break

            items = page.get("items", [])
            if not items:
                break

            all_items.extend(items)

            if len(items) < PAGE_LIMIT:
                break

            offset += PAGE_LIMIT

        return all_items

    @staticmethod
    def normalize_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for raw in items:
            match_id = raw.get("match_id")
            status = raw.get("status", "unknown")

            scheduled_at = raw.get("scheduled_at")
            started_at = raw.get("started_at")
            finished_at = raw.get("finished_at")

            display_ts = scheduled_at if scheduled_at else started_at

            teams_block = raw.get("teams", {})
            factions: List[Dict[str, Any]] = [
                v for v in teams_block.values() if isinstance(v, dict)
            ]

            team_a_name = "Unknown"
            team_a_id = None
            team_b_name = "Unknown"
            team_b_id = None

            if len(factions) >= 1:
                fa = factions[0]
                team_a_name = fa.get("name", "Unknown")
                team_a_id = fa.get("faction_id") or fa.get("team_id") or fa.get("id")

            if len(factions) >= 2:
                fb = factions[1]
                team_b_name = fb.get("name", "Unknown")
                team_b_id = fb.get("faction_id") or fb.get("team_id") or fb.get("id")

            out.append(
                {
                    "match_id": match_id,
                    "status": status,
                    "when_local": _ts_to_local(display_ts),
                    "scheduled_local": _ts_to_local(scheduled_at),
                    "started_local": _ts_to_local(started_at),
                    "finished_local": _ts_to_local(finished_at),
                    "team_a_name": team_a_name,
                    "team_a_id": team_a_id,
                    "team_b_name": team_b_name,
                    "team_b_id": team_b_id,
                    "match_url": f"{FACEIT_ROOM_BASE}/{match_id}" if match_id else None,
                    "scheduled_at_unix": scheduled_at,
                    "started_at_unix": started_at,
                }
            )

        return out

    @staticmethod
    def filter_for_team(matches: List[Dict[str, Any]], team_id: str) -> List[Dict[str, Any]]:
        return [
            m for m in matches
            if m["team_a_id"] == team_id or m["team_b_id"] == team_id
        ]

    @staticmethod
    def sort_upcoming(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            matches,
            key=lambda m: (m["scheduled_at_unix"] is None, m["scheduled_at_unix"]),
        )

    @staticmethod
    def sort_past(matches: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            matches,
            key=lambda m: (m["started_at_unix"] is None, m["started_at_unix"]),
            reverse=True,
        )

    async def fetch_match_stats(self, match_id: str) -> Dict[str, Any]:
        url = f"{FACEIT_API_BASE}/matches/{match_id}/stats"
        return await self._get_json(url)

    @staticmethod
    def extract_crescent_players_from_stats(stats_json: Dict[str, Any], team_id: str) -> List[Dict[str, Any]]:
        per_player: Dict[str, Dict[str, Any]] = {}
        rounds_list = stats_json.get("rounds") or []

        for mp in rounds_list:
            round_stats = mp.get("round_stats", {}) or {}
            map_rounds = _to_int(round_stats.get("Rounds"))

            for t in (mp.get("teams", []) or []):
                tid = t.get("team_id") or t.get("faction_id") or t.get("id")
                if tid != team_id:
                    continue

                for p in (t.get("players", []) or []):
                    pid = p.get("player_id") or p.get("guid") or "unknown"
                    nick = p.get("nickname") or p.get("name") or pid
                    stats = p.get("player_stats") or p

                    kills     = _to_int(stats.get("Kills") or stats.get("kills"))
                    deaths    = _to_int(stats.get("Deaths") or stats.get("deaths"))
                    headshots = _to_int(stats.get("Headshots") or stats.get("headshots") or stats.get("HS"))
                    adr_val   = _to_float(stats.get("ADR") or stats.get("Average Damage per Round") or stats.get("Damage") or 0)

                    if pid not in per_player:
                        per_player[pid] = {
                            "player_id": pid,
                            "nickname": nick,
                            "kills": 0,
                            "deaths": 0,
                            "headshots": 0,
                            "rounds": 0,
                            "adr_weighted_sum": 0.0,
                        }

                    slot = per_player[pid]
                    slot["nickname"] = nick
                    slot["kills"] += kills
                    slot["deaths"] += deaths
                    slot["headshots"] += headshots
                    slot["rounds"] += map_rounds
                    if map_rounds > 0:
                        slot["adr_weighted_sum"] += adr_val * map_rounds

        out: List[Dict[str, Any]] = []
        for pid, slot in per_player.items():
            kills = slot["kills"]
            deaths = slot["deaths"]
            headshots = slot["headshots"]
            rounds_played = slot["rounds"]

            adr_avg = round(slot["adr_weighted_sum"] / rounds_played, 2) if rounds_played > 0 else 0.0
            kd_val = float(kills) if deaths == 0 else round(kills / deaths, 3)
            hs_pct_val = round((headshots / kills) * 100.0, 2) if kills > 0 else 0.0
            kpr_val = round(kills / rounds_played, 3) if rounds_played > 0 else 0.0

            out.append({
                "player_id": pid,
                "nickname": slot["nickname"],
                "kills": kills,
                "deaths": deaths,
                "headshots": headshots,
                "rounds": rounds_played,
                "adr": adr_avg,
                "kd": kd_val,
                "hs_pct": hs_pct_val,
                "kpr": kpr_val,
            })

        out.sort(key=lambda r: (-r["kills"], r["nickname"].lower()))
        return out

    @staticmethod
    def aggregate_season_totals(per_match: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        agg: Dict[str, Dict[str, Any]] = {}

        for match_entry in per_match:
            for p in match_entry.get("players", []):
                pid = p["player_id"]
                nick = p["nickname"]

                if pid not in agg:
                    agg[pid] = {
                        "player_id": pid,
                        "nickname": nick,
                        "matches_played": 0,
                        "kills": 0,
                        "deaths": 0,
                        "headshots": 0,
                        "rounds": 0,
                        "adr_weighted_sum": 0.0,
                    }

                slot = agg[pid]
                slot["nickname"] = nick
                slot["matches_played"] += 1

                k = int(p.get("kills", 0))
                d = int(p.get("deaths", 0))
                hs = int(p.get("headshots", 0))
                r = int(p.get("rounds", 0))
                adr_match_avg = _to_float(p.get("adr", 0.0))

                slot["kills"] += k
                slot["deaths"] += d
                slot["headshots"] += hs
                slot["rounds"] += r
                if r > 0:
                    slot["adr_weighted_sum"] += adr_match_avg * r

        for pid, slot in agg.items():
            kills_total = slot["kills"]
            deaths_total = slot["deaths"]
            hs_total = slot["headshots"]
            rounds_total = slot["rounds"]

            slot["kd_overall"] = float(kills_total) if deaths_total == 0 else round(kills_total / deaths_total, 3)
            slot["hs_pct_overall"] = round((hs_total / kills_total) * 100.0, 2) if kills_total > 0 else 0.0
            slot["adr_overall"] = round(slot["adr_weighted_sum"] / rounds_total, 2) if rounds_total > 0 else 0.0
            slot["kpr_overall"] = round(kills_total / rounds_total, 3) if rounds_total > 0 else 0.0

            del slot["adr_weighted_sum"]

        sorted_items = sorted(
            agg.items(),
            key=lambda kv: (-kv[1]["adr_overall"], kv[1]["nickname"].lower())
        )
        return dict(sorted_items)


# =========================
# DISCORD COG
# =========================

class EseaStats(commands.Cog):
    """
    /esea -> ADR sorted leaderboard for crescent in league play
    /upcoming -> next scheduled crescent matches

    Background task:
    - poll upcoming matches
    - if any match starts in ~30 min, ping ROLE_ID in ALERT_CHANNEL_ID
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None

        ############ alert state ############
        # match_ids we've already pinged about
        self.alerted_matches: Set[str] = set()
        #####################################

        # start background loop
        self.match_alert_loop.start()

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()

    async def cog_unload(self) -> None:
        # stop bg loop
        self.match_alert_loop.cancel()

        if self.session and not self.session.closed:
            await self.session.close()

    async def _build_api(self) -> EseaAPI:
        api_key = os.getenv("FACEIT_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("FACEIT_API_KEY is not set on the bot host.")
        assert self.session is not None
        return EseaAPI(self.session, api_key)

    async def _collect_upcoming_and_stats(self) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
        api = await self._build_api()

        # upcoming
        all_upcoming_raw = await api.collect_all_for_type("upcoming")
        upcoming_norm = api.normalize_items(all_upcoming_raw)
        upcoming_crescent = api.filter_for_team(upcoming_norm, TEAM_ID)
        upcoming_crescent_sorted = api.sort_upcoming(upcoming_crescent)

        # past
        all_past_raw = await api.collect_all_for_type("past")
        past_norm = api.normalize_items(all_past_raw)
        past_crescent = api.filter_for_team(past_norm, TEAM_ID)
        past_crescent_sorted = api.sort_past(past_crescent)

        # per-match player stats for crescent
        per_match_player_stats: List[Dict[str, Any]] = []

        for m in past_crescent_sorted:
            match_id = m.get("match_id")
            if not match_id:
                continue

            try:
                stats_json = await api.fetch_match_stats(match_id)
            except Exception:
                continue  # skip bad /stats calls

            players = api.extract_crescent_players_from_stats(stats_json, TEAM_ID)
            per_match_player_stats.append({
                "match_id": match_id,
                "players": players,
            })

            if REQUEST_DELAY_SEC > 0:
                await discord.utils.sleep_until(
                    dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=REQUEST_DELAY_SEC)
                )

        season_totals = api.aggregate_season_totals(per_match_player_stats)

        return upcoming_crescent_sorted, season_totals

    ############ BACKGROUND LOOP ############
    @tasks.loop(seconds=ALERT_LOOP_SECONDS)
    async def match_alert_loop(self):
        """
        Every ALERT_LOOP_SECONDS:
        - fetch upcoming crescent matches
        - find matches starting within ALERT_LEAD_MINUTES
        - if not yet alerted, ping role in ALERT_CHANNEL_ID
        """
        # don't run until the bot is ready
        if not self.bot.is_ready():
            return

        try:
            api = await self._build_api()

            all_upcoming_raw = await api.collect_all_for_type("upcoming")
            upcoming_norm = api.normalize_items(all_upcoming_raw)
            upcoming_crescent = api.filter_for_team(upcoming_norm, TEAM_ID)
            upcoming_sorted = api.sort_upcoming(upcoming_crescent)

            now_local = dt.datetime.now(ZoneInfo(OUTPUT_TZ))

            for m in upcoming_sorted:
                match_id = m.get("match_id")
                sched_unix = m.get("scheduled_at_unix")
                if not match_id or sched_unix is None:
                    continue

                sched_local = _unix_to_dt_local(sched_unix)
                if sched_local is None:
                    continue

                # compute time delta
                delta = sched_local - now_local
                minutes_until = delta.total_seconds() / 60.0

                # We alert if:
                #   0 <= minutes_until <= ALERT_LEAD_MINUTES
                #   and we haven't alerted this match yet
                if 0 <= minutes_until <= ALERT_LEAD_MINUTES:
                    if match_id not in self.alerted_matches:
                        # build mention message
                        # normalize team names so crescent appears first
                        team_a = m["team_a_name"] or "Unknown"
                        team_b = m["team_b_name"] or "Unknown"
                        if team_a.lower() == "crescent":
                            crescent_side = team_a
                            opp_side = team_b
                        elif team_b.lower() == "crescent":
                            crescent_side = team_b
                            opp_side = team_a
                        else:
                            crescent_side = team_a
                            opp_side = team_b

                        vs_text = f"{crescent_side} vs {opp_side}"
                        room_url = m.get("match_url") or ""

                        # timestamp pretty string
                        pretty_when = _ts_to_local(sched_unix)

                        channel = self.bot.get_channel(ALERT_CHANNEL_ID)
                        if isinstance(channel, discord.TextChannel):
                            # allow role mention explicitly
                            role_mention = f"<@&{ROLE_ID}>"
                            content = (
                                f"{role_mention} Match in {int(round(minutes_until))} minutes.\n"
                                f"{vs_text}\n"
                                f"Start: {pretty_when}\n"
                                f"{room_url}"
                            )

                            allowed = discord.AllowedMentions(
                                roles=True,
                                users=False,
                                everyone=False,
                            )

                            try:
                                await channel.send(content, allowed_mentions=allowed)
                                self.alerted_matches.add(match_id)
                            except Exception:
                                # swallow exceptions so the loop keeps running
                                pass

        except Exception:
            # swallow any fetch/parse failure so the loop does not die
            return

    @match_alert_loop.before_loop
    async def before_match_alert_loop(self):
        # wait until bot is ready before first iteration
        await self.bot.wait_until_ready()
    #########################################

    @guilds_decorator()
    @app_commands.command(
        name="esea",
        description="crescent league stats (KD, ADR, HS%, etc) for the current ESEA season"
    )
    async def esea(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        try:
            _upcoming, totals = await self._collect_upcoming_and_stats()
        except Exception as e:
            await interaction.followup.send(
                f"Error fetching data: {e}",
                ephemeral=True
            )
            return

        rows: List[Dict[str, Any]] = [v for _, v in totals.items()]

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
                f"{_fmt_safe(r['kd_overall']):>{kd_w}}  "
                f"{_fmt_safe(r['adr_overall']):>{adr_w}}  "
                f"{_fmt_safe(r['hs_pct_overall']):>{hs_w}}  "
                f"{_fmt_safe(r['kpr_overall']):>{kpr_w}}  "
                f"{r['rounds']:>{rnd_w}}"
            )

        leaderboard_block = "```text\n" + "\n".join(lines) + "\n```"

        embed = discord.Embed(
            title="crescent <:crescent:855175620891508736> • ESEA Main stats",
            description=leaderboard_block,
            color=0x0c9547
        )
        embed.set_footer(text="Source: FACEIT Data API (ESEA season only)")
        await interaction.followup.send(embed=embed)

    @guilds_decorator()
    @app_commands.command(
        name="upcoming",
        description="next scheduled crescent league matches"
    )
    async def upcoming(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        try:
            upcoming, _totals = await self._collect_upcoming_and_stats()
        except Exception as e:
            await interaction.followup.send(
                f"Error fetching data: {e}",
                ephemeral=True
            )
            return

        if not upcoming:
            await interaction.followup.send(
                "No upcoming matches scheduled for crescent.",
                ephemeral=False
            )
            return

        bullets: List[str] = []
        for m in upcoming:
            team_a = m["team_a_name"] or "Unknown"
            team_b = m["team_b_name"] or "Unknown"

            if team_a.lower() == "crescent":
                crescent_side = team_a
                opp_side = team_b
            elif team_b.lower() == "crescent":
                crescent_side = team_b
                opp_side = team_a
            else:
                crescent_side = team_a
                opp_side = team_b

            vs_text = f"{crescent_side} vs {opp_side}"

            pretty_when = m["when_local"]
            try:
                raw_dt, raw_tz = pretty_when.rsplit(" ", 1)
                raw_date, raw_time = raw_dt.split(" ")
                year, month, day = raw_date.split("-")
                hour, minute = raw_time.split(":")[:2]

                dt_obj = dt.datetime(
                    year=int(year),
                    month=int(month),
                    day=int(day),
                    hour=int(hour),
                    minute=int(minute),
                )
                pretty_when = dt_obj.strftime("%a %b %d, %Y ") + f"{hour}:{minute} {raw_tz}"
            except Exception:
                pass

            room_url = m["match_url"] or ""
            bullet_line = f"• {pretty_when} [{vs_text}]({room_url})"
            bullets.append(bullet_line)

        embed = discord.Embed(
            title=f"crescent <:crescent:855175620891508736> • upcoming matches",
            description="\n".join(bullets),
            color=0x0c9547
        )
        embed.set_footer(text="Times shown in " + OUTPUT_TZ)
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(EseaStats(bot))
