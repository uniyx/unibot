import os

import cloudscraper
import requests
from dotenv import load_dotenv


OPEN_FACEIT_BASE = "https://open.faceit.com/data/v4"


def get_banlist() -> list[str]:
    raw = os.getenv("FACEIT_BANLIST", "").strip()
    return [nickname.strip() for nickname in raw.split(",") if nickname.strip()]


def resolve_player_id(nickname: str, api_key: str) -> tuple[str, str]:
    response = requests.get(
        f"{OPEN_FACEIT_BASE}/players",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"nickname": nickname},
        timeout=30,
    )
    response.raise_for_status()

    data = response.json()
    player_id = data.get("player_id")
    resolved_name = data.get("nickname") or nickname
    if not player_id:
        raise RuntimeError(f"Could not resolve player_id for '{nickname}'")
    return player_id, resolved_name


def is_in_live_match(player_id: str) -> bool:
    scraper = cloudscraper.create_scraper()
    response = scraper.get(
        "https://www.faceit.com/api/match/v4/matches/groupByState",
        params={"userId": player_id},
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json().get("payload", {})
    return bool(payload.get("ONGOING"))


def main() -> None:
    load_dotenv(".env")

    api_key = os.getenv("FACEIT_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("FACEIT_API_KEY is missing from .env")

    nicknames = get_banlist()
    if not nicknames:
        raise SystemExit("FACEIT_BANLIST is empty or missing from .env")

    for nickname in nicknames:
        try:
            player_id, resolved_name = resolve_player_id(nickname, api_key)
            if is_in_live_match(player_id):
                print(f"{resolved_name} is currently in a live match!")
            else:
                print(f"{resolved_name} is not queueing.")
        except Exception as exc:
            print(f"{nickname}: failed to check status ({exc})")


if __name__ == "__main__":
    main()
