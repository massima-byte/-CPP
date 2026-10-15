import os
import ssl
import unittest
from unittest.mock import Mock, patch

from ldap3.core.exceptions import LDAPException

from app.auth import (
    InvalidCredentialsError,
    LDAPUnavailableError,
    authenticate_ldap,
    create_session,
    delete_session,
    session_username,
)


class LDAPAuthenticationTests(unittest.TestCase):
    @patch.dict(os.environ, {
        "MTN_LDAP_HOST": "ldap.example.test",
        "MTN_LDAP_PORT": "636",
        "MTN_LDAP_USE_TLS": "true",
        "MTN_LDAP_DOMAIN": "mtn.local",
        "MTN_LDAP_BIND_FORMAT": "{username}@{domain}",
    })
    @patch("app.auth.Tls")
    @patch("app.auth.Server")
    @patch("app.auth.Connection")
    def test_uses_verified_ldaps_and_domain_bind(
        self, connection_factory, server_factory, tls_factory
    ):
        connection = Mock()
        connection.bind.return_value = True
        connection_factory.return_value = connection

        self.assertEqual(authenticate_ldap("MASSIMA", "secret"), "MASSIMA")
        tls_factory.assert_called_once_with(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=None,
        )
        server_factory.assert_called_once_with(
            "ldap.example.test",
            port=636,
            use_ssl=True,
            tls=tls_factory.return_value,
            connect_timeout=5,
        )
        self.assertEqual(connection_factory.call_args.kwargs["user"], "MASSIMA@mtn.local")
        self.assertEqual(connection_factory.call_args.kwargs["password"], "secret")
        connection.unbind.assert_called_once_with()

    @patch.dict(os.environ, {
        "MTN_LDAP_HOST": "10.77.71.15",
        "MTN_LDAP_PORT": "389",
        "MTN_LDAP_USE_TLS": "false",
        "MTN_LDAP_DOMAIN": "mtn.local",
        "MTN_LDAP_BIND_FORMAT": "{username}@{domain}",
    })
    @patch("app.auth.Tls")
    @patch("app.auth.Server")
    @patch("app.auth.Connection")
    def test_supports_the_dq_platform_plain_ldap_transport(
        self, connection_factory, server_factory, tls_factory
    ):
        connection = Mock()
        connection.bind.return_value = True
        connection_factory.return_value = connection

        self.assertEqual(authenticate_ldap("massima", "secret"), "massima")
        server_factory.assert_called_once_with(
            "10.77.71.15",
            port=389,
            use_ssl=False,
            tls=None,
            connect_timeout=5,
        )
        tls_factory.assert_not_called()

    @patch("app.auth.Connection")
    @patch("app.auth.Server")
    @patch("app.auth.Tls")
    def test_rejects_invalid_credentials(self, tls_factory, server_factory, connection_factory):
        connection = Mock()
        connection.bind.return_value = False
        connection.result = {"result": 49, "message": "invalid credentials"}
        connection_factory.return_value = connection

        with self.assertRaises(InvalidCredentialsError):
            authenticate_ldap("massima", "wrong")
        connection.unbind.assert_called_once_with()

    @patch("app.auth.Connection")
    @patch("app.auth.Server")
    @patch("app.auth.Tls")
    def test_classifies_server_errors_as_unavailable(self, tls_factory, server_factory, connection_factory):
        connection = Mock()
        connection.bind.return_value = False
        connection.result = {"result": 81, "message": "server down"}
        connection_factory.return_value = connection

        with self.assertRaises(LDAPUnavailableError):
            authenticate_ldap("massima", "secret")
        connection.unbind.assert_called_once_with()

    def test_maps_ldap_exception_to_unavailable(self):
        with patch("app.auth.Server", side_effect=LDAPException("offline")):
            with self.assertRaises(LDAPUnavailableError):
                authenticate_ldap("massima", "secret")


class SessionTests(unittest.TestCase):
    @patch.dict(os.environ, {"MTN_MARKETING_SESSION_TIMEOUT": "60"})
    def test_session_can_be_resolved_and_deleted(self):
        token, timeout = create_session("massima")

        self.assertEqual(timeout, 60)
        self.assertEqual(session_username(token), "massima")
        delete_session(token)
        self.assertIsNone(session_username(token))

    @patch.dict(os.environ, {"MTN_MARKETING_SESSION_TIMEOUT": "60"})
    @patch("app.auth.time.time", side_effect=[100, 161])
    def test_expired_session_is_rejected(self, _clock):
        token, _ = create_session("massima")

        self.assertIsNone(session_username(token))


if __name__ == "__main__":
    unittest.main()