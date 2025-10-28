import os
import json
import math
import asyncio
import datetime as dt
from typing import Dict, Optional, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands

try:
    import yaml  # type: ignore
except Exception:
    yaml = None

try:
    import yfinance as yf  # type: ignore
except Exception as e:
    raise RuntimeError("This cog requires 'yfinance'. Install with: pip install yfinance") from e

try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except Exception:
    ZoneInfo = None


DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
GUILDS = app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)


def load_portfolio_file(path: str) -> Dict[str, int]:
    """
    Read the holdings file.
    Expected YAML / JSON: {SYMBOL: shares}
    Example:
        RYCEY: 635
        PLTR: 100
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Portfolio file not found: {path}")

    _, ext = os.path.splitext(path.lower())
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    if ext in (".yaml", ".yml"):
        if yaml is None:
            raise RuntimeError("PyYAML is not installed. pip install pyyaml")
        data = yaml.safe_load(raw) or {}
    elif ext == ".json":
        data = json.loads(raw or "{}")
    else:
        raise ValueError("Unsupported portfolio extension. Use .yaml, .yml, or .json")

    out: Dict[str, int] = {}
    for sym, qty in data.items():
        out[str(sym).upper()] = int(qty)
    if not out:
        raise ValueError("Portfolio file has no positions")
    return out


def pct_change(prev_val: float, curr_val: float) -> float:
    if prev_val == 0:
        return 0.0
    return (curr_val / prev_val - 1.0) * 100.0


def pick_emoji(delta: float) -> str:
    if delta > 0:
        return "🟢⬆"
    elif delta < 0:
        return "🔴⬇"
    else:
        return "⚪"


def _color_for_delta(delta: float) -> discord.Color:
    if delta > 0:
        return discord.Color.green()
    if delta < 0:
        return discord.Color.red()
    return discord.Color.dark_grey()


def market_is_open(now_utc: dt.datetime) -> bool:
    """
    Regular session 09:30 <= t < 16:00 America/New_York, Monday to Friday.
    Holidays/half-days ignored. If tz fails, assume closed.
    """
    if ZoneInfo is None:
        return False

    ny_tz = ZoneInfo("America/New_York")
    now_ny = now_utc.astimezone(ny_tz)

    if now_ny.weekday() > 4:
        return False

    hm = now_ny.hour * 60 + now_ny.minute
    open_min = 9 * 60 + 30      # 09:30
    close_min = 16 * 60         # 16:00
    return open_min <= hm < close_min


async def fetch_prev_close_and_mark_intraday(ticker: str) -> Optional[Tuple[float, float]]:
    """
    Try to return (prev_close, current_price) intraday using fast_info/info.
    Return None if we can't get sane values.
    """
    loop = asyncio.get_running_loop()

    def _download():
        t = yf.Ticker(ticker)

        prev_close = None
        curr = None

        # First attempt: fast_info (cheap, intended for live-ish quotes)
        fast = getattr(t, "fast_info", None)
        if fast and isinstance(fast, dict):
            prev_close = fast.get("previousClose")
            curr = fast.get("lastPrice") or fast.get("regularMarketPrice")

        # Fallback: .info (slower, sometimes throttled)
        if (prev_close is None or curr is None) and hasattr(t, "info"):
            try:
                inf = t.info
            except Exception:
                inf = {}
            if prev_close is None:
                prev_close = inf.get("regularMarketPreviousClose") or inf.get("previousClose")
            if curr is None:
                curr = (
                    inf.get("regularMarketPrice")
                    or inf.get("currentPrice")
                    or inf.get("ask")
                )

        if prev_close is None or curr is None:
            return None

        return float(prev_close), float(curr)

    return await loop.run_in_executor(None, _download)


async def fetch_last_two_closes(ticker: str) -> Optional[Tuple[float, float]]:
    """
    Fallback for after-hours / when live data isn't available.
    Return (prev_close, last_close) from daily candles.
    """
    loop = asyncio.get_running_loop()

    def _download():
        return yf.Ticker(ticker).history(
            period="5d",
            interval="1d",
            auto_adjust=False,
            actions=False
        )

    df = await loop.run_in_executor(None, _download)

    if df is None or df.empty or "Close" not in df.columns:
        return None

    closes: List[float] = [
        float(x)
        for x in df["Close"].tolist()
        if x is not None and not math.isnan(x)
    ]
    if len(closes) < 2:
        return None

    prev_close = closes[-2]
    last_close = closes[-1]
    return (prev_close, last_close)


async def get_price_pair_for_symbol(sym: str, intraday_ok: bool) -> Optional[Tuple[float, float]]:
    """
    Return (prev_close, current_or_last_close) for symbol.
    If intraday_ok is True, try live first, else fall back to daily close.
    """
    if intraday_ok:
        pair = await fetch_prev_close_and_mark_intraday(sym)
        if pair is not None:
            return pair
    return await fetch_last_two_closes(sym)


def _format_rows_pretty(rows: List[Dict[str, str]]) -> str:
    """
    Return a monospaced mini-table inside a code block.
    """
    if not rows:
        return "```(no positions)```"

    sym_w = max(len(r["sym"]) for r in rows)
    qty_w = max(len(r["qty"]) for r in rows + [{"qty": "QTY"}])
    price_w = max(len(r["price"]) for r in rows + [{"price": "LAST"}])
    abs_w = max(len(r["abs"]) for r in rows + [{"abs": "Δ USD"}])
    pct_w = max(len(r["pct"]) for r in rows + [{"pct": "Δ %"}])

    header = (
        f"{'':2} "
        f"{'SYM'.ljust(sym_w)}  "
        f"{'QTY'.rjust(qty_w)}  "
        f"{'LAST'.rjust(price_w)}  "
        f"{'Δ USD'.rjust(abs_w)}  "
        f"{'Δ %'.rjust(pct_w)}"
    )
    sep = "-" * len(header)

    body_lines: List[str] = []
    for r in rows:
        body_lines.append(
            f"{r['emoji']:2} "
            f"{r['sym'].ljust(sym_w)}  "
            f"{r['qty'].rjust(qty_w)}  "
            f"{r['price'].rjust(price_w)}  "
            f"{r['abs'].rjust(abs_w)}  "
            f"{r['pct'].rjust(pct_w)}"
        )

    return "```\n" + header + "\n" + sep + "\n" + "\n".join(body_lines) + "\n```"


def _color_for_total_change(delta_abs: float) -> discord.Color:
    return _color_for_delta(delta_abs)


class Portfolio(commands.Cog):
    """
    /portfolio_daily

    Behavior:
    - During regular US market hours (09:30-16:00 ET, Mon-Fri):
        Use live mark (prevClose vs current price). Labeled LIVE (intraday).
    - Otherwise:
        Use last two closes (close-to-close). Labeled CLOSE/CLOSE.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        env_path = (os.getenv("PORTFOLIO_FILE") or "").strip()
        if env_path:
            self.portfolio_file = env_path
        else:
            if os.name == "nt":
                self.portfolio_file = r"F:\unibot\data\portfolio.yaml"
            else:
                self.portfolio_file = "/unibot/data/portfolio.yaml"

        self.utc_tz = dt.timezone.utc
        self.ny_tz = ZoneInfo("America/New_York") if ZoneInfo else None

    @GUILDS
    @app_commands.command(
        name="portfolio_daily",
        description="Portfolio move and per-ticker change (live if market open)."
    )
    async def portfolio_daily(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        # Load holdings
        try:
            holdings = load_portfolio_file(self.portfolio_file)
        except Exception as e:
            await interaction.followup.send(f"Failed to load portfolio: {e}", ephemeral=True)
            return

        symbols = sorted(holdings.keys())

        # Time references
        now_utc = dt.datetime.now(self.utc_tz)
        intraday_ok = market_is_open(now_utc)

        # We'll show human-readable timestamp in ET (New York)
        if self.ny_tz:
            now_local = now_utc.astimezone(self.ny_tz)
            ts_display = now_local.strftime("%Y-%m-%d %H:%M %Z")
        else:
            # Fallback to UTC label if ZoneInfo is not available
            ts_display = now_utc.strftime("%Y-%m-%d %H:%M UTC")

        # Get price data
        fetch_tasks = {sym: get_price_pair_for_symbol(sym, intraday_ok) for sym in symbols}
        results = await asyncio.gather(*fetch_tasks.values())
        sym_to_prices: Dict[str, Optional[Tuple[float, float]]] = {}
        for sym, task_result in zip(fetch_tasks.keys(), results):
            sym_to_prices[sym] = task_result

        total_prev_val = 0.0
        total_curr_val = 0.0
        table_rows: List[Dict[str, str]] = []

        for sym in symbols:
            qty = holdings[sym]
            pair = sym_to_prices.get(sym)

            if not pair:
                table_rows.append({
                    "emoji": "❓",
                    "sym": sym,
                    "qty": str(qty),
                    "price": "n/a",
                    "abs": "n/a",
                    "pct": "n/a",
                })
                continue

            prev_px, curr_px = pair
            prev_val = prev_px * qty
            curr_val = curr_px * qty

            chg_abs = curr_val - prev_val
            chg_pct = pct_change(prev_px, curr_px)
            emoji = pick_emoji(chg_abs)

            total_prev_val += prev_val
            total_curr_val += curr_val

            table_rows.append({
                "emoji": emoji,
                "sym": sym,
                "qty": str(qty),
                "price": f"{curr_px:,.2f}",
                "abs": f"{chg_abs:+.2f}",
                "pct": f"{chg_pct:+.2f}%",
            })

        if total_prev_val == 0:
            total_chg_abs = 0.0
            total_chg_pct = 0.0
        else:
            total_chg_abs = total_curr_val - total_prev_val
            total_chg_pct = pct_change(total_prev_val, total_curr_val)

        total_emoji = pick_emoji(total_chg_abs)
        mode_label = "LIVE (intraday)" if intraday_ok else "CLOSE/CLOSE"
        table_block = _format_rows_pretty(table_rows)

        embed = discord.Embed(
            title="Portfolio Daily Move",
            description=f"{total_emoji} As of {ts_display}",
            color=_color_for_total_change(total_chg_abs),
            timestamp=now_utc  # Discord renders this in client-local time
        )

        embed.add_field(
            name="Mode",
            value=f"`{mode_label}`",
            inline=True
        )
        embed.add_field(
            name="Total Value",
            value=f"`${total_curr_val:,.2f}`",
            inline=True
        )
        embed.add_field(
            name="P/L Today",
            value=f"`{total_chg_abs:+.2f} USD`\n`{total_chg_pct:+.2f}%`",
            inline=True
        )

        embed.add_field(
            name="Holdings",
            value=table_block,
            inline=False
        )

        embed.set_footer(
            text=f"File: {self.portfolio_file}"
        )

        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Portfolio(bot))
