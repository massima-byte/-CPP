import os
import ssl
import unittest
from datetime import date
from unittest.mock import Mock, patch

from app.presto import (
    MarketingRefresh,
    RefreshError,
    _PrestoTLSAdapter,
    is_transient_network_error,
    refresh_marketing_data,
)


class PrestoTLSAdapterTests(unittest.TestCase):
    def test_adapter_excludes_weak_dh_but_keeps_ecdhe(self):
        cipher_names = {cipher["name"] for cipher in _PrestoTLSAdapter(False).ssl_context.get_ciphers()}

        self.assertTrue(any(name.startswith("ECDHE-RSA-") for name in cipher_names))
        self.assertFalse(any(name.startswith("DHE-RSA-") for name in cipher_names))

    def test_adapter_preserves_configured_certificate_verification(self):
        verified_context = _PrestoTLSAdapter(True).ssl_context
        unverified_context = _PrestoTLSAdapter(False).ssl_context

        self.assertTrue(verified_context.check_hostname)
        self.assertEqual(verified_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(unverified_context.check_hostname)
        self.assertEqual(unverified_context.verify_mode, ssl.CERT_NONE)

    @patch("app.presto.create_engine")
    def test_refresh_passes_the_tls_session_to_presto(self, create_engine):
        refresh = MarketingRefresh()
        refresh.settings = {
            "password": None,
            "username": "reporting",
            "host": "presto.example.test",
            "port": 8443,
            "catalog": "hive",
            "schema": "default",
            "protocol": "https",
            "tls_verify": False,
            "query_timeout": "90s",
        }
        expected_connection = Mock()
        create_engine.return_value.connect.return_value = expected_connection

        self.assertIs(refresh.connect(), expected_connection)

        connect_args = create_engine.call_args.kwargs["connect_args"]
        session = connect_args["requests_session"]
        try:
            self.assertIsInstance(session.get_adapter("https://presto/"), _PrestoTLSAdapter)
            self.assertFalse(connect_args["requests_kwargs"]["verify"])
        finally:
            session.close()


class PrestoQueryTests(unittest.TestCase):
    def test_registration_partition_query_prunes_to_recent_partitions(self):
        statement = MarketingRefresh.registration_partition_query(
            date(2026, 10, 12)
        )

        self.assertIn("WHERE tbl_dt IN (", statement)
        self.assertEqual(statement.count(",") + 1, 31)
        self.assertIn("20261012", statement)
        self.assertIn("20260912", statement)

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

    def test_query_includes_source_when_fetching_results_fails(self):
        connection = Mock()
        connection.execute.return_value.fetchall.side_effect = RuntimeError(
            "Query exceeded maximum time limit of 90.00s"
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
