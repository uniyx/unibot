import asyncio
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from core.config import env_str
from core.discord_utils import guilds_decorator
from core.faceit_utils import (
    fetch_grouped_matches,
    fetch_match_details,
    fetch_player_by_id,
    fetch_player_by_nickname,
    fetch_recent_match_id,
    find_ongoing_match,
    parse_iso8601_utc,
    resolve_player_id,
    resolve_player_nickname,
    to_discord_relative_timestamp,
    to_iso8601_utc,
)
from core.sqlite_utils import connect_sqlite

BANLIST_DB_PATH = env_str("BANLIST_DB_PATH", "data/faceit_banlist.sqlite3")
GAME = "cs2"
THEME_COLOR = 0xFF5500


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


def _remove_player_by_id(conn: sqlite3.Connection, player_id: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM faceit_banlist WHERE player_id = ?",
        (player_id,),
    )
    conn.commit()
    return cursor.rowcount > 0


def build_player_status(api_key: str, player_id: str, counter: ApiCounter) -> dict:
    player = fetch_player_by_id(api_key, player_id, counter)
    resolved_name = resolve_player_nickname(player, fallback=player_id)
    resolved_player_id = resolve_player_id(player, fallback=player_id)

    grouped_data = fetch_grouped_matches(str(resolved_player_id), counter)
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

    recent_match_id = fetch_recent_match_id(
        api_key,
        str(resolved_player_id),
        game=GAME,
        counter=counter,
    )
    if not recent_match_id:
        return {
            "nickname": resolved_name,
            "playerId": str(resolved_player_id),
            "active": False,
            "source": "history",
            "message": "No recent matches found.",
        }

    match_details = fetch_match_details(api_key, recent_match_id, counter)
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
        f"`{item['nickname']}` | last active {to_discord_relative_timestamp(item.get('finishedAt'))}"
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
        self.conn = connect_sqlite(BANLIST_DB_PATH)
        self.db_lock = threading.Lock()
        _ensure_schema(self.conn)

    def cog_unload(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    @guilds_decorator()
    @app_commands.command(name="banlist", description="Check FACEIT banlist activity or add a player by nickname")
    @app_commands.describe(
        add="Optional FACEIT nickname to add to the stored banlist",
        remove="Optional FACEIT nickname to remove from the stored banlist",
    )
    async def banlist(
        self,
        interaction: discord.Interaction,
        add: Optional[str] = None,
        remove: Optional[str] = None,
    ) -> None:
        await interaction.response.defer()

        api_key = env_str("FACEIT_API_KEY")
        if not api_key:
            await interaction.followup.send(
                "FACEIT_API_KEY is not configured in `.env`.",
                ephemeral=True,
            )
            return

        if add and remove:
            await interaction.followup.send(
                "Use either `add` or `remove`, not both in the same command.",
                ephemeral=True,
            )
            return

        if add:
            await self._handle_add(interaction, api_key, add.strip())
            return

        if remove:
            await self._handle_remove(interaction, api_key, remove.strip())
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
            player = await asyncio.to_thread(fetch_player_by_nickname, api_key, nickname, counter)
        except Exception as exc:
            await interaction.followup.send(
                f"Failed to resolve `{nickname}` on FACEIT: {exc}",
                ephemeral=True,
            )
            return

        try:
            player_id = resolve_player_id(player, fallback=nickname)
        except Exception:
            await interaction.followup.send(
                f"FACEIT did not return a player ID for `{nickname}`.",
                ephemeral=True,
            )
            return
        resolved_name = resolve_player_nickname(player, fallback=nickname)

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

    async def _handle_remove(
        self,
        interaction: discord.Interaction,
        api_key: str,
        nickname: str,
    ) -> None:
        counter = ApiCounter()

        try:
            player = await asyncio.to_thread(fetch_player_by_nickname, api_key, nickname, counter)
        except Exception as exc:
            await interaction.followup.send(
                f"Failed to resolve `{nickname}` on FACEIT: {exc}",
                ephemeral=True,
            )
            return

        try:
            player_id = resolve_player_id(player, fallback=nickname)
        except Exception:
            await interaction.followup.send(
                f"FACEIT did not return a player ID for `{nickname}`.",
                ephemeral=True,
            )
            return
        resolved_name = resolve_player_nickname(player, fallback=nickname)

        with self.db_lock:
            removed = _remove_player_by_id(self.conn, str(player_id))

        if not removed:
            await interaction.followup.send(
                f"`{resolved_name}` is not currently in the banlist.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="FACEIT Banlist",
            description=f"Removed `{resolved_name}` with player ID `{player_id}`.",
            color=THEME_COLOR,
        )
        embed.set_footer(text=f"Total FACEIT API calls: {counter.value}")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Banlist(bot))
