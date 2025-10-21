#!/usr/bin/env python3
import argparse, csv, os, re, sys, time
from collections import Counter
from typing import Dict, List, Optional, Tuple
import requests

STEAM_APPID, STEAM_CONTEXT = 730, 2
STEAM_TIMEOUT = 25
CSFLOAT_TIMEOUT = 25
DEFAULT_SLEEP = 0.5

# ---------- Steam inventory ----------

def resolve_vanity_to_steamid64(vanity: str) -> str:
    vanity = vanity.strip().strip("/")
    if vanity.isdigit() and len(vanity) >= 16:
        return vanity
    r = requests.get(f"https://steamcommunity.com/id/{vanity}/?xml=1", timeout=STEAM_TIMEOUT)
    r.raise_for_status()
    m = re.search(r"<steamID64>(\d+)</steamID64>", r.text)
    if not m:
        raise ValueError(f"Could not resolve vanity '{vanity}'. Provide --steamid.")
    return m.group(1)

def fetch_full_inventory(steamid64: str, lang="english", page_count=100) -> List[Dict]:
    url = f"https://steamcommunity.com/inventory/{steamid64}/{STEAM_APPID}/{STEAM_CONTEXT}"
    params = {"l": lang, "count": page_count}
    start = None
    assets: List[Dict] = []
    sess = requests.Session()
    sess.headers.update({"User-Agent": "csfloat-valuator/1.1"})

    while True:
        if start is not None:
            params["start_assetid"] = start
        r = sess.get(url, params=params, timeout=STEAM_TIMEOUT)
        if r.status_code == 403:
            raise PermissionError("Inventory is private or not accessible.")
        r.raise_for_status()
        data = r.json()

        desc_map = {f"{d.get('classid','')}_{d.get('instanceid','')}": d for d in data.get("descriptions", [])}
        for a in data.get("assets", []):
            key = f"{a.get('classid','')}_{a.get('instanceid','')}"
            d = desc_map.get(key, {})
            name = (d.get("market_hash_name") or d.get("market_name") or "").strip()
            if name:
                assets.append({"name": name, "marketable": d.get("marketable", 0)})

        if data.get("more_items") == 1:
            start = data.get("last_assetid") or None
            if not start:
                break
        else:
            break
    return assets

def count_by_name(assets: List[Dict], include_unmarketable=True) -> Dict[str, int]:
    c = Counter()
    for a in assets:
        if not include_unmarketable and not a.get("marketable", 0):
            continue
        n = (a.get("name") or "").strip()
        if n:
            c[n] += 1
    return dict(c)

# ---------- CSFloat client ----------

class CSFloatClient:
    BASE = "https://csfloat.com/api/v1/listings"

    def __init__(self, api_key: str, sleep_sec: float = DEFAULT_SLEEP, verbose: bool = False):
        if not api_key or len(api_key.strip()) < 8:
            raise ValueError("Missing CSFloat API key. Set CSFLOAT_API_KEY or pass --key.")
        self.key = api_key.strip().replace("Bearer ", "")
        self.sleep = max(0.0, float(sleep_sec))
        self.verbose = verbose
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": "csfloat-valuator/1.2", "Accept": "application/json"})
        # cache maps a lookup-key to tuple: (price_cents, listing_id, final_url_string)
        self.cache: Dict[str, Optional[Tuple[int, str, str]]] = {}

    def _final_url(self, params: Dict) -> str:
        """Return the fully encoded URL that requests will call."""
        req = requests.Request("GET", self.BASE, params=params, headers={"Authorization": self.key})
        prepped = self.sess.prepare_request(req)
        return prepped.url

    def _get(self, params: Dict) -> Tuple[List[Dict], str]:
        url_for_log = self._final_url(params)
        if self.verbose:
            print(f"[CSFloat] GET {url_for_log}")
        last_exc = None
        for attempt in range(2):
            try:
                r = self.sess.get(self.BASE, params=params, headers={"Authorization": self.key}, timeout=CSFLOAT_TIMEOUT)
                if r.status_code in (401, 403):
                    raise PermissionError(f"CSFloat auth rejected ({r.status_code}). Check your key.")
                if r.status_code == 429:
                    time.sleep(1.2 * (attempt + 1)); continue
                r.raise_for_status()
                data = r.json() if r.text else []
                return data, url_for_log
            except Exception as e:
                last_exc = e
                time.sleep(0.6 * (attempt + 1))
        raise RuntimeError(f"CSFloat request failed after retries: {last_exc}")

    def lowest_listing_exact(self, market_hash_name: str) -> Optional[Tuple[int, str, str]]:
        key = ("exact|" + market_hash_name).strip()
        if key in self.cache:
            return self.cache[key]
        params = {"market_hash_name": market_hash_name, "limit": 1, "sort_by": "lowest_price"}
        data, final_url = self._get(params)
        time.sleep(self.sleep)
        if not isinstance(data, list) or not data:
            self.cache[key] = None
            return None
        cents = int(data[0]["price"])
        listing_id = str(data[0]["id"])
        self.cache[key] = (cents, listing_id, final_url)
        return self.cache[key]

    def lowest_listing_broad(self, market_hash_name: str) -> Optional[Tuple[int, str, str]]:
        m = re.match(r"^(.*?)(?:\s*\([^)]*\))$", market_hash_name)
        base = m.group(1).strip() if m else market_hash_name.strip()
        key = ("broad|" + base)
        if key in self.cache:
            return self.cache[key]
        if base == market_hash_name:
            self.cache[key] = None
            return None
        params = {"market_hash_name": base, "limit": 1, "sort_by": "lowest_price"}
        data, final_url = self._get(params)
        time.sleep(self.sleep)
        if not isinstance(data, list) or not data:
            self.cache[key] = None
            return None
        cents = int(data[0]["price"])
        listing_id = str(data[0]["id"])
        self.cache[key] = (cents, listing_id, final_url)
        return self.cache[key]

# ---------- Valuation with sequential printing ----------

def sequential_value(counts: Dict[str, int], cf: CSFloatClient, show_source: bool = True) -> Tuple[int, List[Dict]]:
    if not counts:
        print("No items to value.")
        return 0, []

    name_w = max([len(n) for n in counts.keys()] + [4])
    qty_w, unit_w, sub_w = max(len(str(v)) for v in counts.values()), 12, 14
    def fmt_cents(c): return "n/a".rjust(unit_w) if c is None else f"{c/100:,.2f}"

    print(f"{'Item':{name_w}}  {'Qty':>{qty_w}}  {'Unit ($)':>{unit_w}}  {'Subtotal ($)':>{sub_w}}")
    print("-" * (name_w + qty_w + unit_w + sub_w + 6))

    total_cents = 0
    rows: List[Dict] = []

    for name in sorted(counts.keys(), key=str.lower):
        qty = counts[name]

        exact = cf.lowest_listing_exact(name)
        used = exact
        used_mode = "exact"

        if exact is None:
            broad = cf.lowest_listing_broad(name)
            if broad is not None:
                used = broad
                used_mode = "broad"

        if used is None:
            unit_cents, listing_id, src_url = None, "", ""
            subtotal_cents = 0
        else:
            unit_cents, listing_id, src_url = used
            subtotal_cents = unit_cents * qty
            total_cents += subtotal_cents

        print(f"{name:{name_w}}  {qty:>{qty_w}}  {fmt_cents(unit_cents):>{unit_w}}  {fmt_cents(subtotal_cents):>{sub_w}}")
        if show_source:
            if unit_cents is None:
                print(f"  ↳ source: none found [{used_mode}]")
            else:
                print(f"  ↳ source: {used_mode} id={listing_id} cents={unit_cents} url={src_url}")

        rows.append({
            "name": name,
            "qty": qty,
            "unit_cents": unit_cents,
            "subtotal_cents": subtotal_cents,
            "priced": unit_cents is not None,
            "mode": used_mode,
            "listing_id": listing_id,
            "url": src_url,
        })

    print("-" * (name_w + qty_w + unit_w + sub_w + 6))
    print(f"{'TOTAL':{name_w}}  {'':>{qty_w}}  {'':>{unit_w}}  {total_cents/100:>{sub_w-1},.2f}")
    return total_cents, rows

# ---------- CSV ----------

def write_csv(rows: List[Dict], grand_cents: int, path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "qty", "unit_usd", "subtotal_usd", "priced", "mode", "listing_id"])
        for r in rows:
            w.writerow([
                r["name"], r["qty"],
                f"{(r['unit_cents'] or 0)/100:.2f}",
                f"{r['subtotal_cents']/100:.2f}",
                "yes" if r["priced"] else "no",
                r["mode"], r["listing_id"]
            ])
        w.writerow(["TOTAL", "", "", f"{grand_cents/100:.2f}", "", "", ""])

# ---------- CLI ----------

def main():
    ap = argparse.ArgumentParser(description="Value a Steam CS2 inventory using CSFloat prices only, with sequential output")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--steamid", help="SteamID64")
    g.add_argument("--user", help="Vanity profile name")
    ap.add_argument("--include-unmarketable", action="store_true", help="Count unmarketable items too")
    ap.add_argument("--sleep", type=float, default=DEFAULT_SLEEP, help="Seconds to sleep between CSFloat requests")
    ap.add_argument("--csv", default=None, help="Write results to CSV file")
    ap.add_argument("--key", default=None, help="CSFloat API key override")
    ap.add_argument("--verbose", action="store_true", help="Verbose CSFloat requests")
    ap.add_argument("--probe", action="store_true", help="Probe CSFloat access by fetching site-wide listings")
    args = ap.parse_args()

    token = args.key or os.getenv("CSFLOAT_API_KEY") or os.getenv("FLOAT_TOKEN")
    if not token:
        print("No CSFloat API key found. Set CSFLOAT_API_KEY or pass --key.", file=sys.stderr)
        sys.exit(2)

    cf = CSFloatClient(api_key=token, sleep_sec=args.sleep, verbose=args.verbose)

    # Optional probe to prove your key actually works
    if args.probe:
        data = cf._get({"limit": 5, "sort_by": "most_recent"})
        print(f"[probe] fetched {len(data)} listings total")
        if isinstance(data, list) and data:
            print(f"[probe] first id={data[0].get('id')} price_cents={data[0].get('price')}")
        return

    steamid64 = args.steamid.strip() if args.steamid else resolve_vanity_to_steamid64(args.user)
    assets = fetch_full_inventory(steamid64)
    counts = count_by_name(assets, include_unmarketable=args.include_unmarketable)

    total_cents, rows = sequential_value(counts, cf, show_source=True)

    if args.csv:
        try:
            write_csv(rows, total_cents, args.csv)
            print(f"\nCSV written to {args.csv}")
        except Exception as e:
            print(f"[csv] Could not write CSV: {e}", file=sys.stderr)

if __name__ == "__main__":
    try:
        main()
    except PermissionError as e:
        print(f"[inventory] {e}", file=sys.stderr); sys.exit(3)
    except requests.HTTPError as e:
        print(f"[http] {e}", file=sys.stderr); sys.exit(4)
    except Exception as e:
        print(f"[fatal] {e}", file=sys.stderr); sys.exit(5)
