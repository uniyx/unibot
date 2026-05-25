import json
import os
import sys
from pathlib import Path
from typing import Any

import requests


FACEIT_BASE_V4 = "https://open.faceit.com/data/v4"
DEFAULT_PLAYER_ID = "cdcb07db-ccb7-4c00-933a-605b01094a79"
DEFAULT_LIMIT = 30


def load_dotenv_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def build_auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def fetch_player_stats(player_id: str, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict[str, Any]:
    load_dotenv_file(Path(".env"))
    api_key = env_str("FACEIT_API_KEY")
    if not api_key:
        raise RuntimeError("FACEIT_API_KEY is not set.")

    url = f"{FACEIT_BASE_V4}/players/{player_id}/games/cs2/stats"
    response = requests.get(
        url,
        headers=build_auth_headers(api_key),
        params={"offset": offset, "limit": limit},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    player_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PLAYER_ID

    try:
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_LIMIT
    except ValueError:
        print("Limit must be an integer.", file=sys.stderr)
        return 2

    try:
        data = fetch_player_stats(player_id, limit=limit)
    except requests.HTTPError as exc:
        body = exc.response.text[:500] if exc.response is not None else str(exc)
        print(f"FACEIT request failed: {body}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
