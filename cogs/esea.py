# cogs/esea.py
import os
import re
import asyncio
import sqlite3
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
CHAMPIONSHIP_ID = "f5856452-1bea-458d-acf2-69ab4d512f75"      # ESEA S56 NA Main Central

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
RESCHEDULE_JITTER_S = 2           # if alert time changes within this, keep existing task

# SQLite
ESEA_DB_PATH = os.getenv("ESEA_DB_PATH", "data/esea.sqlite3")

# Garbage collection
STALE_SEEN_HOURS = 24

# =========================
# UTILITIES
# =========================

def _now_local() -> dt.datetime:
    return dt.datetime.now(ZoneInfo(OUTPUT_TZ))

def _now_unix() -> int:
    return int(_now_local().timestamp())

def _ms_to_local(ms: Optional[int]) -> str:
    if not ms:
        return "TBD"
    dtu = dt.datetime.fromtimestamp(int(ms) / 1000.0, dt.timezone.utc)
    return dtu.astimezone(ZoneInfo(OUTPUT_TZ)).strftime("%a %b %d, %Y %H:%M %Z")

def _to_local_dt(ms: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc).astimezone(ZoneInfo(OUTPUT_TZ))

def _alert_at_unix(sched_ms: int) -> int:
    # alert time is schedule minus lead minutes
    start_unix = int(sched_ms // 1000)
    return start_unix - int(ALERT_LEAD_MINUTES * 60)

def _seconds_until(unix_ts: int) -> float:
    return float(unix_ts - _now_unix())

# =========================
# SQLITE HELPERS
# =========================

class EseaDbError(RuntimeError):
    pass

def _ensure_db_dir(path: str) -> None:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)

def _connect_db(path: str) -> sqlite3.Connection:
    _ensure_db_dir(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS esea_alerts (
            match_id TEXT PRIMARY KEY,
            team_id TEXT NOT NULL,
            champ_id TEXT NOT NULL,
            scheduled_ms INTEGER,
            alert_at_unix INTEGER,
            opponent_name TEXT,
            match_url TEXT,
            last_seen_unix INTEGER NOT NULL,
            fired_unix INTEGER
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_due ON esea_alerts(alert_at_unix)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen ON esea_alerts(last_seen_unix)")
    conn.commit()

def _upsert_match(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    team_id: str,
    champ_id: str,
    scheduled_ms: Optional[int],
    alert_at_unix: Optional[int],
    opponent_name: str,
    match_url: str,
    last_seen_unix: int,
) -> None:
    conn.execute(
        """
        INSERT INTO esea_alerts (
            match_id, team_id, champ_id, scheduled_ms, alert_at_unix,
            opponent_name, match_url, last_seen_unix, fired_unix
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
        ON CONFLICT(match_id) DO UPDATE SET
            scheduled_ms   = excluded.scheduled_ms,
            alert_at_unix  = excluded.alert_at_unix,
            opponent_name  = excluded.opponent_name,
            match_url      = excluded.match_url,
            last_seen_unix = excluded.last_seen_unix
        """,
        (
            match_id, team_id, champ_id,
            scheduled_ms, alert_at_unix,
            opponent_name, match_url,
            last_seen_unix,
        )
    )

def _mark_fired(conn: sqlite3.Connection, match_id: str, fired_unix: int) -> None:
    conn.execute(
        "UPDATE esea_alerts SET fired_unix = ? WHERE match_id = ?",
        (fired_unix, match_id),
    )

def _delete_match(conn: sqlite3.Connection, match_id: str) -> None:
    conn.execute("DELETE FROM esea_alerts WHERE match_id = ?", (match_id,))

def _get_pending_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT match_id, scheduled_ms, alert_at_unix, opponent_name, match_url, fired_unix
        FROM esea_alerts
        WHERE fired_unix IS NULL
          AND alert_at_unix IS NOT NULL
        """
    ).fetchall()

    out: List[Dict[str, Any]] = []
    for (mid, sched_ms, alert_at, opp, url, fired) in rows:
        out.append({
            "match_id": mid,
            "scheduled_ms": sched_ms,
            "alert_at_unix": alert_at,
            "opponent_name": opp or "Unknown",
            "match_url": url or "",
            "fired_unix": fired,
        })
    return out

def _get_upcoming_rows(conn: sqlite3.Connection, limit: int = 10) -> List[Dict[str, Any]]:
    now = _now_unix()
    rows = conn.execute(
        """
        SELECT match_id, scheduled_ms, alert_at_unix, opponent_name, match_url, fired_unix
        FROM esea_alerts
        WHERE scheduled_ms IS NOT NULL
          AND (scheduled_ms / 1000) > ?
        ORDER BY scheduled_ms ASC
        LIMIT ?
        """,
        (now, int(limit)),
    ).fetchall()

    out: List[Dict[str, Any]] = []
    for (mid, sched_ms, alert_at, opp, url, fired) in rows:
        out.append({
            "match_id": mid,
            "scheduled_ms": sched_ms,
            "alert_at_unix": alert_at,
            "opponent_name": opp or "Unknown",
            "match_url": url or "",
            "fired_unix": fired,
        })
    return out

def _gc_stale(conn: sqlite3.Connection) -> None:
    cutoff = _now_unix() - int(STALE_SEEN_HOURS * 3600)
    conn.execute(
        """
        DELETE FROM esea_alerts
        WHERE last_seen_unix < ?
          AND (fired_unix IS NOT NULL OR alert_at_unix IS NULL)
        """,
        (cutoff,),
    )
    conn.commit()

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
    Durable state in SQLite, transient execution as asyncio tasks.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: Optional[aiohttp.ClientSession] = None
        self._api: Optional[FaceitV1Client] = None

        self._conn = _connect_db(ESEA_DB_PATH)
        _ensure_schema(self._conn)

        # match_id -> asyncio.Task (execution only)
        self._scheduled: Dict[str, asyncio.Task] = {}
        self._send_lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()

    async def cog_load(self) -> None:
        self.session = aiohttp.ClientSession()
        self._api = FaceitV1Client(self.session)

        # Rehydrate from DB first (restart-safe), then reconcile from network.
        await self._rehydrate_from_db()
        await self._reconcile_schedule()

        self.upcoming_refresh_loop.start()

    async def cog_unload(self) -> None:
        self.upcoming_refresh_loop.cancel()

        for t in list(self._scheduled.values()):
            t.cancel()
        self._scheduled.clear()

        if self.session and not self.session.closed:
            await self.session.close()

        try:
            self._conn.close()
        except Exception:
            pass

    # =========================
    # ALERT SCHEDULING (DB-backed)
    # =========================

    def _should_send_now(self, alert_at_unix: int) -> bool:
        # Send if within grace window around alert time.
        # If it is too far in the past, we treat as missed and mark fired to prevent noise.
        delta = _seconds_until(alert_at_unix)
        return (-ALERT_GRACE_SECONDS <= delta <= 0.0) or (delta > 0.0)

    def _delay_seconds(self, alert_at_unix: int) -> float:
        return max(_seconds_until(alert_at_unix), 0.0)

    async def _send_alert(self, row: Dict[str, Any]) -> None:
        """
        Send alert exactly once, then mark fired in DB.
        """
        match_id = row["match_id"]
        alert_at_unix = int(row["alert_at_unix"])
        sched_ms = row.get("scheduled_ms")
        opponent = row.get("opponent_name") or "Unknown"
        match_url = row.get("match_url") or ""

        # If the bot comes back online long after alert time, do not spam.
        if _seconds_until(alert_at_unix) < -ALERT_GRACE_SECONDS:
            async with self._db_lock:
                _mark_fired(self._conn, match_id, _now_unix())
                self._conn.commit()
            return

        async with self._send_lock:
            # Recheck DB idempotency at the send site.
            async with self._db_lock:
                fired = self._conn.execute(
                    "SELECT fired_unix FROM esea_alerts WHERE match_id = ?",
                    (match_id,),
                ).fetchone()
                if not fired:
                    return
                if fired[0] is not None:
                    return

            channel = self.bot.get_channel(ALERT_CHANNEL_ID)
            if not isinstance(channel, discord.TextChannel):
                return

            role_mention = f"<@&{ROLE_ID}>"

            start_unix = int(sched_ms // 1000) if isinstance(sched_ms, int) else None
            start_line = f"<t:{start_unix}:f> (<t:{start_unix}:R>)" if start_unix else "TBD"

            embed = discord.Embed(
                title="ESEA Match Alert",
                description=f"Match in **{ALERT_LEAD_MINUTES} minutes**",
                color=THEME_COLOR,
            )
            embed.add_field(name="Match", value=f"crescent vs **{opponent}**", inline=False)
            embed.add_field(name="Start", value=start_line, inline=True)
            if match_url:
                embed.add_field(name="Room", value=match_url, inline=False)
            embed.set_footer(text=f"championship: {CHAMPIONSHIP_ID}")

            allowed = discord.AllowedMentions(roles=True, users=False, everyone=False)
            try:
                await channel.send(content=role_mention, embed=embed, allowed_mentions=allowed)
            except Exception:
                return

            async with self._db_lock:
                _mark_fired(self._conn, match_id, _now_unix())
                self._conn.commit()

    def _schedule_task_for_row(self, row: Dict[str, Any]) -> None:
        match_id = row["match_id"]
        alert_at_unix = row.get("alert_at_unix")
        if not match_id or alert_at_unix is None:
            return

        alert_at_unix = int(alert_at_unix)

        # If already scheduled, keep unless timing changed materially.
        existing = self._scheduled.get(match_id)
        if existing and not existing.done():
            # We do not have remaining time, so compare against DB truth by rescheduling only
            # when we detect a non-trivial shift at reconcile time.
            return

        async def _runner(delay: float):
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                await self._send_alert(row)
            except asyncio.CancelledError:
                return
            finally:
                self._scheduled.pop(match_id, None)

        delay = self._delay_seconds(alert_at_unix)
        self._scheduled[match_id] = asyncio.create_task(_runner(delay), name=f"esea_alert_{match_id}")

    async def _rehydrate_from_db(self) -> None:
        async with self._db_lock:
            rows = _get_pending_rows(self._conn)

        # Schedule what is pending; anything too stale gets marked fired in _send_alert.
        for r in rows:
            self._schedule_task_for_row(r)

    # =========================
    # RECONCILIATION
    # =========================

    async def _reconcile_schedule(self) -> None:
        if not self._api:
            return

        try:
            upcoming = await self._api.upcoming_created(TEAM_ID, CHAMPIONSHIP_ID)
        except Exception:
            return

        now = _now_unix()
        seen_ids: Set[str] = set()

        async with self._db_lock:
            for m in upcoming:
                match_id = m.get("match_id") or ""
                sched_ms = m.get("scheduled_ms")
                if not match_id:
                    continue

                seen_ids.add(match_id)

                alert_unix: Optional[int] = None
                if isinstance(sched_ms, int):
                    alert_unix = _alert_at_unix(sched_ms)

                _upsert_match(
                    self._conn,
                    match_id=match_id,
                    team_id=TEAM_ID,
                    champ_id=CHAMPIONSHIP_ID,
                    scheduled_ms=sched_ms if isinstance(sched_ms, int) else None,
                    alert_at_unix=alert_unix,
                    opponent_name=str(m.get("opponent_name") or "Unknown"),
                    match_url=str(m.get("match_url") or ""),
                    last_seen_unix=now,
                )

            self._conn.commit()
            _gc_stale(self._conn)

            # Pull fresh pending rows to schedule and to detect timing shifts.
            pending = _get_pending_rows(self._conn)

        # Cancel tasks for matches that are no longer upcoming and not pending in DB.
        pending_ids = {r["match_id"] for r in pending}
        for mid, task in list(self._scheduled.items()):
            if mid not in pending_ids:
                task.cancel()
                self._scheduled.pop(mid, None)

        # Schedule all pending rows.
        for r in pending:
            # If timing shifts materially, cancel and reschedule.
            existing = self._scheduled.get(r["match_id"])
            if existing and not existing.done():
                # Compare current scheduled delay vs new delay; if within jitter, keep.
                new_delay = self._delay_seconds(int(r["alert_at_unix"]))
                if new_delay <= RESCHEDULE_JITTER_S:
                    continue
                existing.cancel()
                self._scheduled.pop(r["match_id"], None)
            self._schedule_task_for_row(r)

        # If a match disappeared from API, we do not delete immediately.
        # We mark last_seen and allow GC to clean it after 24h if it remains irrelevant.
        # That avoids thrashing on temporary API weirdness.

    # =========================
    # SLASH COMMAND: /upcoming
    # =========================

    @guilds_decorator()
    @app_commands.command(
        name="upcoming",
        description="Show upcoming crescent league matches and whether alerts are scheduled."
    )
    async def upcoming(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)

        if not self._api:
            await interaction.followup.send("API client not ready.", ephemeral=True)
            return

        # Reconcile so DB and tasks represent current truth.
        await self._reconcile_schedule()

        try:
            crescent_w, crescent_l = await self._api.compute_record(TEAM_ID, CHAMPIONSHIP_ID)
        except Exception:
            crescent_w, crescent_l = 0, 0

        async with self._db_lock:
            rows = _get_upcoming_rows(self._conn, limit=10)

        if not rows:
            await interaction.followup.send("No upcoming matches scheduled for crescent.", ephemeral=False)
            return

        lines: List[str] = []
        now = _now_unix()

        for r in rows:
            opp = r.get("opponent_name") or "Unknown"
            url = r.get("match_url") or ""
            sched_ms = r.get("scheduled_ms")
            fired = r.get("fired_unix")

            if isinstance(sched_ms, int):
                start_unix = int(sched_ms // 1000)
                match_rel = f"<t:{start_unix}:R>"
            else:
                start_unix = None
                match_rel = "TBD"

            alert_at = r.get("alert_at_unix")
            # Scheduled if we have an alert time and it has not been fired and it is not way past grace.
            if fired is not None:
                alert_flag = "✅"  # already fired, which is good from an idempotency perspective
            elif alert_at is None:
                alert_flag = "❌"
            else:
                # If it is far in the past, it will be marked fired on next poller execution.
                delta = alert_at - now
                alert_flag = "✅" if delta >= -ALERT_GRACE_SECONDS else "❌"

            if url:
                lines.append(f"• **{opp}** • {match_rel} • alert: {alert_flag} • [room]({url})")
            else:
                lines.append(f"• **{opp}** • {match_rel} • alert: {alert_flag}")

        embed = discord.Embed(
            title=f"{TITLE_BASE} ({crescent_w}W - {crescent_l}L) • upcoming matches",
            description="\n".join(lines),
            color=THEME_COLOR,
        )
        embed.set_footer(text=f"Lead time {ALERT_LEAD_MINUTES} min • durable alerts via SQLite")
        await interaction.followup.send(embed=embed, ephemeral=False)

    # =========================
    # BACKGROUND
    # =========================

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
