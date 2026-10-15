from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from app.access_control import AccessControlStore
from app.server import MarketingHandler, request_address_allowed


class RemoteAccessPolicyTests(unittest.TestCase):
    def test_loopback_is_allowed_without_remote_opt_in(self):
        self.assertTrue(request_address_allowed(
            "127.0.0.1", "127.0.0.1:8533", allow_remote=False, allowed_hosts=set()
        ))

    def test_remote_access_is_denied_by_default(self):
        self.assertFalse(request_address_allowed(
            "10.77.4.20", "10.77.60.96:8533", allow_remote=False,
            allowed_hosts={"10.77.60.96"},
        ))

    def test_remote_access_requires_opt_in_and_allowlisted_host(self):
        self.assertTrue(request_address_allowed(
            "10.77.4.20", "10.77.60.96:8533", allow_remote=True,
            allowed_hosts={"10.77.60.96"},
        ))
        self.assertFalse(request_address_allowed(
            "10.77.4.20", "attacker.example:8533", allow_remote=True,
            allowed_hosts={"10.77.60.96"},
        ))

    def test_request_logs_do_not_include_query_strings(self):
        handler = SimpleNamespace(
            client_address=("10.77.4.20", 12345),
            requestline="GET /?username=massima&password=secret HTTP/1.1",
        )
        with patch("builtins.print") as output:
            MarketingHandler.log_message(handler, '"%s" %s', handler.requestline, "200")

        logged = output.call_args.args[0]
        self.assertNotIn("password", logged)
        self.assertNotIn("secret", logged)
        self.assertEqual(logged, '10.77.4.20 - "GET /" 200')


class MarketingAuthHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.access_store = AccessControlStore(Path(self.temp_dir.name) / "access.sqlite3")
        self.access_store_patch = patch("app.server.access_store", self.access_store)
        self.access_store_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MarketingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.access_store_patch.stop()
        self.temp_dir.cleanup()

    def request(self, path, method="GET", payload=None, cookie=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if cookie:
            headers["Cookie"] = cookie
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            return urlopen(request, timeout=3)
        except HTTPError as error:
            return error

    def test_metrics_and_refresh_reject_requests_without_session(self):
        with patch("app.server.load_snapshot") as load_snapshot:
            metrics = self.request("/api/metrics")
            refresh = self.request("/api/refresh", "POST", {})

        self.assertEqual(metrics.code, 401)
        self.assertEqual(refresh.code, 401)
        load_snapshot.assert_not_called()

    def test_login_cookie_authorizes_metrics_and_logout_revokes_it(self):
        snapshot = {
            "schema_version": 1,
            "generated_at": "2026-10-09T12:00:00+00:00",
            "executive": {},
            "sources": [],
        }
        with (
            patch("app.server.authenticate_ldap", return_value="massima") as authenticate,
            patch("app.server.load_snapshot", return_value=snapshot),
        ):
            login = self.request(
                "/api/login",
                "POST",
                {"username": "massima", "password": "test-password"},
            )
            cookie = login.headers["Set-Cookie"].split(";", 1)[0]
            metrics = self.request("/api/metrics", cookie=cookie)
            logout = self.request("/api/logout", "POST", {}, cookie=cookie)
            rejected = self.request("/api/metrics", cookie=cookie)

        self.assertEqual(login.code, 200)
        self.assertIn("HttpOnly", login.headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", login.headers["Set-Cookie"])
        self.assertEqual(json.loads(metrics.read())["generated_at"], snapshot["generated_at"])
        self.assertEqual(logout.code, 200)
        self.assertEqual(rejected.code, 401)
        authenticate.assert_called_once_with("massima", "test-password")

    def test_first_ldap_login_waits_for_approval_without_creating_a_session(self):
        with patch("app.server.authenticate_ldap", return_value="new.user"):
            login = self.request(
                "/api/login",
                "POST",
                {"username": "new.user", "password": "test-password"},
            )
            session = self.request("/api/session")
            metrics = self.request("/api/metrics")

        self.assertEqual(login.code, 202)
        self.assertIsNone(login.headers.get("Set-Cookie"))
        self.assertEqual(json.loads(session.read())["authenticated"], False)
        self.assertEqual(metrics.code, 401)
        self.assertEqual(len(self.access_store.list_requests()), 1)

    def test_admin_approves_user_and_viewer_cannot_access_admin_routes(self):
        pending = self.access_store.record_ldap_login("reader")
        with patch("app.server.authenticate_ldap", return_value="massima"):
            admin_login = self.request(
                "/api/login", "POST", {"username": "massima", "password": "test-password"}
            )
        admin_cookie = admin_login.headers["Set-Cookie"].split(";", 1)[0]
        approval = self.request(
            f"/api/admin/requests/{pending['request_id']}/approve",
            "POST",
            {"profile_key": "viewer"},
            cookie=admin_cookie,
        )
        self.assertEqual(approval.code, 200)

        with patch("app.server.authenticate_ldap", return_value="reader"):
            viewer_login = self.request(
                "/api/login", "POST", {"username": "reader", "password": "test-password"}
            )
        viewer_cookie = viewer_login.headers["Set-Cookie"].split(";", 1)[0]
        denied = self.request("/api/admin/users", cookie=viewer_cookie)

        self.assertEqual(viewer_login.code, 200)
        self.assertEqual(denied.code, 403)

    def test_viewer_cannot_refresh_data_or_read_audit(self):
        pending = self.access_store.record_ldap_login("reader")
        self.access_store.approve_request(pending["request_id"], "massima", "viewer")
        with patch("app.server.authenticate_ldap", return_value="reader"):
            login = self.request(
                "/api/login", "POST", {"username": "reader", "password": "test-password"}
            )
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]

        with patch("app.server._refresh_snapshot") as refresh:
            refresh_response = self.request("/api/refresh", "POST", {}, cookie)
            audit = self.request("/api/admin/audit", cookie=cookie)

        self.assertEqual(refresh_response.code, 403)
        self.assertEqual(audit.code, 403)
        refresh.assert_not_called()

    def test_request_reviewer_can_approve_without_managing_accounts(self):
        self.access_store.update_profile_privileges(
            "viewer", "massima", {"review_account_requests": True}
        )
        reviewer_request = self.access_store.record_ldap_login("reviewer")
        self.access_store.approve_request(reviewer_request["request_id"], "massima", "viewer")
        pending = self.access_store.record_ldap_login("candidate")
        with patch("app.server.authenticate_ldap", return_value="reviewer"):
            login = self.request(
                "/api/login", "POST", {"username": "reviewer", "password": "test-password"}
            )
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]

        requests = self.request("/api/admin/requests", cookie=cookie)
        users = self.request("/api/admin/users", cookie=cookie)
        approval = self.request(
            f"/api/admin/requests/{pending['request_id']}/approve",
            "POST",
            {"profile_key": "viewer"},
            cookie,
        )

        self.assertEqual(requests.code, 200)
        self.assertEqual(users.code, 403)
        self.assertEqual(approval.code, 200)

    def test_profile_manager_can_edit_roles_without_account_management(self):
        self.access_store.update_profile_privileges("viewer", "massima", {"manage_profiles": True})
        pending = self.access_store.record_ldap_login("role.manager")
        self.access_store.approve_request(pending["request_id"], "massima", "viewer")
        with patch("app.server.authenticate_ldap", return_value="role.manager"):
            login = self.request(
                "/api/login", "POST", {"username": "role.manager", "password": "test-password"}
            )
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]

        profiles = self.request("/api/admin/profiles", cookie=cookie)
        users = self.request("/api/admin/users", cookie=cookie)
        updated = self.request(
            "/api/admin/profiles/viewer/privileges",
            "POST",
            {"privileges": {"export_data": True}},
            cookie,
        )

        self.assertEqual(profiles.code, 200)
        self.assertEqual(users.code, 403)
        self.assertEqual(updated.code, 200)

    def test_admin_api_cannot_demote_or_suspend_massima(self):
        with patch("app.server.authenticate_ldap", return_value="massima"):
            login = self.request(
                "/api/login", "POST", {"username": "massima", "password": "test-password"}
            )
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        user = next(user for user in self.access_store.list_users() if user["username"] == "massima")

        response = self.request(
            f"/api/admin/users/{user['id']}",
            "POST",
            {"profile_key": "viewer", "status": "disabled"},
            cookie,
        )

        self.assertEqual(response.code, 400)
        access = self.access_store.get_access("massima")
        self.assertEqual(access["profile_key"], "admin")
        self.assertEqual(access["status"], "active")

    def test_suspension_revokes_an_existing_session_on_its_next_request(self):
        pending = self.access_store.record_ldap_login("reader")
        self.access_store.approve_request(pending["request_id"], "massima", "viewer")
        with patch("app.server.authenticate_ldap", return_value="reader"):
            login = self.request(
                "/api/login", "POST", {"username": "reader", "password": "test-password"}
            )
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        user = next(user for user in self.access_store.list_users() if user["username"] == "reader")
        self.access_store.update_user(user["id"], "massima", "viewer", "disabled")

        with patch("app.server._refresh_snapshot") as refresh:
            denied = self.request("/api/refresh", "POST", {}, cookie)
            session = self.request("/api/session", cookie=cookie)

        self.assertEqual(denied.code, 401)
        self.assertFalse(json.loads(session.read())["authenticated"])
        refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()