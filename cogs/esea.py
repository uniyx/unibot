# cogs/esea.py
import os
import asyncio
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple, Set

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo

# =========================
# CONFIG
# =========================
OUTPUT_TZ       = "America/New_York"
TEAM_ID         = "15c9a36f-8169-49eb-a41b-0a0e7567ed37"      # crescent
CHAMPIONSHIP_ID = "c5749517-d0b9-4d12-aec1-329393db934b"      # ESEA S55 NA Main Central

FACEIT_PUBLIC_V1  = "https://www.faceit.com/api"
FACEIT_ROOM_BASE  = "https://www.faceit.com/en/cs2/room"

THEME_COLOR = 0x0c9547
TITLE_BASE  = "crescent <:crescent:855175620891508736>"

# Guild scoping
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)

# Alerts
ROLE_ID             = 1432855602224304258      # role to ping
ALERT_CHANNEL_ID    = 1430384084802211952      # channel to send alerts
ALERT_LEAD_MINUTES  = 30

REQUEST_TIMEOUT     = 20.0
REFRESH_HOURS       = 3  # eight times a day

# Idempotency controls
ALERT_GRACE_SECONDS = 90          # after fire time, do not send again past this window
RESCHEDULE_JITTER_S = 2           # if an existing task is due within this, keep it

# =========================
# UTILITIES
# =========================
def _ms_to_local(ms: Optional[int]) -> str:
    if not ms:
        return "TBD"
    dtu = dt.datetime.fromtimestamp(int(ms) / 1000.0, dt.timezone.utc)
    return dtu.astimezone(ZoneInfo(OUTPUT_TZ)).strftime("%a %b %d, %Y %H:%M %Z")

def _to_local_dt(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).astimezone(ZoneInfo(OUTPUT_TZ))

def _fmt_delta(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    s = abs(int(seconds))
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{sign}{h}h {m}m {s}s"
    if m:
        return f"{sign}{m}m {s}s"
    return f"{sign}{s}s"

# =========================
# FACEIT CLIENT (Public v1 only)
# =========================
class FaceitV1Client:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self._name_cache: Dict[str, str] = {}

    def _headers(self) -> Dict[str, str]:
        return {"Accept": "application/json"}

    async def fetch_team_fixtures(self, team_id: str, champ_id: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        offset = 0
        limit = 70
        while True:
            params = {
                "participantId": team_id,
                "participantType": "TEAM",
                "championshipId": champ_id,
                "limit": str(limit),
                "offset": str(offset),
                "sort": "ASC",
            }
            url = f"{FACEIT_PUBLIC_V1}/championships/v1/matches"
            async with self.session.get(url, params=params, headers=self._headers(), timeout=REQUEST_TIMEOUT) as resp:
                if resp.status in (404, 400):
                    break
                if resp.status != 200:
                    break
                data = await resp.json()
                page = (data.get("payload") or {}).get("items") or []
                if not page:
                    break
                items.extend(page)
                if len(page) < limit:
                    break
                offset += limit
        items.sort(key=lambda m: (m.get("origin", {}).get("schedule", 0)))
        return items

    async def team_name(self, team_id: str) -> str:
        if team_id in self._name_cache:
            return self._name_cache[team_id]
        for path in (f"/teams/v1/teams/{team_id}", f"/teams/v1/teams/{team_id}/profile"):
            url = f"{FACEIT_PUBLIC_V1}{path}"
            try:
                async with self.session.get(url, headers=self._headers(), timeout=REQUEST_TIMEOUT) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    name = (
                        str(data.get("name") or data.get("nickname") or "") or
                        str((data.get("payload") or {}).get("name") or (data.get("payload") or {}).get("nickname") or "") or
                        str(((data.get("payload") or {}).get("team") or {}).get("name") or ((data.get("payload") or {}).get("team") or {}).get("nickname") or "")
                    ).strip()
                    if name:
                        self._name_cache[team_id] = name
                        return name
            except Exception:
                continue
        self._name_cache[team_id] = team_id
        return team_id

    async def compute_record(self, team_id: str, champ_id: str) -> Tuple[int, int]:
        fixtures = await self.fetch_team_fixtures(team_id, champ_id)
        wins = losses = 0
        for m in fixtures:
            if str(m.get("status", "")).lower() != "finished":
                continue
            winner = str(m.get("winner") or "")
            if not winner:
                continue
            if winner == team_id:
                wins += 1
            else:
                losses += 1
        return wins, losses

    async def upcoming_created(self, team_id: str, champ_id: str) -> List[Dict[str, Any]]:
        fixtures = await self.fetch_team_fixtures(team_id, champ_id)
        upcoming = [m for m in fixtures if str(m.get("status", "")).lower() == "created"]

        out: List[Dict[str, Any]] = []
        for m in upcoming:
            factions = m.get("factions") or []
            if len(factions) != 2:
                continue
            a_id = str(factions[0].get("id"))
            b_id = str(factions[1].get("id"))
            opp_id = a_id if a_id != team_id else b_id

            origin = m.get("origin") or {}
            room_id = str(origin.get("id") or "")
            sched_ms = origin.get("schedule")

            try:
                opp_name = await self.team_name(opp_id)
            except Exception:
                opp_name = opp_id

            out.append({
                "match_id": room_id,
                "scheduled_ms": sched_ms,
                "scheduled_local": _ms_to_local(sched_ms),
                "opponent_id": opp_id,
                "opponent_name": opp_name,
                "match_url": f"{FACEIT_ROOM_BASE}/{room_id}" if room_id else "",
            })

        out.sort(key=lambda x: (x["scheduled_ms"] is None, x["scheduled_ms"]))
        return out

# =========================
# DISCORD COG
# =========================
class EseaUpcoming(commands.Cog):
    """
    Slow producer that reconciles upcoming matches and schedules one-shot alerts.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._api: Optional[FaceitV1Client] = None

        # match_id -> asyncio.Task
        self._scheduled: Dict[str, asyncio.Task] = {}
        # match_id -> metadata for debug
        self._scheduled_meta: Dict[str, Dict[str, Any]] = {}
        # match_id -> unix seconds when we sent the alert
        self._fired: Dict[str, int] = {}

        # mutex to guard double send in racy reschedules
        self._send_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        self._api = FaceitV1Client(self.session)
        await self._reconcile_schedule()
        self.upcoming_refresh_loop.start()

    async def cog_unload(self) -> None:
        self.upcoming_refresh_loop.cancel()
        for t in list(self._scheduled.values()):
            t.cancel()
        self._scheduled.clear()
        self._scheduled_meta.clear()
        if self.session and not self.session.closed:
            await self.session.close()

    def _alert_time_seconds(self, sched_ms: int) -> float:
        alert_at = _to_local_dt(sched_ms) - dt.timedelta(minutes=ALERT_LEAD_MINUTES)
        return (alert_at - dt.datetime.now(ZoneInfo(OUTPUT_TZ))).total_seconds()

    def _schedule_alert_if_needed(self, match: Dict[str, Any]) -> None:
        match_id = match.get("match_id")
        sched_ms = match.get("scheduled_ms")
        if not match_id or not sched_ms:
            return

        seconds_until = self._alert_time_seconds(sched_ms)
        now_unix = int(dt.datetime.now(ZoneInfo(OUTPUT_TZ)).timestamp())

        # Already fired or far past the window: do nothing
        if match_id in self._fired:
            return
        if seconds_until < -ALERT_GRACE_SECONDS:
            # Mark as fired so we will not chase it anymore
            self._fired[match_id] = now_unix
            self._scheduled.pop(match_id, None)
            self._scheduled_meta.pop(match_id, None)
            return

        # Refresh metadata for debug
        self._scheduled_meta[match_id] = {
            "scheduled_ms": sched_ms,
            "scheduled_local": match.get("scheduled_local", "TBD"),
            "opponent_name": match.get("opponent_name", "Unknown"),
            "match_url": match.get("match_url", ""),
        }

        async def _wait_and_ping(initial_delay: float):
            try:
                if initial_delay > 0:
                    await asyncio.sleep(initial_delay)

                async with self._send_lock:
                    # Double check idempotency at the send site
                    if match_id in self._fired:
                        return
                    channel = self.bot.get_channel(ALERT_CHANNEL_ID)
                    if isinstance(channel, discord.TextChannel):
                        role_mention = f"<@&{ROLE_ID}>"
                        content = (
                            f"{role_mention} Match in {ALERT_LEAD_MINUTES} minutes.\n"
                            f"crescent vs {match.get('opponent_name', 'Unknown')}\n"
                            f"Start: {match.get('scheduled_local', 'TBD')}\n"
                            f"{match.get('match_url', '')}"
                        )
                        allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)
                        await channel.send(content, allowed_mentions=allowed)
                        self._fired[match_id] = int(dt.datetime.now(ZoneInfo(OUTPUT_TZ)).timestamp())
            except asyncio.CancelledError:
                return
            except Exception:
                return
            finally:
                self._scheduled.pop(match_id, None)
                self._scheduled_meta.pop(match_id, None)

        # If a task exists and is about to fire, keep it to avoid churn
        existing = self._scheduled.get(match_id)
        if existing and not existing.done():
            # If the remaining time is essentially the same, do not replace
            # Otherwise, replace with the new timing
            # We cannot read its remaining time directly, so use a small jitter rule
            if abs(seconds_until) <= RESCHEDULE_JITTER_S:
                return
            existing.cancel()
            self._scheduled.pop(match_id, None)

        delay = max(seconds_until, 0.0)
        task = asyncio.create_task(_wait_and_ping(delay), name=f"esea_alert_{match_id}")
        self._scheduled[match_id] = task

    async def _reconcile_schedule(self) -> None:
        if not self._api:
            return
        try:
            upcoming = await self._api.upcoming_created(TEAM_ID, CHAMPIONSHIP_ID)
        except Exception:
            return

        seen_ids: Set[str] = set()
        for m in upcoming:
            mid = m.get("match_id", "")
            seen_ids.add(mid)
            self._schedule_alert_if_needed(m)

        # cancel tasks for matches no longer upcoming
        for mid, task in list(self._scheduled.items()):
            if mid not in seen_ids:
                task.cancel()
                self._scheduled.pop(mid, None)
                self._scheduled_meta.pop(mid, None)

        # garbage collect fired entries older than 24 hours
        cutoff = int((dt.datetime.now(ZoneInfo(OUTPUT_TZ)) - dt.timedelta(hours=24)).timestamp())
        for mid, ts in list(self._fired.items()):
            if ts < cutoff and mid not in seen_ids:
                self._fired.pop(mid, None)

    # ---------- Slash: /upcoming ----------
    @guilds_decorator()
    @app_commands.command(
        name="upcoming",
        description="Next scheduled crescent league matches (also reconciles alert scheduling)"
    )
    async def upcoming(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        assert self._api is not None
        try:
            await self._reconcile_schedule()
            upcoming = await self._api.upcoming_created(TEAM_ID, CHAMPIONSHIP_ID)
            crescent_w, crescent_l = await self._api.compute_record(TEAM_ID, CHAMPIONSHIP_ID)
        except Exception as e:
            await interaction.followup.send(f"Error fetching data: {e}", ephemeral=True)
            return

        if not upcoming:
            await interaction.followup.send("No upcoming matches scheduled for crescent.", ephemeral=False)
            return

        bullets: List[str] = []
        for m in upcoming:
            opp_id = m["opponent_id"]
            opp_name = m["opponent_name"] or "Unknown"
            try:
                opp_w, opp_l = await self._api.compute_record(opp_id, CHAMPIONSHIP_ID)
            except Exception:
                opp_w = opp_l = 0

            bullets.append(
                f"• {m['scheduled_local']} [crescent ({crescent_w}W - {crescent_l}L) vs {opp_name} ({opp_w}W - {opp_l}L)]({m['match_url']})"
            )

        embed = discord.Embed(
            title=f"{TITLE_BASE} ({crescent_w}W - {crescent_l}L) • upcoming matches",
            description="\n".join(bullets),
            color=THEME_COLOR
        )
        embed.set_footer(text="Times shown in " + OUTPUT_TZ)
        await interaction.followup.send(embed=embed)

    # ---------- Slash: /alerts ----------
    @guilds_decorator()
    @app_commands.command(
        name="alerts",
        description="Debug: show which matches currently have an alert scheduled"
    )
    @app_commands.describe(refresh="If true, fetch FACEIT now to reconcile before listing.")
    async def alerts(self, interaction: discord.Interaction, refresh: Optional[bool] = False):
        await interaction.response.defer(thinking=True)
        if refresh:
            try:
                await self._reconcile_schedule()
            except Exception as e:
                await interaction.followup.send(f"Reconcile failed: {e}", ephemeral=True)
                return

        if not self._scheduled_meta:
            await interaction.followup.send("No alert tasks are currently scheduled.", ephemeral=True)
            return

        items: List[Tuple[int, str]] = []
        for meta in self._scheduled_meta.values():
            ms = meta.get("scheduled_ms")
            if isinstance(ms, int):
                alert_dt = _to_local_dt(ms) - dt.timedelta(minutes=ALERT_LEAD_MINUTES)
                items.append((int(alert_dt.timestamp()), str(meta.get("opponent_name", "Unknown"))))

        if not items:
            await interaction.followup.send("No alert tasks are currently scheduled.", ephemeral=True)
            return

        items.sort(key=lambda x: x[0])
        lines = [f"• {opp} <t:{unix}:R>" for unix, opp in items]

        embed = discord.Embed(
            title=f"{TITLE_BASE} • alert countdowns",
            description="\n".join(lines),
            color=THEME_COLOR
        )
        embed.set_footer(text=f"Lead time {ALERT_LEAD_MINUTES} min • tasks={len(items)} • Times shown via Discord relative timestamps")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ---------- Background ----------
    @tasks.loop(hours=REFRESH_HOURS)
    async def upcoming_refresh_loop(self):
        if not self.bot.is_ready():
            return
        await self._reconcile_schedule()

    @upcoming_refresh_loop.before_loop
    async def before_upcoming_refresh_loop(self):
        await self.bot.wait_until_ready()
        await self._reconcile_schedule()

async def setup(bot: commands.Bot):
    await bot.add_cog(EseaUpcoming(bot))
