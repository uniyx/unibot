"""Print a complete FACEIT player profile and CS2 statistics report.

The API key is read from FACEIT_API_KEY in the environment or the repository's
.env file. The JSON printed to stdout contains only FACEIT response data; the
key is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from dotenv import load_dotenv


FACEIT_BASE_V4 = "https://open.faceit.com/data/v4"
DEFAULT_GAME = "cs2"
DEFAULT_LIMIT = 30
MAX_LIMIT = 100
MAX_OFFSET = 200


class FaceitApiError(RuntimeError):
    """A user-facing FACEIT API or configuration error."""


def _bounded_int(value: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer.") from exc
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return parsed


def _limit_arg(value: str) -> int:
    return _bounded_int(value, name="limit", minimum=1, maximum=MAX_LIMIT)


def _offset_arg(value: str) -> int:
    return _bounded_int(value, name="offset", minimum=0, maximum=MAX_OFFSET)


def _api_key() -> str:
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)
    key = (os.getenv("FACEIT_API_KEY") or "").strip()
    if not key:
        raise FaceitApiError(
            "FACEIT_API_KEY is not set. Add it to .env or the environment."
        )
    return key


class FaceitClient:
    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
                "User-Agent": "unibot-faceit-full-stats/1.0",
            }
        )

    def _get_json(self, path: str, *, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{FACEIT_BASE_V4}{path}"
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FaceitApiError(f"FACEIT request failed: {exc}") from exc

        if response.status_code >= 400:
            detail = response.text[:500].strip()
            suffix = f": {detail}" if detail else ""
            raise FaceitApiError(
                f"FACEIT request failed [{response.status_code}]{suffix}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise FaceitApiError("FACEIT returned invalid JSON.") from exc
        if not isinstance(data, dict):
            raise FaceitApiError("FACEIT returned an unexpected JSON shape.")
        return data

    def resolve_player(self, nickname: str, *, game: str) -> Dict[str, Any]:
        return self._get_json(
            "/players",
            params={"nickname": nickname, "game": game},
        )

    def get_lifetime_stats(self, player_id: str, *, game: str) -> Dict[str, Any]:
        return self._get_json(f"/players/{player_id}/stats/{game}")

    def get_recent_stats(
        self,
        player_id: str,
        *,
        game: str,
        offset: int,
        limit: int,
    ) -> Dict[str, Any]:
        return self._get_json(
            f"/players/{player_id}/games/{game}/stats",
            params={"offset": offset, "limit": limit},
        )

    def full_report(
        self,
        nickname: str,
        *,
        game: str,
        offset: int,
        limit: int,
        include_lifetime: bool,
    ) -> Dict[str, Any]:
        player = self.resolve_player(nickname, game=game)
        player_id = player.get("player_id")
        if not player_id:
            raise FaceitApiError("FACEIT did not return a player_id for that nickname.")

        report: Dict[str, Any] = {
            "player": player,
            "recent_match_stats": self.get_recent_stats(
                player_id,
                game=game,
                offset=offset,
                limit=limit,
            ),
        }
        if include_lifetime:
            report["lifetime_stats"] = self.get_lifetime_stats(player_id, game=game)
        return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Print full FACEIT profile, lifetime, and recent CS2 stats as JSON."
    )
    parser.add_argument(
        "nickname",
        nargs="?",
        default="uni",
        help="FACEIT nickname (default: uni)",
    )
    parser.add_argument(
        "--game",
        default=DEFAULT_GAME,
        help="FACEIT game identifier (default: cs2)",
    )
    parser.add_argument(
        "--offset",
        type=_offset_arg,
        default=0,
        help="Number of recent records to skip (0-200, default: 0)",
    )
    parser.add_argument(
        "--limit",
        type=_limit_arg,
        default=DEFAULT_LIMIT,
        help="Number of recent records to return (1-100, default: 30)",
    )
    parser.add_argument(
        "--recent-only",
        action="store_true",
        help="Skip the lifetime stats request",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    nickname = args.nickname.strip()
    if not nickname:
        print("Nickname must not be empty.", file=sys.stderr)
        return 2

    try:
        client = FaceitClient(_api_key())
        report = client.full_report(
            nickname,
            game=args.game,
            offset=args.offset,
            limit=args.limit,
            include_lifetime=not args.recent_only,
        )
    except FaceitApiError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
