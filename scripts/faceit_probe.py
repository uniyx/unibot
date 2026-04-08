import argparse
import asyncio
import json
import os
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

import aiohttp
from dotenv import load_dotenv


OPEN_FACEIT_BASE = "https://open.faceit.com/data/v4"
FACEIT_SITE_BASE = "https://www.faceit.com"
SITE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.faceit.com",
    "Referer": "https://www.faceit.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0.0.0 Safari/537.36"
    ),
}


def build_url(target: str) -> str:
    if target.startswith("http://") or target.startswith("https://"):
        return target
    if target.startswith("/"):
        return OPEN_FACEIT_BASE + target
    return OPEN_FACEIT_BASE + "/" + target


def parse_params(values: list[str]) -> Dict[str, str]:
    params: Dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"Invalid --param '{value}'. Expected key=value.")
        key, raw = value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid --param '{value}'. Key cannot be empty.")
        params[key] = raw
    return params


def should_use_bearer(url: str, force_site_headers: bool) -> bool:
    if force_site_headers:
        return False
    host = urlparse(url).netloc.lower()
    return "open.faceit.com" in host


async def resolve_player_id(
    session: aiohttp.ClientSession,
    nickname: str,
    api_key: str,
) -> Tuple[str, str]:
    headers = {"Authorization": f"Bearer {api_key}"}
    async with session.get(
        f"{OPEN_FACEIT_BASE}/players",
        headers=headers,
        params={"nickname": nickname},
        timeout=30,
    ) as response:
        text = await response.text()
        if response.status != 200:
            raise RuntimeError(f"Resolve failed [{response.status}]: {text[:300]}")

    data = json.loads(text)
    player_id = str(data.get("player_id") or "")
    resolved_name = str(data.get("nickname") or nickname)
    if not player_id:
        raise RuntimeError(f"Could not resolve player_id for '{nickname}'")
    return player_id, resolved_name


async def fetch(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: Dict[str, str],
    api_key: str,
    force_site_headers: bool,
) -> Tuple[int, str, Dict[str, str]]:
    headers: Dict[str, str] = {}
    if should_use_bearer(url, force_site_headers):
        headers["Authorization"] = f"Bearer {api_key}"
    elif force_site_headers or "faceit.com" in urlparse(url).netloc.lower():
        headers.update(SITE_HEADERS)

    async with session.get(url, headers=headers, params=params, timeout=30) as response:
        text = await response.text()
        response_headers = dict(response.headers)
        return response.status, text, response_headers


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe FACEIT endpoints with your local .env FACEIT_API_KEY.",
    )
    parser.add_argument(
        "target",
        help="Endpoint path or full URL. Example: /players or https://www.faceit.com/api/match/v1/matches/groupByState",
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        help="Query param in key=value form. Repeat for multiple params.",
    )
    parser.add_argument(
        "--nickname",
        help="Resolve a FACEIT nickname and inject player_id into --param values via {player_id}.",
    )
    parser.add_argument(
        "--site",
        action="store_true",
        help="Use browser-like faceit.com headers instead of Bearer auth.",
    )
    parser.add_argument(
        "--headers",
        action="store_true",
        help="Print response headers too.",
    )
    args = parser.parse_args()

    load_dotenv(".env")
    api_key = os.getenv("FACEIT_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("FACEIT_API_KEY is missing from .env")

    url = build_url(args.target)
    params = parse_params(args.param)

    async with aiohttp.ClientSession() as session:
        if args.nickname:
            player_id, resolved_name = await resolve_player_id(session, args.nickname, api_key)
            params = {
                key: value.replace("{player_id}", player_id).replace("{nickname}", resolved_name)
                for key, value in params.items()
            }
            print(f"Resolved nickname '{args.nickname}' -> {resolved_name} ({player_id})")

        status, text, response_headers = await fetch(
            session,
            url,
            params=params,
            api_key=api_key,
            force_site_headers=bool(args.site),
        )

    print(f"URL: {url}")
    print(f"Params: {params}")
    print(f"HTTP {status}")

    if args.headers:
        print("Response headers:")
        for key in sorted(response_headers):
            print(f"  {key}: {response_headers[key]}")

    try:
        parsed = json.loads(text)
        print(json.dumps(parsed, indent=2))
    except Exception:
        print(text)


if __name__ == "__main__":
    asyncio.run(main())
