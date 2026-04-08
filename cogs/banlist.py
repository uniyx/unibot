import asyncio
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import discord
import requests
from curl_cffi import requests as curl_requests
from discord import app_commands
from discord.ext import commands


DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
BANLIST_DB_PATH = os.getenv("BANLIST_DB_PATH", "data/faceit_banlist.sqlite3")
GAME = "cs2"
THEME_COLOR = 0xFF5500


def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)


class ApiCounter:
    def __init__(self) -> None:
        self._count = 0
        self._lock = threading.Lock()

    def inc(self) -> None:
        with self._lock:
            self._count += 1

    @property
    def value(self) -> int:
        with self._lock:
            return self._count


def _ensure_db_dir(path: str) -> None:
    db_dir = os.path.dirname(path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)


def _connect_db(path: str) -> sqlite3.Connection:
    _ensure_db_dir(path)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS faceit_banlist (
            player_id TEXT PRIMARY KEY,
            last_known_nickname TEXT NOT NULL,
            added_at_unix INTEGER NOT NULL,
            updated_at_unix INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_faceit_banlist_nickname ON faceit_banlist(last_known_nickname)"
    )
    conn.commit()


def _now_unix() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _upsert_player(conn: sqlite3.Connection, player_id: str, nickname: str) -> bool:
    now = _now_unix()
    existing = conn.execute(
        "SELECT 1 FROM faceit_banlist WHERE player_id = ?",
        (player_id,),
    ).fetchone()

    conn.execute(
        """
        INSERT INTO faceit_banlist (player_id, last_known_nickname, added_at_unix, updated_at_unix)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(player_id) DO UPDATE SET
            last_known_nickname = excluded.last_known_nickname,
            updated_at_unix = excluded.updated_at_unix
        """,
        (player_id, nickname, now, now),
    )
    conn.commit()
    return existing is None


def _touch_player_nickname(conn: sqlite3.Connection, player_id: str, nickname: str) -> None:
    conn.execute(
        """
        UPDATE faceit_banlist
        SET last_known_nickname = ?, updated_at_unix = ?
        WHERE player_id = ?
        """,
        (nickname, _now_unix(), player_id),
    )
    conn.commit()


def _get_banlist_players(conn: sqlite3.Connection) -> List[Dict[str, str]]:
    rows = conn.execute(
        """
        SELECT player_id, last_known_nickname
        FROM faceit_banlist
        ORDER BY LOWER(last_known_nickname) ASC
        """
    ).fetchall()
    return [
        {"player_id": str(row[0]), "nickname": str(row[1])}
        for row in rows
    ]


def get_player_by_nickname(api_key: str, nickname: str, counter: ApiCounter) -> dict:
    counter.inc()

    url = f"https://open.faceit.com/data/v4/players?nickname={quote(nickname)}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_player_by_id(api_key: str, player_id: str, counter: ApiCounter) -> dict:
    counter.inc()

    url = f"https://open.faceit.com/data/v4/players/{quote(player_id)}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def get_grouped_matches(player_id: str, counter: ApiCounter) -> dict:
    counter.inc()

    url = (
        "https://www.faceit.com/api/match/v4/matches/groupByState"
        f"?userId={quote(player_id)}"
    )
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.faceit.com/",
        "Origin": "https://www.faceit.com",
    }

    resp = curl_requests.get(
        url,
        headers=headers,
        impersonate="chrome136",
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def find_ongoing_match(grouped_data: dict) -> dict | None:
    payload = grouped_data.get("payload", {})

    for state_name, matches in payload.items():
        if "ongoing" in str(state_name).lower():
            return matches[0] if matches else None

        if isinstance(matches, list):
            for match in matches:
                status = str(match.get("status", "")).lower()
                state = str(match.get("state", "")).lower()
                if "ongoing" in status or "ongoing" in state:
                    return match

    return None


def get_recent_match_id(api_key: str, player_id: str, counter: ApiCounter) -> str | None:
    counter.inc()

    url = (
        f"https://open.faceit.com/data/v4/players/{player_id}/history"
        f"?game={GAME}&offset=0&limit=1"
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    items = data.get("items", [])
    if not items:
        return None

    return items[0].get("match_id")


def get_match_details(api_key: str, match_id: str, counter: ApiCounter) -> dict:
    counter.inc()

    url = f"https://open.faceit.com/data/v4/matches/{quote(match_id)}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()


def to_iso8601_utc(value: object) -> str | None:
    if value is None:
        return None

    if isinstance(value, str):
        return value

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    return str(value)


def parse_iso8601_utc(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)

    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def to_discord_relative(value: str | None) -> str:
    if not value:
        return "unknown"
    dt_value = parse_iso8601_utc(value)
    if dt_value == datetime.min.replace(tzinfo=timezone.utc):
        return "unknown"
    return f"<t:{int(dt_value.timestamp())}:R>"


def build_player_status(api_key: str, player_id: str, counter: ApiCounter) -> dict:
    player = get_player_by_id(api_key, player_id, counter)
    resolved_name = player.get("nickname") or player_id
    resolved_player_id = player.get("player_id") or player.get("id") or player.get("user_id") or player_id

    grouped_data = get_grouped_matches(str(resolved_player_id), counter)
    ongoing_match = find_ongoing_match(grouped_data)

    if ongoing_match:
        return {
            "nickname": resolved_name,
            "playerId": str(resolved_player_id),
            "active": True,
            "source": "groupByState",
            "matchId": ongoing_match.get("id"),
            "status": ongoing_match.get("status") or ongoing_match.get("state"),
            "competitionName": ongoing_match.get("competition_name")
            or ongoing_match.get("entity", {}).get("name"),
            "queueId": ongoing_match.get("entityCustom", {}).get("queueId"),
            "createdAt": to_iso8601_utc(ongoing_match.get("createdAt")),
        }

    recent_match_id = get_recent_match_id(api_key, str(resolved_player_id), counter)
    if not recent_match_id:
        return {
            "nickname": resolved_name,
            "playerId": str(resolved_player_id),
            "active": False,
            "source": "history",
            "message": "No recent matches found.",
        }

    match_details = get_match_details(api_key, recent_match_id, counter)
    finished_at = match_details.get("finishedAt") or match_details.get("finished_at")

    return {
        "nickname": resolved_name,
        "playerId": str(resolved_player_id),
        "active": False,
        "source": "history",
        "matchId": recent_match_id,
        "status": match_details.get("status"),
        "competitionName": match_details.get("competition_name"),
        "finishedAt": to_iso8601_utc(finished_at),
    }


def build_status_lines(results: List[dict]) -> tuple[List[str], List[str], List[str]]:
    active_players = [item for item in results if item.get("active") and not item.get("error")]
    inactive_players = [item for item in results if not item.get("active") and not item.get("error")]
    errors = [item for item in results if item.get("error")]

    active_players.sort(key=lambda item: str(item["nickname"]).lower())
    inactive_players.sort(
        key=lambda item: parse_iso8601_utc(item.get("finishedAt")),
        reverse=True,
    )

    active_lines = [
        f"`{item['nickname']}`"
        for item in active_players
    ]
    inactive_lines = [
        f"`{item['nickname']}` | last active {to_discord_relative(item.get('finishedAt'))}"
        for item in inactive_players
    ]
    error_lines = [
        f"`{item['nickname']}` | {item['error']}"
        for item in errors
    ]

    return active_lines, inactive_lines, error_lines


class Banlist(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.conn = _connect_db(BANLIST_DB_PATH)
        self.db_lock = threading.Lock()
        _ensure_schema(self.conn)

    def cog_unload(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    @guilds_decorator()
    @app_commands.command(name="banlist", description="Check FACEIT banlist activity or add a player by nickname")
    @app_commands.describe(add="Optional FACEIT nickname to add to the stored banlist")
    async def banlist(self, interaction: discord.Interaction, add: Optional[str] = None) -> None:
        await interaction.response.defer()

        api_key = os.getenv("FACEIT_API_KEY", "").strip()
        if not api_key:
            await interaction.followup.send(
                "FACEIT_API_KEY is not configured in `.env`.",
                ephemeral=True,
            )
            return

        if add:
            await self._handle_add(interaction, api_key, add.strip())
            return

        with self.db_lock:
            players = _get_banlist_players(self.conn)

        if not players:
            await interaction.followup.send(
                "The banlist is empty. Use `/banlist add:<nickname>` to store a FACEIT player first.",
                ephemeral=True,
            )
            return

        counter = ApiCounter()
        tasks = [
            asyncio.to_thread(build_player_status, api_key, player["player_id"], counter)
            for player in players
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        results: List[dict] = []
        nickname_updates: List[tuple[str, str]] = []
        for player, result in zip(players, raw_results):
            if isinstance(result, Exception):
                results.append(
                    {
                        "nickname": player["nickname"],
                        "active": False,
                        "finishedAt": None,
                        "error": str(result),
                    }
                )
                continue

            results.append(result)
            if result.get("playerId") and result.get("nickname"):
                nickname_updates.append((str(result["playerId"]), str(result["nickname"])))

        if nickname_updates:
            with self.db_lock:
                for player_id, nickname in nickname_updates:
                    _touch_player_nickname(self.conn, player_id, nickname)

        active_lines, inactive_lines, error_lines = build_status_lines(results)

        embed = discord.Embed(
            title="FACEIT Banlist",
            color=THEME_COLOR,
        )
        embed.add_field(
            name=f"Active Players ({len(active_lines)})",
            value="\n".join(active_lines[:10]) if active_lines else "None",
            inline=False,
        )
        embed.add_field(
            name=f"Inactive Players ({len(inactive_lines)})",
            value="\n".join(inactive_lines[:10]) if inactive_lines else "None",
            inline=False,
        )

        if error_lines:
            trimmed = "\n".join(error_lines[:8])
            if len(error_lines) > 8:
                trimmed += f"\n...and {len(error_lines) - 8} more"
            embed.add_field(
                name=f"Errors ({len(error_lines)})",
                value=trimmed,
                inline=False,
            )

        embed.set_footer(text=f"Tracked players: {len(players)} | Total FACEIT API calls: {counter.value}")
        await interaction.followup.send(embed=embed)

    async def _handle_add(
        self,
        interaction: discord.Interaction,
        api_key: str,
        nickname: str,
    ) -> None:
        counter = ApiCounter()

        try:
            player = await asyncio.to_thread(get_player_by_nickname, api_key, nickname, counter)
        except Exception as exc:
            await interaction.followup.send(
                f"Failed to resolve `{nickname}` on FACEIT: {exc}",
                ephemeral=True,
            )
            return

        player_id = player.get("player_id") or player.get("id") or player.get("user_id")
        resolved_name = player.get("nickname") or nickname
        if not player_id:
            await interaction.followup.send(
                f"FACEIT did not return a player ID for `{nickname}`.",
                ephemeral=True,
            )
            return

        with self.db_lock:
            created = _upsert_player(self.conn, str(player_id), str(resolved_name))

        verb = "Added" if created else "Updated"
        embed = discord.Embed(
            title="FACEIT Banlist",
            description=f"{verb} `{resolved_name}` with player ID `{player_id}`.",
            color=THEME_COLOR,
        )
        embed.set_footer(text=f"Total FACEIT API calls: {counter.value}")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Banlist(bot))
