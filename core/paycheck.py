"""Pure paycheck accrual calculations for the personal /paycheck command."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo


PAY_TIMEZONE = ZoneInfo("America/New_York")
EMPLOYMENT_START = datetime(2026, 8, 1, tzinfo=PAY_TIMEZONE)

PAYCHECK_GROSS = Decimal("3175.00")
PAYCHECK_TAX = Decimal("718.68")
PAYCHECK_NET = Decimal("2456.32")

LIVE_UPDATE_SECONDS = 60
LIVE_REFRESH_SECONDS = 3

_SECONDS_PER_DAY = Decimal(86400)
_MONEY_PRECISION = 50


@dataclass(frozen=True)
class PayPeriod:
    """A semi-monthly period represented as a local-time half-open interval."""

    start: datetime
    end: datetime

    @property
    def pay_date(self) -> date:
        return self.end.date() - timedelta(days=1)


def to_pay_timezone(timestamp: datetime) -> datetime:
    """Return ``timestamp`` in the payroll timezone.

    Naive timestamps are treated as local payroll time so calculation tests can
    use concise timestamps without silently using the machine's timezone.
    """

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        return timestamp.replace(tzinfo=PAY_TIMEZONE)
    return timestamp.astimezone(PAY_TIMEZONE)


def _at_midnight(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=PAY_TIMEZONE)


def _first_of_next_month(day: date) -> date:
    if day.month == 12:
        return date(day.year + 1, 1, 1)
    return date(day.year, day.month + 1, 1)


def get_pay_period(timestamp: datetime) -> PayPeriod:
    """Return the period containing ``timestamp``.

    Period ends are exclusive: exactly 00:00 on the 16th belongs to the
    16th-through-month-end period.
    """

    local = to_pay_timezone(timestamp)
    if local.day <= 15:
        start_day = date(local.year, local.month, 1)
        end_day = date(local.year, local.month, 16)
    else:
        start_day = date(local.year, local.month, 16)
        end_day = _first_of_next_month(start_day)
    return PayPeriod(_at_midnight(start_day), _at_midnight(end_day))


def get_next_pay_date(timestamp: datetime) -> date:
    """Return the next applicable period-end paycheck date."""

    local = to_pay_timezone(timestamp)
    if local < EMPLOYMENT_START:
        return get_pay_period(EMPLOYMENT_START).pay_date
    return get_pay_period(local).pay_date


def count_weekdays(period: PayPeriod) -> int:
    """Count Monday-Friday calendar dates in ``period``."""

    days = (period.end.date() - period.start.date()).days
    return sum(
        (period.start.date() + timedelta(days=offset)).weekday() < 5
        for offset in range(days)
    )


def count_eligible_duration(start: datetime, end: datetime) -> Decimal:
    """Return eligible local wall-clock seconds between two timestamps."""

    start_local = to_pay_timezone(start).replace(tzinfo=None)
    end_local = to_pay_timezone(end).replace(tzinfo=None)
    if end_local <= start_local:
        return Decimal(0)

    total = Decimal(0)
    current_day = start_local.date()
    last_day = end_local.date()
    while current_day <= last_day:
        day_start = datetime.combine(current_day, time.min)
        day_end = day_start + timedelta(days=1)
        segment_start = max(start_local, day_start)
        segment_end = min(end_local, day_end)
        if current_day.weekday() < 5 and segment_end > segment_start:
            delta = segment_end - segment_start
            total += Decimal(delta.days * 86400 + delta.seconds)
            if delta.microseconds:
                total += Decimal(delta.microseconds) / Decimal(1_000_000)
        current_day += timedelta(days=1)
    return total


def get_period_rate(period: PayPeriod) -> Decimal:
    """Return gross dollars per eligible second for ``period``.

    Each period gets its own rate so all eligible weekday seconds in that
    period sum to exactly the authoritative $3,175.00 gross paycheck.
    """

    eligible_seconds = Decimal(count_weekdays(period)) * _SECONDS_PER_DAY
    if not eligible_seconds:
        return Decimal(0)
    with localcontext() as context:
        context.prec = _MONEY_PRECISION
        return PAYCHECK_GROSS / eligible_seconds


def calculate_period_earnings(
    period: PayPeriod,
    timestamp: datetime,
    employment_start: datetime = EMPLOYMENT_START,
) -> Decimal:
    """Calculate gross earned in ``period`` at ``timestamp``."""

    now = to_pay_timezone(timestamp)
    period_start = to_pay_timezone(period.start)
    period_end = to_pay_timezone(period.end)
    start = max(period_start, to_pay_timezone(employment_start))

    if now <= start:
        return Decimal(0)
    if now >= period_end:
        return PAYCHECK_GROSS if period_end > start else Decimal(0)

    eligible_seconds = count_eligible_duration(start, min(now, period_end))
    if not eligible_seconds:
        return Decimal(0)

    with localcontext() as context:
        context.prec = _MONEY_PRECISION
        earned = get_period_rate(period) * eligible_seconds
    return min(PAYCHECK_GROSS, earned)


def _next_period(period: PayPeriod) -> PayPeriod:
    return get_pay_period(period.end)


def calculate_total_gross(timestamp: datetime) -> Decimal:
    """Calculate total gross earned since employment began."""

    now = to_pay_timezone(timestamp)
    if now <= EMPLOYMENT_START:
        return Decimal(0)

    first_period = get_pay_period(EMPLOYMENT_START)
    current_period = get_pay_period(now)
    total = Decimal(0)

    # There are only two small calendar periods per month, so this avoids
    # second-by-second work while retaining exact period-boundary behavior.
    with localcontext() as context:
        context.prec = _MONEY_PRECISION
        period = first_period
        while period.start < current_period.start:
            total += calculate_period_earnings(period, period.end)
            period = _next_period(period)
        total += calculate_period_earnings(current_period, now)
    return total


def calculate_estimated_tax(gross: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _MONEY_PRECISION
        return (gross / PAYCHECK_GROSS) * PAYCHECK_TAX


def calculate_estimated_net(gross: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _MONEY_PRECISION
        return (gross / PAYCHECK_GROSS) * PAYCHECK_NET


def is_earning_time(timestamp: datetime) -> bool:
    """Return whether weekday accrual is active at ``timestamp``."""

    local = to_pay_timezone(timestamp)
    return local >= EMPLOYMENT_START and local.weekday() < 5
