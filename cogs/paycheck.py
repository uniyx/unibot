"""Discord presentation for the /paycheck snapshot command."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import discord
from discord import app_commands
from discord.ext import commands

from core.discord_utils import guilds_decorator
from core.paycheck import (
    EMPLOYMENT_START,
    PAYCHECK_GROSS,
    PAY_TIMEZONE,
    WORKDAY_END,
    WORKDAY_START,
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
    elif now.weekday() >= 5:
        status = "\N{DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16} WEEKEND / PAUSED"
        color = discord.Color.gold()
    else:
        status = "\N{DOUBLE VERTICAL BAR}\N{VARIATION SELECTOR-16} OUTSIDE WORK HOURS"
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
            f"Workday           {_money(rate * Decimal(8 * 60 * 60))}\n"
            f"Schedule          {WORKDAY_START.strftime('%H:%M')} - {WORKDAY_END.strftime('%H:%M')} ET\n"
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
    embed.set_footer(text="Snapshot at invocation • Gross based on actual paycheck")
    return embed


class PaycheckCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @GUILDS
    @app_commands.command(name="paycheck", description="Show paycheck accrual now.")
    async def paycheck(self, interaction: discord.Interaction) -> None:
        now = datetime.now(PAY_TIMEZONE)
        await interaction.response.send_message(
            embed=build_paycheck_embed(now)
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PaycheckCog(bot))
