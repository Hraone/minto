"""Deterministic tests for dashboard financial calculations."""

import unittest
from datetime import date, datetime

from dashboard_finance import (
    calculate_dashboard_flow,
    compute_safe_to_spend,
    filter_commitments_to_current_month,
    get_dashboard_period_start,
)


class DashboardFinanceTests(unittest.TestCase):
    def test_period_starts_for_today_week_month_year_and_all(self):
        now = datetime(2026, 10, 13, 12, 34, 56)

        self.assertEqual(datetime(2026, 10, 13), get_dashboard_period_start("today", now))
        self.assertEqual(datetime(2026, 10, 6, 12, 34, 56), get_dashboard_period_start("week", now))
        self.assertEqual(datetime(2026, 10, 1), get_dashboard_period_start("month", now))
        self.assertEqual(datetime(2026, 1, 1), get_dashboard_period_start("year", now))
        self.assertIsNone(get_dashboard_period_start("all", now))

    def test_empty_period_has_zero_totals_and_no_breakdowns(self):
        self.assertEqual(
            {
                "total_in": 0.0,
                "total_out": 0.0,
                "net": 0.0,
                "expense_by_category": {},
                "outgoing_by_category": {},
                "spend_by_source": {},
            },
            calculate_dashboard_flow([]),
        )

    def test_income_only_period(self):
        summary = calculate_dashboard_flow([
            {"amount": "1250.00", "direction": "in", "category": "income"},
        ])

        self.assertEqual(1250.0, summary["total_in"])
        self.assertEqual(0.0, summary["total_out"])
        self.assertEqual(1250.0, summary["net"])

    def test_outgoing_only_period_keeps_negative_net_and_category_totals(self):
        summary = calculate_dashboard_flow([
            {
                "amount": 80,
                "direction": "out",
                "category": "expense",
                "expense_category": "groceries",
                "user_sources": {"name": "Cash"},
            },
            {
                "amount": 20,
                "direction": "out",
                "category": "investment",
                "investment_category": "mutual_fund",
                "user_sources": {"name": "Cash"},
            },
        ])

        self.assertEqual(0.0, summary["total_in"])
        self.assertEqual(100.0, summary["total_out"])
        self.assertEqual(-100.0, summary["net"])
        self.assertEqual({"groceries": 80.0}, summary["expense_by_category"])
        self.assertEqual(
            {"Groceries": 80.0, "Investment · Mutual Fund": 20.0},
            summary["outgoing_by_category"],
        )
        self.assertEqual({"Cash": 100.0}, summary["spend_by_source"])

    def test_transfers_lending_and_trip_movements_are_excluded(self):
        summary = calculate_dashboard_flow([
            {"amount": 500, "direction": "out", "category": "transfer", "user_sources": {"name": "Bank"}},
            {"amount": 50, "direction": "in", "category": "lending"},
            {"amount": 120, "direction": "out", "category": "trip_expense_payment", "user_sources": {"name": "Bank"}},
            {"amount": 40, "direction": "in", "category": "trip_settlement"},
            {
                "amount": 120,
                "direction": "out",
                "category": "expense",
                "expense_category": "dinner",
                "user_sources": {"name": "Bank"},
            },
        ])

        self.assertEqual(0.0, summary["total_in"])
        self.assertEqual(120.0, summary["total_out"])
        self.assertEqual(-120.0, summary["net"])
        self.assertEqual({"Dinner": 120.0}, summary["outgoing_by_category"])
        self.assertEqual({"Bank": 120.0}, summary["spend_by_source"])

    def test_safe_to_spend_handles_empty_shortfall_and_decimal_balances(self):
        self.assertEqual(0.0, compute_safe_to_spend([], []))
        self.assertEqual(0.0, compute_safe_to_spend([{"balance": 0}], []))
        self.assertEqual(0.0, compute_safe_to_spend([{"balance": -20}], []))
        self.assertEqual(
            70.15,
            compute_safe_to_spend(
                [{"balance": 100.25}, {"balance": 20}],
                [{"amount": 50.10}],
            ),
        )
        self.assertEqual(
            0.0,
            compute_safe_to_spend([{"balance": 40}], [{"amount": 45}]),
        )

    def test_current_month_filter_includes_month_end_and_excludes_next_month(self):
        today = date(2024, 2, 29)
        commitments = [
            {"name": "Today", "due_date": date(2024, 2, 29)},
            {"name": "Earlier this month", "due_date": date(2024, 2, 1)},
            {"name": "Next month", "due_date": date(2024, 3, 1)},
            {"name": "Same month next year", "due_date": date(2025, 2, 28)},
            {"name": "Missing date"},
        ]

        self.assertEqual(
            ["Today", "Earlier this month"],
            [item["name"] for item in filter_commitments_to_current_month(commitments, today)],
        )


if __name__ == "__main__":
    unittest.main()

