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


DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID", "0")) or None
GUILDS = app_commands.guilds(discord.Object(id=DEV_GUILD_ID)) if DEV_GUILD_ID else (lambda f: f)


def load_portfolio_file(path: str) -> Dict[str, int]:
    """
    Read the holdings file.
    Expected YAML / JSON: {SYMBOL: shares}
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


async def fetch_last_two_closes(ticker: str) -> Optional[Tuple[float, float]]:
    """
    Return (prev_close, last_close) for ticker using yfinance daily candles.
    We ask for up to 5 days to safely span weekends.
    If we cannot get two valid closes, return None.
    """
    loop = asyncio.get_running_loop()

    def _download():
        # yfinance quirk: .history(period="5d", interval="1d") already adjusts for most nonsense.
        return yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=False, actions=False)

    df = await loop.run_in_executor(None, _download)

    if df is None or df.empty or "Close" not in df.columns:
        return None

    closes: List[float] = [float(x) for x in df["Close"].tolist() if not (x is None or math.isnan(x))]
    if len(closes) < 2:
        return None

    # last two closes in chronological order, so we take the final pair
    prev_close = closes[-2]
    last_close = closes[-1]
    return (prev_close, last_close)


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


class Portfolio(commands.Cog):
    """
    /portfolio_daily:
    - Reads holdings from the Samba-mounted YAML
    - Pulls yesterday close vs latest close from yfinance
    - Reports per-ticker daily change and total portfolio P/L for the day
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Preferred override via env, lets you redirect without editing code.
        env_path = os.getenv("PORTFOLIO_FILE", "").strip()
        if env_path:
            self.portfolio_file = env_path
        else:
            # Match tweets.py convention: host /mnt/shared/unibot -> container /unibot
            self.portfolio_file = "/unibot/data/portfolio.yaml"

        # For timestamp display only. We are not converting prices to tz, because we're just
        # using discrete daily close values.
        self.tz = dt.timezone.utc

    @GUILDS
    @app_commands.command(
        name="portfolio_daily",
        description="Show today's portfolio move and per-ticker daily change."
    )
    async def portfolio_daily(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)

        # 1. Load positions
        try:
            holdings = load_portfolio_file(self.portfolio_file)
        except Exception as e:
            await interaction.followup.send(f"Failed to load portfolio: {e}", ephemeral=True)
            return

        symbols = sorted(holdings.keys())

        # 2. Pull last two closes for each ticker
        fetch_tasks = {
            sym: fetch_last_two_closes(sym)
            for sym in symbols
        }
        results = await asyncio.gather(*fetch_tasks.values())

        # Map symbol -> (prev_close, last_close) or None
        sym_to_prices: Dict[str, Optional[Tuple[float, float]]] = {}
        for (sym, task_result) in zip(fetch_tasks.keys(), results):
            sym_to_prices[sym] = task_result

        # 3. Compute per-ticker changes and also aggregate total portfolio value
        lines: List[str] = []
        total_prev_val = 0.0
        total_curr_val = 0.0

        for sym in symbols:
            qty = holdings[sym]
            prices = sym_to_prices.get(sym)

            if not prices:
                # We couldn't get valid data for this ticker; surface it clearly
                lines.append(f"{sym}: {qty} shares [no data]")
                continue

            prev_close, last_close = prices
            prev_val = prev_close * qty
            curr_val = last_close * qty
            chg_abs = curr_val - prev_val
            chg_pct = pct_change(prev_close, last_close)
            emoji = pick_emoji(chg_abs)

            total_prev_val += prev_val
            total_curr_val += curr_val

            lines.append(
                f"{emoji} {sym}: {qty} sh "
                f"${last_close:,.2f} ({chg_abs:+.2f} USD, {chg_pct:+.2f}%)"
            )

        # 4. Compute total portfolio change
        if total_prev_val == 0:
            total_chg_abs = 0.0
            total_chg_pct = 0.0
        else:
            total_chg_abs = total_curr_val - total_prev_val
            total_chg_pct = pct_change(total_prev_val, total_curr_val)

        total_emoji = pick_emoji(total_chg_abs)

        # 5. Format output
        # Present:
        #   - Timestamp (UTC)
        #   - Total current value and daily delta
        #   - Per-ticker breakdown
        now_utc = dt.datetime.now(self.tz).strftime("%Y-%m-%d %H:%M UTC")

        header = (
            f"{total_emoji} Portfolio daily move ({now_utc}):\n"
            f"Total: ${total_curr_val:,.2f} "
            f"({total_chg_abs:+.2f} USD, {total_chg_pct:+.2f}%)\n"
            "Tickers:"
        )

        body = "\n".join(lines)

        msg = f"{header}\n{body}"

        await interaction.followup.send(msg)


async def setup(bot: commands.Bot):
    await bot.add_cog(Portfolio(bot))
