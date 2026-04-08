from __future__ import annotations

import datetime as dt
import time
from typing import Any, Callable, Optional
from urllib.parse import quote

import aiohttp
import requests
from curl_cffi import requests as curl_requests


FACEIT_BASE_V4 = "https://open.faceit.com/data/v4"
FACEIT_MATCH_GROUPS_URL = "https://www.faceit.com/api/match/v4/matches/groupByState"


class FaceitApiError(RuntimeError):
    pass


REQUEST_TIMEOUT = 20
REQUEST_RETRIES = 3
REQUEST_BACKOFF_SECONDS = 0.75


def build_auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def _count(counter: Any) -> None:
    if counter is None:
        return
    inc = getattr(counter, "inc", None)
    if callable(inc):
        inc()


def _is_name_resolution_error(exc: Exception) -> bool:
    text = str(exc).lower()
    markers = (
        "could not resolve host",
        "nameresolutionerror",
        "temporary failure in name resolution",
        "failed to resolve",
        "getaddrinfo failed",
    )
    return any(marker in text for marker in markers)


def _friendly_request_error(exc: Exception, target: str) -> FaceitApiError:
    if _is_name_resolution_error(exc):
        return FaceitApiError(f"Temporary DNS error while reaching {target}.")
    return FaceitApiError(f"Temporary network error while reaching {target}.")


def _sync_get_json(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: int = REQUEST_TIMEOUT,
    retries: int = REQUEST_RETRIES,
    counter: Any = None,
    target_name: str,
) -> dict:
    backoff = REQUEST_BACKOFF_SECONDS
    last_error: Optional[Exception] = None

    for attempt in range(retries):
        _count(counter)
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise _friendly_request_error(exc, target_name) from exc

    raise FaceitApiError(f"Failed to reach {target_name}.") from last_error


def _sync_curl_get_json(
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: int = REQUEST_TIMEOUT,
    retries: int = REQUEST_RETRIES,
    counter: Any = None,
    target_name: str,
    impersonate: str = "chrome136",
) -> dict:
    backoff = REQUEST_BACKOFF_SECONDS
    last_error: Optional[Exception] = None

    for attempt in range(retries):
        _count(counter)
        try:
            response = curl_requests.get(
                url,
                headers=headers,
                impersonate=impersonate,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise _friendly_request_error(exc, target_name) from exc

    raise FaceitApiError(f"Failed to reach {target_name}.") from last_error


def fetch_player_by_nickname(api_key: str, nickname: str, counter: Any = None) -> dict:
    url = f"{FACEIT_BASE_V4}/players?nickname={quote(nickname)}"
    return _sync_get_json(
        url,
        headers=build_auth_headers(api_key),
        counter=counter,
        target_name="FACEIT player lookup",
    )


def fetch_player_by_id(api_key: str, player_id: str, counter: Any = None) -> dict:
    url = f"{FACEIT_BASE_V4}/players/{quote(player_id)}"
    return _sync_get_json(
        url,
        headers=build_auth_headers(api_key),
        counter=counter,
        target_name="FACEIT player lookup",
    )


def resolve_player_id(data: dict, *, fallback: Optional[str] = None) -> str:
    player_id = data.get("player_id") or data.get("id") or data.get("user_id")
    if not player_id:
        if fallback:
            raise FaceitApiError(f"FACEIT did not return a player ID for '{fallback}'.")
        raise FaceitApiError("FACEIT did not return a player ID.")
    return str(player_id)


def resolve_player_nickname(data: dict, *, fallback: str) -> str:
    return str(data.get("nickname") or fallback)


def fetch_grouped_matches(player_id: str, counter: Any = None) -> dict:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.faceit.com/",
        "Origin": "https://www.faceit.com",
    }
    return _sync_curl_get_json(
        f"{FACEIT_MATCH_GROUPS_URL}?userId={quote(player_id)}",
        headers=headers,
        counter=counter,
        target_name="FACEIT live match status",
    )


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


def fetch_recent_match_id(
    api_key: str,
    player_id: str,
    *,
    game: str = "cs2",
    counter: Any = None,
) -> str | None:
    url = f"{FACEIT_BASE_V4}/players/{player_id}/history?game={game}&offset=0&limit=1"
    data = _sync_get_json(
        url,
        headers=build_auth_headers(api_key),
        counter=counter,
        target_name="FACEIT match history",
    )
    items = data.get("items", [])
    if not items:
        return None
    return items[0].get("match_id")


def fetch_match_details(api_key: str, match_id: str, counter: Any = None) -> dict:
    url = f"{FACEIT_BASE_V4}/matches/{quote(match_id)}"
    return _sync_get_json(
        url,
        headers=build_auth_headers(api_key),
        counter=counter,
        target_name="FACEIT match details",
    )


def to_iso8601_utc(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def parse_iso8601_utc(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def to_discord_relative_timestamp(value: str | None) -> str:
    if not value:
        return "unknown"
    dt_value = parse_iso8601_utc(value)
    if dt_value == dt.datetime.min.replace(tzinfo=dt.timezone.utc):
        return "unknown"
    return f"<t:{int(dt_value.timestamp())}:R>"


async def fetch_json(
    session: aiohttp.ClientSession,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    params: Optional[dict[str, Any]] = None,
    timeout: int = 10,
    error_factory: Optional[Callable[[str], Exception]] = None,
) -> Any:
    async with session.get(url, headers=headers, params=params, timeout=timeout) as response:
        if response.status != 200:
            text = await response.text()
            message = f"HTTP {response.status} for {url}: {text[:200]}"
            if error_factory:
                raise error_factory(message)
            raise FaceitApiError(message)
        return await response.json()


async def resolve_player_id_async(
    session: aiohttp.ClientSession,
    api_key: str,
    nickname: str,
    *,
    error_factory: Optional[Callable[[str], Exception]] = None,
) -> str:
    data = await fetch_json(
        session,
        f"{FACEIT_BASE_V4}/players",
        headers=build_auth_headers(api_key),
        params={"nickname": nickname},
        timeout=10,
        error_factory=error_factory,
    )
    player_id = data.get("player_id")
    if not player_id:
        message = f"Could not resolve FACEIT player_id for nickname '{nickname}'"
        if error_factory:
            raise error_factory(message)
        raise FaceitApiError(message)
    return str(player_id)
