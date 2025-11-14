# cogs/inv.py

import os
import re
import json
from pathlib import Path
from typing import Any, Dict, Tuple, List, Set, Optional
from urllib.parse import quote_plus

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# =========================
# CONFIG PATHS AND CONSTANTS
# =========================

DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None


def guilds_decorator():
    return app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)


# CSGOTrader csfloat API
PRICES_LOCAL_FILE = Path("/unibot/data/prices.json")
PRICES_API_URL = "https://prices.csgotrader.app/latest/csfloat.json"


class Inv(commands.Cog):
    """
    CS2 inventory valuation using:

      - steamcommunity.com public inventory JSON
      - csfloat.json from CSGOTrader (market_hash_name -> price)

    No DB, no currency conversion. Output is directly in csfloat price units.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # For persona name, avatar, and vanity resolution; cog still works without it
        self.steam_token = os.getenv("STEAM_TOKEN")

        # prices: market_hash_name -> float
        self.prices: Dict[str, float] = {}

        self._data_loaded = False

        # Initial load
        self.bot.loop.create_task(self._load_all_data())

        # Periodic refresh from API
        self.price_refresh_task.start()

    # =========================
    # DATA LOADING
    # =========================

    async def _load_all_data(self) -> None:
        """
        Load csfloat prices into memory once at startup.
        """
        self.prices = await self._load_prices()
        self._data_loaded = True

        print(f"[INV] Loaded price entries: {len(self.prices)}")

    @staticmethod
    def _flatten_prices(raw: Dict[str, Any]) -> Dict[str, float]:
        """
        Flatten csfloat schema:
            { "name": { "price": 1.23 }, ... } -> { "name": 1.23 }
        """
        prices: Dict[str, float] = {}
        for name, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            price = entry.get("price")
            if price is None:
                continue
            try:
                prices[name] = float(price)
            except (TypeError, ValueError):
                continue
        return prices

    async def _load_prices_from_disk(self) -> Dict[str, float]:
        with PRICES_LOCAL_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return self._flatten_prices(raw)

    async def _refresh_prices_from_api(self) -> Dict[str, float]:
        """
        Fetch plain JSON from PRICES_API_URL and save to data/prices.json.
        """
        async with aiohttp.ClientSession() as session:
            async with session.get(PRICES_API_URL, timeout=60) as resp:
                resp.raise_for_status()
                raw = await resp.json()

        PRICES_LOCAL_FILE.parent.mkdir(parents=True, exist_ok=True)
        with PRICES_LOCAL_FILE.open("w", encoding="utf-8") as f:
            json.dump(raw, f)

        return self._flatten_prices(raw)

    async def _load_prices(self) -> Dict[str, float]:
        """
        Load prices from local disk, otherwise pull from API.
        """
        if PRICES_LOCAL_FILE.exists():
            try:
                return await self._load_prices_from_disk()
            except Exception as e:
                print(f"[INV] Failed to load local prices.json, refetching: {e}")

        print("[INV] Fetching csfloat prices from API...")
        return await self._refresh_prices_from_api()

    # Periodic refresh task

    @tasks.loop(hours=6)
    async def price_refresh_task(self) -> None:
        """
        Periodically refresh prices directly from the API and update the cache.
        """
        try:
            new_prices = await self._refresh_prices_from_api()
            self.prices = new_prices
            print(f"[INV] Periodic price refresh succeeded. Entries={len(self.prices)}")
        except Exception as e:
            print(f"[INV] Periodic price refresh failed: {e}")

    @price_refresh_task.before_loop
    async def before_price_refresh_task(self) -> None:
        await self.bot.wait_until_ready()

    # =========================
    # LOW LEVEL HTTP + UTIL
    # =========================

    async def _fetch_json(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=20) as resp:
                resp.raise_for_status()
                return await resp.json()

    @staticmethod
    def _extract_steamid64(raw: str) -> Optional[str]:
        """
        Extract a 17-digit SteamID64 from arbitrary text, if present.
        """
        match = re.search(r"\b(\d{17})\b", raw)
        if not match:
            return None
        return match.group(1)

    async def _resolve_vanity(self, vanity: str) -> str:
        """
        Resolve a vanity string (e.g. 'uniyx') to SteamID64 using
        ISteamUser/ResolveVanityURL.

        Raises ValueError on failure.
        """
        if not self.steam_token:
            raise ValueError(
                "STEAM_TOKEN is not set, cannot resolve vanity URLs. "
                "Provide a 17-digit SteamID64 instead."
            )

        vanity_clean = vanity.strip().strip("/")
        if not vanity_clean:
            raise ValueError("Empty vanity URL / ID.")

        url = (
            "http://api.steampowered.com/ISteamUser/ResolveVanityURL/v0001/"
            f"?key={self.steam_token}&vanityurl={quote_plus(vanity_clean)}"
        )

        data = await self._fetch_json(url)
        resp = data.get("response", {})
        if resp.get("success") != 1:
            raise ValueError(
                f"Could not resolve vanity '{vanity_clean}' to a SteamID64. "
                "Check that the profile exists and the vanity name is correct."
            )

        steamid = resp.get("steamid")
        if not steamid:
            raise ValueError(
                f"Vanity '{vanity_clean}' resolved unsuccessfully (no steamid returned)."
            )

        return steamid

    async def _normalize_steam_id(self, raw: str) -> str:
        """
        Accept:
          - 17 digit SteamID64
          - https://steamcommunity.com/profiles/7656...
          - https://steamcommunity.com/id/uniyx/
          - plain vanity strings like 'uniyx'

        Returns a 17-digit SteamID64 or raises ValueError.
        """
        raw = raw.strip()

        # First, try to extract a 17-digit ID from anywhere in the string
        direct = self._extract_steamid64(raw)
        if direct is not None:
            return direct

        # Then, look for /id/<vanity> pattern
        m = re.search(r"steamcommunity\.com/id/([^/]+)/?", raw, re.IGNORECASE)
        if m:
            vanity = m.group(1)
            return await self._resolve_vanity(vanity)

        # If it looks like a URL but we did not find /profiles/ or /id/, complain
        if "steamcommunity.com" in raw:
            raise ValueError(
                "Could not extract a SteamID64 or vanity name from the provided URL."
            )

        # Otherwise treat the raw string as a possible vanity name
        return await self._resolve_vanity(raw)

    # =========================
    # STEAM COMMUNITY INVENTORY + SUMMARY
    # =========================

    async def _fetch_inventory_steamcommunity(self, steamid64: str) -> Dict[str, Any]:
        """
        Fetch the full CS2 inventory from steamcommunity.com with pagination.

        Endpoint:
          https://steamcommunity.com/inventory/{steamid}/730/2?l=english&count=N&start_assetid=...

        We:
          - Pull in pages of size PAGE_COUNT
          - Follow more_items and last_assetid
          - Deduplicate descriptions by (classid, instanceid)
        """
        PAGE_COUNT = 200

        base_url = f"https://steamcommunity.com/inventory/{steamid64}/730/2"
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; unibot/1.0; +https://github.com/uniyx)"
        }

        all_assets: List[Dict[str, Any]] = []
        all_descriptions: List[Dict[str, Any]] = []
        seen_desc_keys: Set[Tuple[Optional[str], Optional[str]]] = set()

        start_assetid: Optional[str] = None
        page_index = 0

        while True:
            params = f"?l=english&count={PAGE_COUNT}"
            if start_assetid is not None:
                params += f"&start_assetid={start_assetid}"

            url = base_url + params
            page_index += 1
            print(f"[INV] Fetching inventory page {page_index}: {url}")

            data = await self._fetch_json(url, headers=headers)

            assets = data.get("assets") or []
            descriptions = data.get("descriptions") or []

            if not assets and not all_assets:
                print("[INV] Inventory appears empty or inaccessible on first page")
                break

            all_assets.extend(assets)

            for desc in descriptions:
                key = (desc.get("classid"), desc.get("instanceid"))
                if key in seen_desc_keys:
                    continue
                seen_desc_keys.add(key)
                all_descriptions.append(desc)

            more_items = data.get("more_items")
            last_assetid = data.get("last_assetid")

            print(
                f"[INV] Page {page_index}: assets={len(assets)}, "
                f"descriptions={len(descriptions)}, more_items={more_items}, "
                f"last_assetid={last_assetid}"
            )

            if not more_items or not last_assetid:
                break

            start_assetid = str(last_assetid)

        print(
            f"[INV] Finished inventory fetch: "
            f"total_assets={len(all_assets)}, total_descriptions={len(all_descriptions)}"
        )

        return {
            "assets": all_assets,
            "descriptions": all_descriptions,
        }

    async def _fetch_player_summary(self, steamid64: str) -> Dict[str, Any]:
        """
        Optional cosmetics. If STEAM_TOKEN is not set or call fails,
        inventory valuation still works.
        """
        if not self.steam_token:
            return {}

        url = (
            "http://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"
            f"?key={self.steam_token}&steamids={steamid64}"
        )

        try:
            data = await self._fetch_json(url)
        except Exception:
            return {}

        players = data.get("response", {}).get("players", [])
        return players[0] if players else {}

    # =========================
    # CORE VALUATION LOGIC (WITH LOGGING)
    # =========================

    async def compute_inventory_value(
        self,
        steamid64: str,
    ) -> Tuple[float, int, List[Tuple[str, float]]]:
        """
        Use csfloat prices to evaluate an inventory.

        Returns:
          total_value, items_priced_count, top_items

        where top_items is a list of (market_hash_name, price) sorted by price desc.
        Logs every pricing decision for debugging.
        """
        if not self._data_loaded:
            await self._load_all_data()

        inv = await self._fetch_inventory_steamcommunity(steamid64)
        descriptions = inv.get("descriptions", [])
        assets = inv.get("assets", [])

        classid_lookup: Dict[str, Dict[str, Any]] = {
            d.get("classid"): d for d in descriptions if d.get("classid")
        }

        total_value = 0.0
        total_success = 0

        # Track best unit price per base_name for top list
        best_prices: Dict[str, float] = {}

        print(f"[INV] === Valuation start for {steamid64} ===")
        print(f"[INV] assets={len(assets)}, descriptions={len(descriptions)}")

        for idx, asset in enumerate(assets):
            classid = asset.get("classid")
            if not classid:
                print(f"[INV] [{idx}] asset missing classid, skipping")
                continue

            desc = classid_lookup.get(classid)
            if not desc:
                print(f"[INV] [{idx}] no description for classid={classid}")
                continue

            base_name = desc.get("market_hash_name")
            if not base_name:
                print(
                    f"[INV] [{idx}] description missing market_hash_name "
                    f"(classid={classid})"
                )
                continue

            price = self.prices.get(base_name)
            if price is None:
                print(f"[INV] [{idx}] NO PRICE base='{base_name}'")
                continue

            try:
                price_f = float(price)
            except (TypeError, ValueError):
                print(
                    f"[INV] [{idx}] price not numeric for base='{base_name}', "
                    f"raw={price!r}"
                )
                continue

            total_value += price_f
            total_success += 1

            # Track max unit price seen for this item name
            prev = best_prices.get(base_name)
            if prev is None or price_f > prev:
                best_prices[base_name] = price_f

            print(
                f"[INV] [{idx}] OK base='{base_name}' "
                f"price={price_f:.4f} running_total={total_value:.4f}"
            )

        print(
            f"[INV] === Valuation end for {steamid64}: "
            f"items_priced={total_success}, total_value={total_value:.4f} ==="
        )

        # Build top 5 list sorted by unit price descending
        sorted_items = sorted(
            best_prices.items(),
            key=lambda kv: kv[1],
            reverse=True,
        )
        top_items = sorted_items[:5]

        return total_value, total_success, top_items

    # =========================
    # SLASH COMMAND
    # =========================

    @guilds_decorator()
    @app_commands.command(
        name="inv",
        description="Check CS2 inventory value using csfloat prices.",
    )
    async def inv(
        self,
        interaction: discord.Interaction,
        steam_id: str,
    ) -> None:
        """
        Example inputs:
          76561198000000000
          https://steamcommunity.com/profiles/76561198000000000
          https://steamcommunity.com/id/uniyx/
          uniyx
        """
        await interaction.response.defer(ephemeral=False, thinking=True)

        try:
            steamid64 = await self._normalize_steam_id(steam_id)
        except ValueError as e:
            embed = discord.Embed(
                title="Invalid Steam ID / vanity URL",
                description=str(e),
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        try:
            total_value, item_count, top_items = await self.compute_inventory_value(
                steamid64
            )
        except Exception as e:
            embed = discord.Embed(
                title="Error fetching inventory",
                description=(
                    "Steam may be rate limiting, the inventory may be private, "
                    "or the pricing data may be unavailable.\n\n"
                    f"Details: `{e}`"
                ),
                color=discord.Color.red(),
            )
            await interaction.followup.send(embed=embed)
            return

        player = await self._fetch_player_summary(steamid64)
        personaname = player.get("personaname", "Unknown player")
        avatar = player.get("avatarfull")

        inventory_url = f"https://steamcommunity.com/profiles/{steamid64}/inventory/730/"

        embed = discord.Embed(
            title=f"{personaname}'s CS2 Inventory",
            url=inventory_url,
            description=f"SteamID64: `{steamid64}`",
            color=discord.Color.from_str("#0069bf"),
        )
        embed.set_author(name="Inventory Valuation")
        if avatar:
            embed.set_thumbnail(url=avatar)

        embed.add_field(
            name="Inventory value (csfloat)",
            value=f"**{total_value:,.2f}**",
            inline=False,
        )
        embed.add_field(
            name="Items priced",
            value=f"**{item_count}**",
            inline=False,
        )

        # Top 5 most expensive items by unit price
        if top_items:
            lines = [
                f"{idx + 1}. `{name}` — **{price:,.2f}**"
                for idx, (name, price) in enumerate(top_items)
            ]
            embed.add_field(
                name="Top 5 most expensive items",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Top 5 most expensive items",
                value="No priced items found.",
                inline=False,
            )

        embed.set_footer(text="Prices from csfloat.json (CSGOTrader).")

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Inv(bot))
