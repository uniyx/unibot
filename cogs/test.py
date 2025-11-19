import os
import asyncio
from statistics import mean
from typing import Optional, Any, List

import aiohttp

# Hardcoded player id for "uni"
PLAYER_ID = "cdcb07db-ccb7-4c00-933a-605b01094a79"

FACEIT_DATA_BASE = "https://open.faceit.com/data/v4"
SCOREBOARD_BASE = "https://www.faceit.com/api/statistics/v1"


def _to_float(x: Any) -> Optional[float]:
    try:
        return float(x)
    except Exception:
        return None


async def fetch_recent_matches(session: aiohttp.ClientSession, api_key: str, limit: int = 30):
    headers = {"Authorization": f"Bearer {api_key}"}
    limit = max(1, min(100, int(limit)))

    url = f"{FACEIT_DATA_BASE}/players/{PLAYER_ID}/games/cs2/stats"
    params = {"offset": 0, "limit": limit}

    async with session.get(url, headers=headers, params=params, timeout=30) as r:
        if r.status >= 400:
            text = await r.text()
            raise RuntimeError(f"fetch_recent_matches failed [{r.status}]: {text[:200]}")
        data = await r.json()

    return data.get("items") or []


async def fetch_match_rws(session: aiohttp.ClientSession, match_id: str) -> Optional[float]:
    """
    Calls the scoreboard endpoint for RWS.
    """
    url = f"{SCOREBOARD_BASE}/cs2/matches/{match_id}/match-rounds/1/scoreboard"
    params = {"statsType": 2}

    try:
        async with session.get(url, params=params, timeout=30) as r:
            if r.status >= 400:
                return None
            data = await r.json()
    except Exception:
        return None

    try:
        teams = (
            data.get("payload", {})
                .get("cs2", {})
                .get("teams", [])
        )
        for team in teams:
            for player in team.get("players", []):
                if player.get("player_id") == PLAYER_ID:
                    total = player.get("total", {})
                    rws = total.get("rws") or total.get("RWS")
                    return _to_float(rws)
    except Exception:
        return None

    return None


async def main():
    # Either set FACEIT_API_KEY in your env, or replace this line with a hardcoded key.
    api_key = "5bc2b8e5-4b00-452b-80a2-afc032a1b92c"
    if not api_key:
        raise SystemExit("FACEIT_API_KEY is not set in the environment.")

    print(f"Using hardcoded player_id = {PLAYER_ID}")

    async with aiohttp.ClientSession() as session:
        items = await fetch_recent_matches(session, api_key, limit=30)
        print(f"Fetched {len(items)} recent matches")

        adr_values: List[float] = []
        rws_values: List[float] = []

        for idx, item in enumerate(items, start=1):
            stats = item.get("stats") or {}

            # ADR
            adr_raw = stats.get("ADR") or stats.get("Average Damage/Round")
            adr_val = _to_float(adr_raw)
            if adr_val is not None:
                adr_values.append(adr_val)

            # Try multiple match ID key formats
            match_id = (
                stats.get("Match Id")
                or stats.get("Match ID")
                or stats.get("MatchId")
                or stats.get("match_id")
                or item.get("match_id")
            )

            if not match_id:
                print(f"[{idx}] No match_id found for this entry; stats keys={list(stats.keys())}")
                continue

            rws_val = await fetch_match_rws(session, match_id)
            if rws_val is not None:
                rws_values.append(rws_val)

            # Per-match printout
            adr_str = f"{adr_val:.2f}" if adr_val is not None else "n/a"
            rws_str = f"{rws_val:.2f}" if rws_val is not None else "n/a"
            print(f"[{idx}] match_id={match_id} | ADR={adr_str} | RWS={rws_str}")

        avg_adr = mean(adr_values) if adr_values else None
        avg_rws = mean(rws_values) if rws_values else None

        print("\n===== RESULTS (Last 30 Matches) =====")
        print(f"Average ADR: {avg_adr:.2f}" if avg_adr is not None else "Average ADR: n/a")
        print(f"Average RWS: {avg_rws:.2f}" if avg_rws is not None else "Average RWS: n/a")
        print(f"Matches with ADR: {len(adr_values)}")
        print(f"Matches with RWS: {len(rws_values)}")


if __name__ == "__main__":
    asyncio.run(main())
