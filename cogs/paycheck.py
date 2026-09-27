"""Discord presentation and bounded live refreshes for /paycheck."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import discord
from discord import app_commands
from discord.ext import commands

from core.discord_utils import guilds_decorator
from core.paycheck import (
    EMPLOYMENT_START,
    LIVE_REFRESH_SECONDS,
    LIVE_UPDATE_SECONDS,
    PAYCHECK_GROSS,
    PAY_TIMEZONE,
    calculate_estimated_net,
    calculate_estimated_tax,
    calculate_period_earnings,
    calculate_total_gross,
    count_weekdays,
    get_next_pay_date,
    get_pay_period,
    get_period_rate,
    is_earning_time,
    to_pay_timezone,
)


logger = logging.getLogger(__name__)
GUILDS = guilds_decorator()
_FILLED = "\N{FULL BLOCK}"
_EMPTY = "\N{LIGHT SHADE}"


def _money(value: Decimal) -> str:
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"${rounded:,.2f}"


def _rate(value: Decimal, places: int) -> str:
    quantum = Decimal(1).scaleb(-places)
    rounded = value.quantize(quantum, rounding=ROUND_HALF_UP)
    return f"${rounded:,.{places}f}"


def progress_bar(percent: Decimal, width: int = 20) -> str:
    """Render a compact, deterministic Unicode progress bar."""

    percent = max(Decimal(0), min(Decimal(100), percent))
    filled = int(
        (percent * Decimal(width) / Decimal(100)).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    return f"{_FILLED * filled}{_EMPTY * (width - filled)} {percent:.2f}%"


def _short_date(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}"


def _long_date(value: date) -> str:
    return f"{value.strftime('%B')} {value.day}, {value.year}"


def build_paycheck_embed(timestamp: datetime) -> discord.Embed:
    now = to_pay_timezone(timestamp)
    period = get_pay_period(now)
    total_gross = calculate_total_gross(now)
    period_gross = calculate_period_earnings(period, now)
    period_remaining = max(Decimal(0), PAYCHECK_GROSS - period_gross)
    period_percent = (period_gross / PAYCHECK_GROSS * Decimal(100)) if PAYCHECK_GROSS else Decimal(0)
    rate = get_period_rate(period)

    if now < EMPLOYMENT_START:
        status = "\N{HOURGLASS WITH FLOWING SAND} NOT STARTED"
        color = discord.Color.blurple()
    elif is_earning_time(now):
        status = "\N{LARGE GREEN CIRCLE} EARNING"
        color = discord.Color.green()
    else:
        status = "\N{DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16} WEEKEND / PAUSED"
        color = discord.Color.gold()

    period_end_date = period.end.date() - timedelta(days=1)
    embed = discord.Embed(
        title="\N{MONEY BAG} Gian's Paycheck",
        color=color,
        timestamp=now,
    )
    embed.add_field(
        name="Total earned",
        value=(
            f"Gross             {_money(total_gross)}\n"
            f"Est. taxes        {_money(calculate_estimated_tax(total_gross))}\n"
            f"Est. take-home    {_money(calculate_estimated_net(total_gross))}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Current paycheck",
        value=(
            f"{_short_date(period.start.date())} - {_short_date(period_end_date)}\n"
            f"Earned            {_money(period_gross)}\n"
            f"Remaining         {_money(period_remaining)}\n"
            f"Progress           {progress_bar(period_percent)}"
        ),
        inline=False,
    )
    embed.add_field(
        name="Current rate",
        value=(
            f"Second            {_rate(rate, 8)}/sec\n"
            f"Minute            {_rate(rate * Decimal(60), 5)}/min\n"
            f"Hour              {_rate(rate * Decimal(3600), 2)}/hr\n"
            f"Weekday           {_money(rate * Decimal(86400))}\n"
            f"Eligible weekdays {count_weekdays(period)}"
        ),
        inline=True,
    )
    embed.add_field(
        name="Next paycheck",
        value=(
            f"{_long_date(get_next_pay_date(now))}\n"
            f"Status            {status}"
        ),
        inline=True,
    )
    embed.set_footer(text="Updates every 3s for 60s • Gross based on actual paycheck")
    return embed


class PaycheckCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._live_tasks: set[asyncio.Task[None]] = set()

    def cog_unload(self) -> None:
        for task in self._live_tasks:
            task.cancel()
        self._live_tasks.clear()

    @GUILDS
    @app_commands.command(name="paycheck", description="Show live paycheck accrual.")
    async def paycheck(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=build_paycheck_embed(datetime.now(PAY_TIMEZONE))
        )
        task = asyncio.create_task(self._refresh(interaction))
        self._live_tasks.add(task)
        task.add_done_callback(self._live_tasks.discard)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        deadline = asyncio.get_running_loop().time() + LIVE_UPDATE_SECONDS
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(LIVE_REFRESH_SECONDS, remaining))
                await interaction.edit_original_response(
                    embed=build_paycheck_embed(datetime.now(PAY_TIMEZONE))
                )
                if asyncio.get_running_loop().time() >= deadline:
                    break
        except asyncio.CancelledError:
            raise
        except discord.NotFound:
            logger.info("Paycheck message was deleted before live updates finished")
        except discord.HTTPException:
            logger.warning("Stopping paycheck live updates after a Discord error", exc_info=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PaycheckCog(bot))
