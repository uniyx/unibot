import os
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from core.config import env_str
from core.discord_utils import guilds_decorator
from core.faceit_utils import FACEIT_BASE_V4, resolve_player_id, resolve_player_nickname

FACEIT_BASE = FACEIT_BASE_V4
FACEIT_ROOM_BASE = "https://www.faceit.com/en/cs2/room/"
THEME_COLOR = 0xFF5500


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


class FaceitFCRAPI:
    def __init__(self, session: aiohttp.ClientSession, api_key: str):
        self.session = session
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self.total_calls = 0
        self.calls_by_label: Dict[str, int] = defaultdict(int)

    def _count_call(self, label: str) -> None:
        self.total_calls += 1
        self.calls_by_label[label] += 1

    def summary_lines(self) -> List[str]:
        items = sorted(self.calls_by_label.items(), key=lambda kv: (-kv[1], kv[0]))
        return [f"{label}: {count}" for label, count in items]

    async def _get_json(self, url: str, params: Optional[dict] = None, *, label: str = "unknown") -> dict:
        self._count_call(label)
        async with self.session.get(url, headers=self.headers, params=params, timeout=30) as response:
            if response.status >= 400:
                text = await response.text()
                raise RuntimeError(f"FACEIT GET {url} failed [{response.status}]: {text[:200]}")
            return await response.json()

    async def resolve_player(self, nickname: str) -> Tuple[str, str, Optional[str], Optional[str]]:
        data = await self._get_json(
            f"{FACEIT_BASE}/players",
            params={"nickname": nickname},
            label="players.resolve",
        )

        player_id = resolve_player_id(data, fallback=nickname)

        return (
            player_id,
            resolve_player_nickname(data, fallback=nickname),
            data.get("faceit_url"),
            data.get("avatar"),
        )

    async def get_recent_match_ids(self, player_id: str, limit: int) -> List[str]:
        data = await self._get_json(
            f"{FACEIT_BASE}/players/{player_id}/games/cs2/stats",
            params={"offset": 0, "limit": limit},
            label="players.stats.recent_batch",
        )

        match_ids: List[str] = []
        for item in data.get("items", []):
            stats = item.get("stats", {})
            match_id = stats.get("Match Id")
            if match_id:
                match_ids.append(match_id)
        return match_ids

    async def get_match_stats(self, match_id: str) -> dict:
        return await self._get_json(f"{FACEIT_BASE}/matches/{match_id}/stats", label="matches.stats")


def calculate_raw_fcr(stats: Dict[str, Any], total_rounds: int) -> Dict[str, float]:
    kills = to_int(stats.get("Kills"))
    assists = to_int(stats.get("Assists"))
    deaths = to_int(stats.get("Deaths"))
    adr = to_float(stats.get("ADR"))
    kd = to_float(stats.get("K/D Ratio"))

    first_kills = to_int(stats.get("First Kills"))
    entry_count = to_int(stats.get("Entry Count"))
    entry_wins = to_int(stats.get("Entry Wins"))
    match_entry_rate = to_float(stats.get("Match Entry Rate"))
    match_entry_success_rate = to_float(stats.get("Match Entry Success Rate"))

    clutch_kills = to_int(stats.get("Clutch Kills"))
    v1_wins = to_int(stats.get("1v1Wins"))
    v1_count = to_int(stats.get("1v1Count"))
    v2_wins = to_int(stats.get("1v2Wins"))
    v2_count = to_int(stats.get("1v2Count"))

    double_kills = to_int(stats.get("Double Kills"))
    triple_kills = to_int(stats.get("Triple Kills"))
    quadro_kills = to_int(stats.get("Quadro Kills"))
    penta_kills = to_int(stats.get("Penta Kills"))

    utility_damage = to_float(stats.get("Utility Damage"))
    utility_count = to_int(stats.get("Utility Count"))
    utility_successes = to_int(stats.get("Utility Successes"))

    flash_count = to_int(stats.get("Flash Count"))
    flash_successes = to_int(stats.get("Flash Successes"))
    enemies_flashed = to_int(stats.get("Enemies Flashed"))
    mvps = to_int(stats.get("MVPs"))

    rounds = max(total_rounds, 1)

    kpr = kills / rounds
    apr = assists / rounds
    dpr = deaths / rounds

    kpr_score = min((kpr / 0.75) ** 1.1, 1.0)
    kd_score = (min(max(kd, 0.3) / 1.1, 1.3)) ** 1.3
    adr_score = (min(adr / 85.0, 1.2)) ** 1.15

    death_penalty = ((max(0.4, 1 - (dpr - 0.7) * 1.2)) ** 1.1) if dpr > 0.7 else 1.0

    multi_kill_bonus = min(
        (double_kills * 0.03 + triple_kills * 0.10 + quadro_kills * 0.25 + penta_kills * 0.50) / rounds,
        0.2,
    )

    fragging_impact = (
        (kpr_score * 0.35 + kd_score * 0.40 + adr_score * 0.25)
        * death_penalty
        * (1 + multi_kill_bonus)
        * 100
    )

    first_kill_rate = first_kills / rounds
    first_kill_score = min((first_kill_rate / 0.25) ** 1.1, 1.0)

    entry_win_rate = (entry_wins / entry_count) if entry_count > 0 else 0.0
    entry_score = (
        match_entry_rate * 0.25
        + match_entry_success_rate * 0.45
        + entry_win_rate * 0.30
    ) if entry_count > 0 else 0.0

    v1_rate = (v1_wins / v1_count) if v1_count > 0 else 0.0
    v2_rate = (v2_wins / v2_count) if v2_count > 0 else 0.0
    clutch_score = min((clutch_kills / rounds) * 3.5 + v1_rate * 0.6 + v2_rate * 1.0, 1.0)

    entry_clutch = (first_kill_score * 0.40 + entry_score * 0.35 + clutch_score * 0.25) * 100

    util_damage_per_round = utility_damage / rounds
    util_damage_score = min((util_damage_per_round / 4.5) ** 0.9, 1.0)
    util_efficiency = min(utility_successes / utility_count, 1.0) if utility_count > 0 else 0.0
    flash_efficiency = min(flash_successes / flash_count, 1.0) if flash_count > 0 else 0.0

    utility_usage = (util_damage_score * 0.50 + util_efficiency * 0.25 + flash_efficiency * 0.25) * 100

    assist_score = min((apr / 0.22) ** 0.95, 1.0)
    flash_team_score = min(((enemies_flashed / rounds) / 0.45) ** 0.9, 1.0) if enemies_flashed > 0 else 0.0
    mvp_score = min(((mvps / rounds) / 0.18) ** 1.05, 1.0)

    teamplay = (assist_score * 0.40 + flash_team_score * 0.30 + mvp_score * 0.30) * 100

    base_rating = (
        fragging_impact * 0.55
        + entry_clutch * 0.20
        + utility_usage * 0.13
        + teamplay * 0.12
    )

    return {"raw_fcr": max(0.0, base_rating * 0.95)}


def format_fixed(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def find_target_team(match_data: Dict[str, Any], player_id: str) -> Tuple[Optional[dict], int]:
    for match_round in match_data.get("rounds", []):
        total_rounds = to_int((match_round.get("round_stats") or {}).get("Rounds"), 0)
        for team in match_round.get("teams", []):
            for player in team.get("players", []):
                if str(player.get("player_id")) == str(player_id):
                    return team, total_rounds
    return None, 0


def normalize_team_fcr(team: dict, total_rounds: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for player in team.get("players", []):
        stats = player.get("player_stats", {})
        calc = calculate_raw_fcr(stats, total_rounds)
        rows.append(
            {
                "player_id": player.get("player_id"),
                "nickname": player.get("nickname"),
                "stats": stats,
                **calc,
            }
        )

    total_raw = sum(row["raw_fcr"] for row in rows)

    for row in rows:
        row["team_fcr"] = round((row["raw_fcr"] / total_raw * 100.0), 1) if total_raw > 0 else 0.0

    return rows


class FCR(commands.Cog):
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
        name="fcr",
        description="FACEIT CS2 normalized FCR summary for a player over their recent matches.",
    )
    @app_commands.describe(
        user="FACEIT nickname.",
        matches="Number of recent matches to use (default 10, max 30).",
        call="If true, append API call breakdown.",
    )
    async def fcr(
        self,
        interaction: discord.Interaction,
        user: str,
        matches: Optional[int] = 10,
        call: Optional[bool] = False,
    ) -> None:
        api_key = env_str("FACEIT_API_KEY")
        if not api_key:
            await interaction.response.send_message(
                "FACEIT_API_KEY is not set on the bot host. Set it and try again.",
                ephemeral=True,
            )
            return

        match_limit = 10 if matches is None else max(1, min(int(matches), 30))

        assert self.session is not None
        api = FaceitFCRAPI(self.session, api_key)

        await interaction.response.defer(thinking=True)

        try:
            player_id, resolved_name, profile_url, avatar = await api.resolve_player(user)
            match_ids = await api.get_recent_match_ids(player_id, match_limit)

            if not match_ids:
                await interaction.followup.send(
                    f"No recent FACEIT CS2 matches were found for `{resolved_name}`."
                )
                return

            normalized_values: List[float] = []
            notes: List[str] = []
            match_rows: List[Dict[str, Any]] = []

            for index, match_id in enumerate(match_ids, start=1):
                match_data = await api.get_match_stats(match_id)
                team, total_rounds = find_target_team(match_data, player_id)

                if not team:
                    notes.append(f"Skipped match `{match_id}` because the player was not found in team stats.")
                    continue

                rows = normalize_team_fcr(team, total_rounds)
                target = next((row for row in rows if row["player_id"] == player_id), None)
                if not target:
                    notes.append(f"Skipped match `{match_id}` because normalized stats were missing.")
                    continue

                normalized_values.append(target["team_fcr"])
                match_rows.append(
                    {
                        "index": index,
                        "match_id": match_id,
                        "fcr": target["team_fcr"],
                        "adr": to_float(target["stats"].get("ADR"), 0.0),
                        "kd": to_float(target["stats"].get("K/D Ratio"), 0.0),
                        "kda": (
                            f"{to_int(target['stats'].get('Kills'))}/"
                            f"{to_int(target['stats'].get('Deaths'))}/"
                            f"{to_int(target['stats'].get('Assists'))}"
                        ),
                    }
                )

            if not normalized_values:
                await interaction.followup.send(
                    f"I couldn't calculate FCR for `{resolved_name}` from the last {len(match_ids)} matches."
                )
                return

            idx_w = max(1, len(str(len(match_rows))))
            fcr_w = max(3, max((len(format_fixed(row["fcr"], 1)) for row in match_rows), default=3))
            adr_w = max(3, max((len(format_fixed(row["adr"], 1)) for row in match_rows), default=3))
            kda_w = max(5, max((len(row["kda"]) for row in match_rows), default=5))
            kd_w = max(3, max((len(format_fixed(row["kd"], 2)) for row in match_rows), default=3))

            detail_lines = [
                f"{'#':>{idx_w}} {'FCR':>{fcr_w}} {'ADR':>{adr_w}} {'K/D/A':>{kda_w}} {'K/D':>{kd_w}}",
                f"{'-' * idx_w} {'-' * fcr_w} {'-' * adr_w} {'-' * kda_w} {'-' * kd_w}",
            ]
            for row in match_rows:
                detail_lines.append(
                    f"{row['index']:>{idx_w}} "
                    f"{format_fixed(row['fcr'], 1):>{fcr_w}} "
                    f"{format_fixed(row['adr'], 1):>{adr_w}} "
                    f"{row['kda']:>{kda_w}} "
                    f"{format_fixed(row['kd'], 2):>{kd_w}}"
                )

            summary_lines = [
                "Summary",
                f"Matches: {len(normalized_values)}",
                f"Average FCR: {mean(normalized_values):.2f}",
                f"Best: {max(normalized_values):.1f}",
                f"Worst: {min(normalized_values):.1f}",
            ]

            embed = discord.Embed(
                title=f"FACEIT FCR • {resolved_name}",
                description="```text\n" + "\n".join(detail_lines) + "\n\n" + "\n".join(summary_lines) + "\n```",
                color=THEME_COLOR,
            )

            match_links = [
                f"[#{row['index']}]({FACEIT_ROOM_BASE}{row['match_id']})"
                for row in match_rows
            ]
            if match_links:
                embed.add_field(name="Matchrooms", value=" • ".join(match_links), inline=False)

            if profile_url:
                embed.add_field(
                    name="Profile",
                    value=f"[{resolved_name}]({profile_url.replace('{lang}', 'en').rstrip('/')})",
                    inline=False,
                )

            if notes:
                notes_text = "\n".join(f"• {note}" for note in notes)
                if len(notes_text) > 1000:
                    notes_text = notes_text[:997] + "..."
                embed.add_field(name="Notes", value=notes_text, inline=False)

            if bool(call):
                call_text = "\n".join(api.summary_lines())
                if len(call_text) > 1000:
                    call_text = call_text[:997] + "..."
                embed.add_field(
                    name="API Calls",
                    value=f"Total: {api.total_calls}\n{call_text}" if call_text else f"Total: {api.total_calls}",
                    inline=False,
                )

            if avatar:
                embed.set_thumbnail(url=avatar)

            embed.set_footer(text=f"Source: FACEIT Data API • Recent matches requested: {match_limit}")

            await interaction.followup.send(embed=embed)
        except ValueError:
            await interaction.followup.send(
                "The `matches` value must be a whole number between 1 and 30.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(
                f"FACEIT FCR lookup failed for `{user}`: {exc}",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(FCR(bot))
