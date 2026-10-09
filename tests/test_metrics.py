from datetime import date, datetime, timezone
import json
import tempfile
import unittest
from pathlib import Path

from app.metrics import (
    assemble_snapshot,
    build_day_over_day,
    build_hourly_trend,
    date_key,
    finite_number,
    freshness_state,
    iso_date,
    load_snapshot,
    normalize_department,
    save_snapshot,
)


class MetricsTests(unittest.TestCase):
    def fixture(
        self,
        refresh_started_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
        *,
        executive_transactions=30,
        executive_customers=20,
        momo_users=500,
        momo_transactions=1_000,
    ):
        return assemble_snapshot(
            executive={
                "date": date(2026, 10, 4),
                "revenue": 12_000,
                "transactions": executive_transactions,
                "customers": executive_customers,
            },
            trend=[
                {"date": date(2026, 10, 4), "revenue": 12_000, "customers": 20},
                {"date": date(2026, 10, 3), "revenue": 10_000, "customers": 18},
            ],
            sims={"date": "20261004", "total": 19_000_000, "activated": 17_000_000},
            recruitment={
                "date": "20261004",
                "daily_counts": [
                    {"date": date(2026, 10, 4), "count": 100},
                    {"date": date(2026, 10, 1), "count": 250},
                ],
                "registrations": 20_000,
            },
            momo={
                "date": date(2026, 10, 5),
                "users": momo_users,
                "transactions": momo_transactions,
                "volume": 1_500_000,
                "fees": 20_000,
                "operations": [
                    {
                        "operation": "P2P",
                        "users": 300,
                        "transactions": 700,
                        "volume": 1_000_000,
                        "fees": 15_000,
                    },
                    {
                        "operation": "CASH_OUT",
                        "users": 250,
                        "transactions": 300,
                        "volume": 500_000,
                        "fees": 5_000,
                    },
                ],
                "trend": [
                    {
                        "date": "20261004",
                        "users": 480,
                        "transactions": 900,
                        "amount": 1_250_000,
                        "fees": 17_000,
                    },
                    {
                        "date": "20261005",
                        "users": 500,
                        "transactions": 1_000,
                        "amount": 1_500_000,
                        "fees": 20_000,
                    },
                ],
            },
            services=[
                {
                    "date": "20261004",
                    "code": "3GBmomo000MM",
                    "activations": 200,
                    "clients": 190,
                    "amount": 400_000,
                }
            ],
            services_trend=[
                {"date": "20261002", "activations": 11, "amount": 22_000},
                {"date": "20261004", "activations": 16, "amount": 48_000},
            ],
            hourly_revenue=[
                {
                    "date": "20261004",
                    "hour": 22,
                    "revenue": 900,
                    "transactions": 6,
                },
                {
                    "date": "20261004",
                    "hour": 23,
                    "revenue": 1_200,
                    "transactions": 8,
                },
            ],
            geographic_revenue={
                "date": "20261004",
                "total_customers": 8,
                "rows": [
                    {"date": "20261004", "department": "LITTORAL", "revenue": 5_000, "customers": 5},
                    {"date": "20261004", "department": "OUEME", "revenue": 3_000, "customers": 3},
                ],
            },
            complaints={
                "period_start": "2026-09-06",
                "period_end": "2026-10-05",
                "tickets": 282_542,
                "ticket_clients": 169_254,
                "ticket_partition": "20261005",
                "ticket_statuses": [
                    {"status": "closed", "tickets": 244_366},
                    {"status": "open", "tickets": 33_620},
                    {"status": "inProgress", "tickets": 4_531},
                    {"status": "resolved", "tickets": 17},
                    {"status": "cancelled", "tickets": 8},
                ],
                "ticket_trend": [
                    {"date": "2026-09-06", "tickets": 282_000},
                    {"date": "2026-10-05", "tickets": 542},
                ],
                "conversations": 61_223,
                "conversation_clients": 42_291,
                "conversation_partition": "20261005",
                "conversation_trend": [
                    {"date": "2026-09-06", "conversations": 61_000},
                    {"date": "2026-10-05", "conversations": 223},
                ],
            },
            refresh_started_at=refresh_started_at,
        )

    def test_snapshot_contains_current_aggregate_domains_and_no_customer_records(self):
        result = self.fixture()
        self.assertEqual(result["executive"]["totalSims"], 19_000_000)
        self.assertEqual(result["executive"]["uniqueCustomers"], 20)
        self.assertEqual(result["executive"]["dayOverDay"]["previousDate"], "2026-10-03")
        self.assertEqual(result["executive"]["dayOverDay"]["revenueDelta"], 2_000)
        self.assertAlmostEqual(result["executive"]["dayOverDay"]["revenuePercentChange"], 0.2)
        self.assertEqual(result["recruitment"]["daily"], 100)
        self.assertEqual(result["recruitment"]["weekly"], 350)
        self.assertEqual(len(result["services"]["trend"]), 7)
        self.assertIsNone(result["services"]["trend"][0]["amount"])
        self.assertEqual(result["services"]["trend"][-1]["activations"], 16)
        self.assertFalse(result["services"]["partial"])
        services_source = next(
            source for source in result["sources"] if source["module"] == "Services"
        )
        self.assertTrue(services_source["state"].startswith("À jour"))
        self.assertEqual(result["momo"]["transactions"], 1_000)
        self.assertEqual(result["momo"]["operations"][0]["type"], "P2P")
        self.assertEqual(result["momo"]["operations"][1]["transactions"], 300)
        self.assertEqual(len(result["momo"]["trend"]), 7)
        self.assertIsNone(result["momo"]["trend"][4]["amount"])
        self.assertEqual(result["momo"]["trend"][-1]["date"], "2026-10-05")
        self.assertEqual(result["momo"]["trend"][-1]["amount"], 1_500_000)
        self.assertIsNone(result["momo"]["merchants"])
        self.assertEqual(result["realtime_hourly"]["previous_hour"]["hour"], 23)
        self.assertEqual(result["realtime_hourly"]["previous_hour"]["revenue"], 1_200)
        self.assertEqual(len(result["realtime_hourly"]["hours"]), 24)
        self.assertEqual(result["geographic"]["revenue"]["Littoral"], 5_000)
        self.assertEqual(result["geographic"]["customers"]["Littoral"], 5)
        self.assertEqual(result["geographic"]["totalCustomers"], 8)
        self.assertTrue(result["geographic"]["customersFresh"])
        self.assertTrue(result["complaints"]["available"])
        self.assertEqual(result["complaints"]["tickets"], 282_542)
        self.assertEqual(sum(row["tickets"] for row in result["complaints"]["ticketStatuses"]), 282_542)
        self.assertEqual(result["complaints"]["ticketStatuses"][0]["status"], "closed")
        self.assertEqual(len(result["complaints"]["ticketTrend"]), 30)
        self.assertIsNone(result["complaints"]["ticketTrend"][1]["count"])
        self.assertEqual(
            sum(row["count"] for row in result["complaints"]["ticketTrend"] if row["count"] is not None),
            282_542,
        )
        self.assertEqual(
            sum(row["count"] for row in result["complaints"]["conversationTrend"] if row["count"] is not None),
            61_223,
        )
        self.assertEqual(result["complaints"]["periodStart"], "2026-09-06")
        rgs90 = next(source for source in result["sources"] if source["module"] == "RGS90")
        self.assertIsNone(rgs90["date"])
        self.assertTrue(rgs90["state"].startswith("Non disponible"))
        self.assertNotIn("customer360", result)
        self.assertNotIn("churn", result)

    def test_current_service_partition_is_marked_partial(self):
        result = self.fixture(
            refresh_started_at=datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
        )
        self.assertTrue(result["services"]["partial"])
        source = next(
            source for source in result["sources"] if source["module"] == "Services"
        )
        self.assertTrue(source["state"].startswith("Journée partielle"))
        serialized = json.dumps(result)
        self.assertNotIn("msisdn", serialized.lower())
        self.assertNotIn("churn", serialized.lower())
        self.assertNotIn("risk_list", serialized.lower())

    def test_day_over_day_revenue_change_is_unavailable_when_previous_day_is_missing(self):
        self.assertIsNone(
            build_day_over_day(
                "2026-10-05",
                12_000,
                [{"date": "2026-10-03", "revenue": 10_000}],
            )
        )

    def test_day_over_day_percent_is_unavailable_for_zero_baseline(self):
        result = build_day_over_day(
            "2026-10-05",
            12_000,
            [{"date": "2026-10-04", "revenue": 0}],
        )
        self.assertEqual(result["revenueDelta"], 12_000)
        self.assertIsNone(result["revenuePercentChange"])

    def test_missing_source_amount_is_not_converted_to_zero(self):
        with self.assertRaisesRegex(ValueError, "missing numeric value"):
            finite_number(None, "revenue")

    def test_average_metrics_are_unavailable_when_the_denominator_is_zero(self):
        result = self.fixture(
            executive_transactions=0,
            executive_customers=0,
            momo_users=0,
            momo_transactions=0,
        )
        self.assertIsNone(result["executive"]["avgTransaction"])
        self.assertIsNone(result["executive"]["arpu"])
        self.assertIsNone(result["momo"]["avgTransaction"])
        self.assertIsNone(result["momo"]["txPerUser"])
        self.assertIsNone(result["momo"]["revenuePerUser"])

    def test_dates_and_departments_are_normalized(self):
        self.assertEqual(iso_date(date(2026, 10, 4)), "2026-10-04")
        self.assertEqual(date_key("2026-10-04"), "20261004")
        self.assertEqual(normalize_department("OUEME"), "Ouémé")
        self.assertEqual(normalize_department("NON RENSEIGNE"), "Non renseigné")
        with self.assertRaises(ValueError):
            iso_date("invalid")

    def test_partition_lag_is_reported_instead_of_marked_current(self):
        self.assertEqual(
            freshness_state("20260930", date(2026, 10, 5), "À jour · dernière partition"),
            "Retard de 5 j · dernière partition disponible",
        )
        self.assertEqual(
            freshness_state("20261004", date(2026, 10, 5), "À jour · dernière partition"),
            "À jour · dernière partition",
        )

    def test_hourly_trend_uses_the_last_completed_hour_and_preserves_gaps(self):
        result = build_hourly_trend(
            [
                {"date": "20261004", "hour": 22, "revenue": 500, "transactions": 2},
                {"date": "20261004", "hour": 22, "revenue": 125, "transactions": 1},
            ],
            datetime(2026, 10, 5, 0, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result["hours"]), 24)
        self.assertEqual(result["hours"][-2]["hour"], 22)
        self.assertEqual(result["hours"][-2]["revenue"], 625)
        self.assertEqual(result["hours"][-2]["transactions"], 3)
        self.assertIsNone(result["hours"][-1]["revenue"])
        self.assertIsNone(result["previous_hour"])
        self.assertIsNone(result["hour_over_hour"])

    def test_hourly_comparison_uses_two_observed_complete_hours_only(self):
        result = build_hourly_trend(
            [
                {"date": "20261004", "hour": 22, "revenue": 625, "transactions": 3},
                {"date": "20261004", "hour": 23, "revenue": 1_200, "transactions": 8},
            ],
            datetime(2026, 10, 5, 0, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(result["previous_hour"]["hour"], 23)
        self.assertEqual(result["hour_over_hour"]["previousTimestamp"], "2026-10-04T22:00+00:00")
        self.assertEqual(result["hour_over_hour"]["revenueDelta"], 575)
        self.assertAlmostEqual(result["hour_over_hour"]["revenuePercentChange"], 0.92)
        self.assertEqual(result["hour_over_hour"]["transactionsDelta"], 5)
        self.assertAlmostEqual(result["hour_over_hour"]["transactionsPercentChange"], 5 / 3)

    def test_hourly_percentage_change_is_unavailable_for_zero_baseline(self):
        result = build_hourly_trend(
            [
                {"date": "20261004", "hour": 22, "revenue": 0, "transactions": 0},
                {"date": "20261004", "hour": 23, "revenue": 1_200, "transactions": 8},
            ],
            datetime(2026, 10, 5, 0, 15, tzinfo=timezone.utc),
        )
        self.assertEqual(result["hour_over_hour"]["revenueDelta"], 1_200)
        self.assertIsNone(result["hour_over_hour"]["revenuePercentChange"])
        self.assertEqual(result["hour_over_hour"]["transactionsDelta"], 8)
        self.assertIsNone(result["hour_over_hour"]["transactionsPercentChange"])

    def test_snapshot_write_is_atomic_and_loadable(self):
        snapshot = self.fixture()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "metrics.json"
            save_snapshot(snapshot, destination)
            loaded = load_snapshot(destination)
            self.assertEqual(loaded["schema_version"], 1)
            self.assertEqual(loaded["executive"]["totalSims"], 19_000_000)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_old_snapshot_computes_hourly_comparison_from_observed_buckets(self):
        snapshot = self.fixture()
        snapshot["realtime_hourly"].pop("hour_over_hour")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "metrics.json"
            save_snapshot(snapshot, destination)
            loaded = load_snapshot(destination)
        self.assertEqual(loaded["realtime_hourly"]["hour_over_hour"]["revenueDelta"], 300)


if __name__ == "__main__":
    unittest.main()
