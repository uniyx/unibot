# cogs/portfolio.py
import os
import io
import json
import math
import asyncio
import datetime as dt
from typing import Dict, List, Tuple, Optional

import discord
from discord import app_commands
from discord.ext import commands

# Optional dependencies
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


def _to_utc_list(index, target_tz: dt.tzinfo) -> List[dt.datetime]:
    """Normalize any pandas.Timestamp or datetime to tz-aware datetimes in target_tz."""
    out: List[dt.datetime] = []
    for ts in list(index):
        py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if py.tzinfo is None:
            # yfinance often yields naive UTC
            py = py.replace(tzinfo=dt.timezone.utc)
        # Always convert to target timezone
        py = py.astimezone(target_tz)
        out.append(py)
    return out


def load_portfolio(path: str) -> Dict[str, int]:
    """Load holdings from YAML or JSON: {SYMBOL: shares}."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Portfolio file not found: {path}")
    _, ext = os.path.splitext(path.lower())
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    if ext in (".yaml", ".yml"):
        if yaml is None:
            raise RuntimeError("YAML file provided but PyYAML is not installed. pip install pyyaml")
        data = yaml.safe_load(text) or {}
    elif ext == ".json":
        data = json.loads(text or "{}")
    else:
        raise ValueError("Unsupported portfolio extension. Use .yaml, .yml, or .json")

    out: Dict[str, int] = {}
    for k, v in data.items():
        out[str(k).upper()] = int(v)
    if not out:
        raise ValueError("Portfolio file has no positions")
    return out


def choose_range(range_name: str, tz: dt.tzinfo) -> Tuple[dt.datetime, dt.datetime, str]:
    now = dt.datetime.now(tz)
    end = now
    rn = range_name.lower()
    if rn == "daily":
        start = now - dt.timedelta(days=30)
        interval = "1d"
    elif rn == "weekly":
        start = now - dt.timedelta(weeks=26)
        interval = "1wk"
    elif rn == "monthly":
        start = now - dt.timedelta(days=365)
        interval = "1mo"
    elif rn == "ytd":
        start = dt.datetime(now.year, 1, 1, tzinfo=tz)
        interval = "1d"
    else:
        raise ValueError("Range must be one of: daily, weekly, monthly, ytd")
    return start, end, interval


async def yf_history_async(ticker: str, start: dt.datetime, end: dt.datetime, interval: str):
    loop = asyncio.get_running_loop()

    def _download():
        return yf.Ticker(ticker).history(
            start=start, end=end, interval=interval, auto_adjust=False, actions=False
        )

    return await loop.run_in_executor(None, _download)


def resample_align(
    series_list: List[Tuple[str, List[dt.datetime], List[float]]]
) -> Tuple[List[dt.datetime], Dict[str, List[float]]]:
    all_ts = sorted(set(ts for _, times, _ in series_list for ts in times))
    aligned: Dict[str, List[float]] = {}
    for sym, times, vals in series_list:
        m = {t: v for t, v in zip(times, vals)}
        last = math.nan
        track: List[float] = []
        for t in all_ts:
            if t in m and not math.isnan(m[t]):
                last = m[t]
            track.append(last)
        first_obs = next((x for x in track if not math.isnan(x)), math.nan)
        track = [first_obs if math.isnan(x) else x for x in track]
        aligned[sym] = track
    return all_ts, aligned


def portfolio_series(aligned_prices: Dict[str, List[float]], shares: Dict[str, int]) -> List[float]:
    n = len(next(iter(aligned_prices.values())))
    total = [0.0] * n
    for sym, price_list in aligned_prices.items():
        qty = shares.get(sym, 0)
        for i, p in enumerate(price_list):
            total[i] += (p or 0.0) * qty
    return total


def ascii_line_chart(values: List[float], width: int = 70, height: int = 12, ylabel: Optional[str] = None) -> str:
    if not values or all(math.isnan(v) for v in values):
        return "(no data)"
    if len(values) > width:
        step = len(values) / width
        xs = [values[int(i * step)] for i in range(width)]
    else:
        xs = values[:]
        width = len(xs)

    vmin = min(xs)
    vmax = max(xs)
    if math.isclose(vmin, vmax):
        vmin -= 1.0
        vmax += 1.0

    def y_for(v):
        return int(round((v - vmin) * (height - 1) / (vmax - vmin)))

    grid = [[" " for _ in range(width)] for _ in range(height)]
    prev_y: Optional[int] = None
    for x, v in enumerate(xs):
        y = y_for(v)
        grid[height - 1 - y][x] = "•"
        if prev_y is not None:
            y0 = height - 1 - prev_y
            y1 = height - 1 - y
            if y0 != y1:
                step = 1 if y1 > y0 else -1
                for yy in range(y0 + step, y1, step):
                    if grid[yy][x] == " ":
                        grid[yy][x] = "│"
        prev_y = y

    top_label = f"{vmax:,.2f}"
    bot_label = f"{vmin:,.2f}"
    label_width = max(len(top_label), len(bot_label))
    lines = []
    for r, row in enumerate(grid):
        if r == 0:
            lab = top_label.rjust(label_width)
        elif r == height - 1:
            lab = bot_label.rjust(label_width)
        else:
            lab = " " * label_width
        lines.append(f"{lab} │ {''.join(row)}")

    if ylabel:
        lines.insert(0, f"{ylabel}")
    lines.append(" " * label_width + " └" + "─" * (width + 1))
    return "\n".join(lines)


def humanize_range_label(start: dt.datetime, end: dt.datetime) -> str:
    s = start.strftime("%Y-%m-%d")
    e = end.strftime("%Y-%m-%d")
    return f"{s} to {e}"


class Portfolio(commands.Cog):
    def __init__(self, bot: commands.Bot, default_file: str = "./data/portfolio.yaml"):
        self.bot = bot
        self.default_file = default_file
        self.tz = dt.timezone.utc

    @GUILDS
    @app_commands.command(
        name="portfolio_chart",
        description="ASCII chart of your portfolio value over time."
    )
    @app_commands.describe(
        range_name="Time range",
        file_path="Path to portfolio file (.yaml, .yml, .json)",
        normalize="Scale to start at 100",
        points="Chart width in points (20 to 160)"
    )
    @app_commands.choices(
        range_name=[
            app_commands.Choice(name="daily", value="daily"),
            app_commands.Choice(name="weekly", value="weekly"),
            app_commands.Choice(name="monthly", value="monthly"),
            app_commands.Choice(name="ytd", value="ytd"),
        ]
    )
    async def portfolio_chart(
        self,
        interaction: discord.Interaction,
        range_name: app_commands.Choice[str],
        file_path: Optional[str] = None,
        normalize: Optional[bool] = False,
        points: Optional[int] = 70,
    ) -> None:
        await interaction.response.defer(thinking=True)

        path = file_path or self.default_file
        try:
            shares = await asyncio.get_running_loop().run_in_executor(None, load_portfolio, path)
        except Exception as e:
            await interaction.followup.send(f"Failed to load portfolio: {e}", ephemeral=True)
            return

        try:
            start, end, interval = choose_range(range_name.value, self.tz)
        except Exception as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        symbols = sorted(shares.keys())
        tasks = [yf_history_async(sym, start, end, interval) for sym in symbols]
        dfs = await asyncio.gather(*tasks)

        series_list: List[Tuple[str, List[dt.datetime], List[float]]] = []
        for sym, df in zip(symbols, dfs):
            if df is None or df.empty or "Close" not in df.columns:
                await interaction.followup.send(f"No price data for {sym} in the requested range.", ephemeral=True)
                return
            # Fixed: robust datetime conversion
            times = _to_utc_list(df.index, self.tz)
            closes = [float(x) if x == x else math.nan for x in df["Close"].tolist()]
            series_list.append((sym, times, closes))

        _, aligned = resample_align(series_list)
        total_values = portfolio_series(aligned, shares)

        ylabel = "Portfolio value (USD)"
        if normalize and total_values:
            base = total_values[0]
            if base == 0 or math.isnan(base):
                await interaction.followup.send("Cannot normalize because the first value is zero or NaN.", ephemeral=True)
                return
            total_values = [100.0 * v / base for v in total_values]
            ylabel = "Portfolio index (start = 100)"

        width = max(20, min(160, int(points or 70)))

        header_lines = []
        for sym in symbols:
            px = aligned[sym]
            if not px or px[0] in (0.0, math.nan):
                chg = 0.0
            else:
                chg = (px[-1] / px[0] - 1.0) * 100.0
            header_lines.append(f"{sym}: {shares[sym]} shares [{chg:+.2f}%]")

        label = humanize_range_label(start, end)
        chart = ascii_line_chart(total_values, width=width, height=12, ylabel=ylabel)

        buf = io.StringIO()
        buf.write("Portfolio Chart\n")
        buf.write(f"Range: {range_name.value.upper()}  ({label})\n")
        buf.write("Positions: " + " | ".join(header_lines) + "\n")
        buf.write("```\n")
        buf.write(chart)
        buf.write("\n```\n")

        await interaction.followup.send(buf.getvalue())


async def setup(bot: commands.Bot):
    await bot.add_cog(Portfolio(bot))
