import os
import unittest
from unittest.mock import Mock, patch

from app.presto import (
    MarketingRefresh,
    RefreshError,
    is_transient_network_error,
    refresh_marketing_data,
)


class PrestoQueryTests(unittest.TestCase):
    def test_query_includes_source_when_presto_fails(self):
        connection = Mock()
        connection.execute.side_effect = RuntimeError(
            "Query exceeded maximum time limit of 90.00s\nAdditional details"
        )

        with self.assertRaisesRegex(
            RefreshError,
            r"Échec d'une requête Presto sur itbi_ess\.datamart_refill_loc_vw "
            r"\(RuntimeError: Query exceeded maximum time limit of 90\.00s\)",
        ):
            MarketingRefresh.query(
                connection,
                "SELECT COUNT(DISTINCT msisdn_key) "
                "FROM itbi_ess.datamart_refill_loc_vw",
            )

    def test_query_reports_unknown_source_when_statement_has_no_from(self):
        connection = Mock()
        connection.execute.side_effect = RuntimeError("connection refused")

        with self.assertRaisesRegex(
            RefreshError,
            r"Échec d'une requête Presto sur source inconnue "
            r"\(RuntimeError: connection refused\)",
        ):
            MarketingRefresh.query(connection, "SELECT 1")

    def test_query_returns_rows_unchanged_on_success(self):
        connection = Mock()
        row = Mock()
        row._mapping = {"total": 42}
        connection.execute.return_value.fetchall.return_value = [row]

        self.assertEqual(
            MarketingRefresh.query(connection, "SELECT COUNT(*) AS total FROM sample"),
            [{"total": 42}],
        )


class TransientDetectionTests(unittest.TestCase):
    def test_connect_timeout_is_transient(self):
        error = RefreshError(
            "Échec d'une requête Presto sur itbi_ess.prod_kpis_revenu "
            "(ConnectTimeout: HTTPSConnectionPool(host='10.77.89.122', "
            "port=8443): Max retries exceeded with url: /v1/statement)."
        )
        self.assertTrue(is_transient_network_error(error))

    def test_missing_partition_is_not_transient(self):
        error = RefreshError("Aucune partition de revenu récente disponible dans Presto.")
        self.assertFalse(is_transient_network_error(error))

    def test_cause_chain_is_followed(self):
        error = RefreshError("Rafraîchissement Presto impossible.")
        error.__cause__ = TimeoutError("connection timed out")
        self.assertTrue(is_transient_network_error(error))

    def test_absence_of_error_is_not_transient(self):
        self.assertFalse(is_transient_network_error(None))


class RefreshRetryTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(
            os.environ,
            {"MTN_PRESTO_RETRIES": "2", "MTN_PRESTO_RETRY_DELAY": "0"},
        )
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def _stub(failures, error):
        calls = []

        class StubRefresh:
            def refresh(self):
                calls.append(1)
                if len(calls) <= failures:
                    raise error
                return {"generated_at": "2026-10-07T21:00:00+00:00"}

        return StubRefresh, calls

    def test_network_failure_is_retried_then_succeeds(self):
        transient = RefreshError(
            "Échec d'une requête Presto sur itbi_ess.prod_kpis_revenu "
            "(ConnectTimeout: HTTPSConnectionPool(host='10.77.89.122', port=8443))."
        )
        stub, calls = self._stub(failures=2, error=transient)
        with patch("app.presto.MarketingRefresh", stub):
            snapshot = refresh_marketing_data()
        self.assertEqual(snapshot["generated_at"], "2026-10-07T21:00:00+00:00")
        self.assertEqual(len(calls), 3, "deux relances puis succès attendus")

    def test_network_failure_gives_up_after_the_configured_attempts(self):
        transient = RefreshError(
            "Échec d'une requête Presto sur itbi_ess.prod_kpis_revenu "
            "(ConnectTimeout: HTTPSConnectionPool(host='10.77.89.122', port=8443))."
        )
        stub, calls = self._stub(failures=99, error=transient)
        with patch("app.presto.MarketingRefresh", stub):
            with self.assertRaises(RefreshError):
                refresh_marketing_data()
        self.assertEqual(len(calls), 3, "1 essai + 2 relances")

    def test_business_failure_is_never_retried(self):
        business = RefreshError("Aucune partition de revenu récente disponible dans Presto.")
        stub, calls = self._stub(failures=99, error=business)
        with patch("app.presto.MarketingRefresh", stub):
            with self.assertRaisesRegex(RefreshError, "Aucune partition"):
                refresh_marketing_data()
        self.assertEqual(len(calls), 1, "un échec métier ne doit pas être rejoué")


if __name__ == "__main__":
    unittest.main()
