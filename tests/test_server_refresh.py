from datetime import datetime, timedelta, timezone
import os
import unittest
from unittest.mock import patch

from app.config import marketing_refresh_interval_seconds
from app.presto import RefreshError
from app.server import (
    _refresh_scheduler,
    _seconds_until_refresh,
    refresh_failure_delay,
)


class StopOnWait:
    def __init__(self):
        self.wait_seconds = None

    def is_set(self):
        return False

    def wait(self, timeout):
        self.wait_seconds = timeout
        return True


class RefreshSchedulingTests(unittest.TestCase):
    def test_refresh_is_due_without_a_snapshot(self):
        self.assertEqual(
            _seconds_until_refresh(None, datetime.now(timezone.utc), 900),
            0,
        )

    def test_refresh_is_due_for_an_old_snapshot(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        snapshot = {"generated_at": (now - timedelta(minutes=20)).isoformat()}
        self.assertEqual(_seconds_until_refresh(snapshot, now, 900), 0)

    def test_refresh_wait_uses_snapshot_age(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        snapshot = {"generated_at": (now - timedelta(minutes=5)).isoformat()}
        self.assertEqual(_seconds_until_refresh(snapshot, now, 900), 600)

    def test_refresh_wait_accepts_naive_snapshot_timestamp_as_utc(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        snapshot = {"generated_at": "2026-10-07T11:55:00"}
        self.assertEqual(_seconds_until_refresh(snapshot, now, 900), 600)

    def test_refresh_wait_treats_invalid_snapshot_timestamp_as_due(self):
        self.assertEqual(
            _seconds_until_refresh({"generated_at": "invalid"}, datetime.now(timezone.utc), 900),
            0,
        )

    def test_refresh_interval_defaults_to_fifteen_minutes_and_has_a_minimum(self):
        with patch.dict(os.environ, {"MTN_MARKETING_REFRESH_INTERVAL_SECONDS": "900"}):
            self.assertEqual(marketing_refresh_interval_seconds(), 900)
        with patch.dict(os.environ, {"MTN_MARKETING_REFRESH_INTERVAL_SECONDS": "30"}):
            with self.assertRaisesRegex(ValueError, "au moins 60"):
                marketing_refresh_interval_seconds()

    def test_scheduler_refreshes_immediately_when_no_snapshot_exists(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        snapshot = {"generated_at": now.isoformat()}
        stop_event = StopOnWait()
        with (
            patch("app.server.load_snapshot", side_effect=[None, snapshot]),
            patch("app.server._utc_now", return_value=now),
            patch("app.server._refresh_snapshot", return_value=snapshot) as refresh,
        ):
            _refresh_scheduler(stop_event, 900)
        refresh.assert_called_once_with()
        self.assertEqual(stop_event.wait_seconds, 900)

    def test_scheduler_waits_until_the_snapshot_expires(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        snapshot = {"generated_at": (now - timedelta(minutes=5)).isoformat()}
        stop_event = StopOnWait()
        with (
            patch("app.server.load_snapshot", return_value=snapshot),
            patch("app.server._utc_now", return_value=now),
            patch("app.server._refresh_snapshot") as refresh,
        ):
            _refresh_scheduler(stop_event, 900)
        refresh.assert_not_called()
        self.assertEqual(stop_event.wait_seconds, 600)


class FailureDelayTests(unittest.TestCase):
    NETWORK_ERROR = RefreshError(
        "Échec d'une requête Presto sur itbi_ess.prod_kpis_revenu "
        "(ConnectTimeout: HTTPSConnectionPool(host='10.77.89.122', port=8443))."
    )
    BUSINESS_ERROR = RefreshError("Aucune partition de revenu récente disponible dans Presto.")

    def test_network_failure_retries_quickly_instead_of_waiting_the_interval(self):
        with patch.dict(os.environ, {"MTN_MARKETING_RETRY_SECONDS": "60"}):
            self.assertEqual(refresh_failure_delay(self.NETWORK_ERROR, 900), 60)

    def test_retry_delay_never_exceeds_the_normal_interval(self):
        with patch.dict(os.environ, {"MTN_MARKETING_RETRY_SECONDS": "600"}):
            self.assertEqual(refresh_failure_delay(self.NETWORK_ERROR, 300), 300)

    def test_business_failure_keeps_the_normal_cadence(self):
        with patch.dict(os.environ, {"MTN_MARKETING_RETRY_SECONDS": "60"}):
            self.assertEqual(refresh_failure_delay(self.BUSINESS_ERROR, 900), 900)

    def test_scheduler_uses_the_short_delay_after_a_network_failure(self):
        now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
        stale = {"generated_at": (now - timedelta(minutes=20)).isoformat()}
        stop_event = StopOnWait()
        with (
            patch("app.server.load_snapshot", return_value=stale),
            patch("app.server._utc_now", return_value=now),
            patch("app.server._refresh_snapshot", side_effect=self.NETWORK_ERROR),
            patch.dict(os.environ, {"MTN_MARKETING_RETRY_SECONDS": "60"}),
        ):
            _refresh_scheduler(stop_event, 900)
        self.assertEqual(stop_event.wait_seconds, 60)


if __name__ == "__main__":
    unittest.main()
