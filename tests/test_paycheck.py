import unittest
from datetime import datetime
from decimal import Decimal, localcontext

from cogs.paycheck import build_paycheck_embed, progress_bar
from core.paycheck import (
    EMPLOYMENT_START,
    PAYCHECK_GROSS,
    PAYCHECK_NET,
    PAYCHECK_TAX,
    PAY_TIMEZONE,
    WORKDAY_SECONDS,
    calculate_estimated_net,
    calculate_estimated_tax,
    calculate_period_earnings,
    calculate_total_gross,
    count_eligible_duration,
    count_weekdays,
    get_next_pay_date,
    get_pay_period,
    get_period_rate,
    is_earning_time,
)


def eastern(year, month, day, hour=0, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=PAY_TIMEZONE)


class PaycheckCalculationTests(unittest.TestCase):
    def test_period_boundaries_are_half_open(self):
        first = get_pay_period(eastern(2026, 9, 15, 23, 59, 59))
        second = get_pay_period(eastern(2026, 9, 16))

        self.assertEqual(first.start, eastern(2026, 9, 1))
        self.assertEqual(first.end, eastern(2026, 9, 16))
        self.assertEqual(second.start, eastern(2026, 9, 16))
        self.assertEqual(second.end, eastern(2026, 10, 1))

    def test_full_period_is_exactly_one_paycheck(self):
        period = get_pay_period(eastern(2026, 8, 20))

        self.assertEqual(count_weekdays(period), 11)
        self.assertEqual(calculate_period_earnings(period, period.end), PAYCHECK_GROSS)
        with localcontext() as context:
            context.prec = 50
            self.assertAlmostEqual(
                get_period_rate(period) * Decimal(count_weekdays(period)) * WORKDAY_SECONDS,
                PAYCHECK_GROSS,
                places=45,
            )

    def test_weekend_freezes_at_saturday_midnight(self):
        period = get_pay_period(eastern(2026, 9, 3))
        friday = calculate_period_earnings(period, eastern(2026, 9, 4, 16, 59, 59))
        friday_end = calculate_period_earnings(period, eastern(2026, 9, 4, 17))
        saturday = calculate_period_earnings(period, eastern(2026, 9, 5))
        sunday = calculate_period_earnings(period, eastern(2026, 9, 6, 23, 59, 59))

        self.assertGreater(friday, Decimal(0))
        self.assertEqual(friday_end, saturday)
        self.assertEqual(saturday, sunday)
        self.assertEqual(count_eligible_duration(eastern(2026, 9, 5), eastern(2026, 9, 7)), Decimal(0))
        self.assertFalse(is_earning_time(eastern(2026, 9, 5)))

    def test_monday_resumes_at_nine(self):
        period = get_pay_period(eastern(2026, 9, 3))
        sunday = calculate_period_earnings(period, eastern(2026, 9, 6, 23, 59, 59))
        monday_before_work = calculate_period_earnings(period, eastern(2026, 9, 7, 8, 59, 59))
        monday = calculate_period_earnings(period, eastern(2026, 9, 7, 9, 0, 1))

        self.assertEqual(monday_before_work, sunday)
        self.assertGreater(monday, sunday)
        self.assertFalse(is_earning_time(eastern(2026, 9, 7, 8, 59, 59)))
        self.assertTrue(is_earning_time(eastern(2026, 9, 7, 9)))

    def test_overnight_is_paused_after_workday(self):
        period = get_pay_period(eastern(2026, 9, 3))
        friday_end = calculate_period_earnings(period, eastern(2026, 9, 4, 17))
        friday_night = calculate_period_earnings(period, eastern(2026, 9, 4, 23, 59, 59))

        self.assertEqual(friday_end, friday_night)
        self.assertFalse(is_earning_time(eastern(2026, 9, 4, 17)))

    def test_weekday_counts_change_rates(self):
        ten_weekday_period = get_pay_period(eastern(2026, 8, 1))
        eleven_weekday_period = get_pay_period(eastern(2026, 8, 20))

        self.assertEqual(count_weekdays(ten_weekday_period), 10)
        self.assertEqual(count_weekdays(eleven_weekday_period), 11)
        self.assertNotEqual(get_period_rate(ten_weekday_period), get_period_rate(eleven_weekday_period))

    def test_total_is_completed_periods_plus_current_partial(self):
        boundary = eastern(2026, 9, 16)
        current = eastern(2026, 9, 20)
        period = get_pay_period(current)

        self.assertEqual(calculate_total_gross(boundary), Decimal("9525.00"))
        with localcontext() as context:
            context.prec = 50
            self.assertEqual(
                calculate_total_gross(current),
                Decimal("9525.00") + calculate_period_earnings(period, current),
            )

    def test_no_earnings_before_start_and_first_weekday(self):
        self.assertEqual(calculate_total_gross(eastern(2026, 7, 31, 23, 59, 59)), Decimal(0))
        self.assertEqual(calculate_total_gross(EMPLOYMENT_START), Decimal(0))
        self.assertEqual(calculate_total_gross(eastern(2026, 8, 2, 23, 59, 59)), Decimal(0))
        self.assertEqual(calculate_total_gross(eastern(2026, 8, 3, 8, 59, 59)), Decimal(0))
        self.assertGreater(calculate_total_gross(eastern(2026, 8, 3, 9, 0, 1)), Decimal(0))

    def test_calendar_edges(self):
        february = get_pay_period(eastern(2027, 2, 28, 12))
        leap_february = get_pay_period(eastern(2028, 2, 29, 12))
        december = get_pay_period(eastern(2026, 12, 31, 12))
        january = get_pay_period(eastern(2027, 1, 1))

        self.assertEqual(february.end, eastern(2027, 3, 1))
        self.assertEqual(leap_february.end, eastern(2028, 3, 1))
        self.assertEqual(calculate_period_earnings(february, february.end), PAYCHECK_GROSS)
        self.assertEqual(calculate_period_earnings(leap_february, leap_february.end), PAYCHECK_GROSS)
        self.assertEqual(december.end, eastern(2027, 1, 1))
        self.assertEqual(january.start, december.end)

    def test_long_running_total_has_no_period_rounding_drift(self):
        end = eastern(2031, 1, 1)
        expected = Decimal(0)
        period = get_pay_period(EMPLOYMENT_START)
        while period.start < end:
            expected += PAYCHECK_GROSS
            period = get_pay_period(period.end)

        self.assertEqual(calculate_total_gross(end), expected)

    def test_tax_and_net_estimates_use_decimal_ratios(self):
        gross = Decimal("12345.6789")
        tax = calculate_estimated_tax(gross)
        net = calculate_estimated_net(gross)

        self.assertIsInstance(tax, Decimal)
        self.assertIsInstance(net, Decimal)
        self.assertAlmostEqual(tax + net, gross, places=40)
        self.assertEqual(calculate_estimated_tax(PAYCHECK_GROSS), PAYCHECK_TAX)
        self.assertEqual(calculate_estimated_net(PAYCHECK_GROSS), PAYCHECK_NET)

    def test_repeated_calculation_is_deterministic(self):
        timestamp = eastern(2029, 6, 17, 13, 14, 15)

        self.assertEqual(calculate_total_gross(timestamp), calculate_total_gross(timestamp))
        self.assertEqual(
            calculate_period_earnings(get_pay_period(timestamp), timestamp),
            calculate_period_earnings(get_pay_period(timestamp), timestamp),
        )

    def test_next_pay_date_before_employment_is_first_pay_date(self):
        self.assertEqual(get_next_pay_date(eastern(2026, 7, 1)), eastern(2026, 8, 15).date())
        self.assertEqual(get_next_pay_date(eastern(2026, 9, 20)), eastern(2026, 9, 30).date())


class PaycheckPresentationTests(unittest.TestCase):
    def test_progress_bar_is_clamped_and_fixed_width(self):
        self.assertEqual(progress_bar(Decimal("0")), "░" * 20 + " 0.00%")
        self.assertEqual(progress_bar(Decimal("100")), "█" * 20 + " 100.00%")
        self.assertEqual(progress_bar(Decimal("150")).count("█"), 20)

    def test_embed_contains_paycheck_sections(self):
        embed = build_paycheck_embed(eastern(2026, 9, 21, 12))
        fields = {field.name: field.value for field in embed.fields}

        self.assertEqual(embed.title, "💰 Gian's Paycheck")
        self.assertIn("Gross", fields["Total earned"])
        self.assertIn("Progress", fields["Current paycheck"])
        self.assertIn("Second", fields["Current rate"])
        self.assertIn("Workday", fields["Current rate"])
        self.assertIn("09:00 - 17:00 ET", fields["Current rate"])
        self.assertIn("September 30, 2026", fields["Next paycheck"])
        self.assertIn("EARNING", fields["Next paycheck"])
        self.assertIn("Snapshot at invocation", embed.footer.text)

    def test_weekend_embed_reports_paused_status(self):
        embed = build_paycheck_embed(eastern(2026, 9, 19, 12))
        next_field = next(field.value for field in embed.fields if field.name == "Next paycheck")

        self.assertIn("WEEKEND / PAUSED", next_field)

    def test_after_hours_embed_reports_work_hours_status(self):
        embed = build_paycheck_embed(eastern(2026, 9, 21, 18))
        next_field = next(field.value for field in embed.fields if field.name == "Next paycheck")

        self.assertIn("OUTSIDE WORK HOURS", next_field)


if __name__ == "__main__":
    unittest.main()
