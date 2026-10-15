from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.access_control import AccessControlStore


class AccessControlStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = AccessControlStore(Path(self.temp_dir.name) / "access.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_massima_is_bootstrapped_as_permanent_admin(self):
        access = self.store.get_access("MASSIMA@mtn.local")

        self.assertEqual(access["profile_key"], "admin")
        self.assertTrue(all(access["privileges"].values()))
        user = next(user for user in self.store.list_users() if user["username"] == "massima")
        with self.assertRaisesRegex(ValueError, "massima"):
            self.store.update_user(user["id"], "massima", "viewer", "disabled")

    def test_first_ldap_login_requests_access_until_approved(self):
        first = self.store.record_ldap_login("analyst", "analyst@mtn.local")
        repeated = self.store.record_ldap_login("ANALYST")

        self.assertEqual(first["state"], "pending")
        self.assertEqual(repeated["request_id"], first["request_id"])
        self.assertIsNone(self.store.get_access("analyst"))
        request = self.store.list_requests()[0]
        self.store.approve_request(request["id"], "massima", "viewer")

        access = self.store.get_access("analyst")
        self.assertEqual(access["profile_key"], "viewer")
        self.assertTrue(access["privileges"]["view_dashboard"])
        self.assertFalse(access["privileges"]["refresh_data"])

    def test_rejected_user_can_submit_a_new_request(self):
        submitted = self.store.record_ldap_login("new.user")
        self.store.reject_request(submitted["request_id"], "massima", "Not in scope")

        resubmitted = self.store.record_ldap_login("new.user")

        self.assertEqual(resubmitted["state"], "pending")
        self.assertNotEqual(resubmitted["request_id"], submitted["request_id"])
        self.assertEqual(len(self.store.list_requests("all")), 2)

    def test_admin_profile_permissions_are_not_editable(self):
        with self.assertRaisesRegex(ValueError, "fixes"):
            self.store.update_profile_privileges("admin", "massima", {"manage_accounts": False})

    def test_profile_privilege_changes_are_persisted_and_audited(self):
        self.store.update_profile_privileges("viewer", "massima", {"refresh_data": True})

        access = self.store.get_access("massima")
        viewer = next(profile for profile in self.store.list_profiles() if profile["profile_key"] == "viewer")
        refresh = next(item for item in viewer["privileges"] if item["privilege_key"] == "refresh_data")

        self.assertTrue(access["privileges"]["refresh_data"])
        self.assertTrue(refresh["can_access"])
        self.assertEqual(refresh["assigned_by"], "massima")
        self.assertIn("profile_privileges_updated", [entry["action"] for entry in self.store.list_audit()])

    def test_analyst_can_export_and_refresh_by_default(self):
        pending = self.store.record_ldap_login("analyst")
        self.store.approve_request(pending["request_id"], "massima", "analyst")

        privileges = self.store.get_access("analyst")["privileges"]

        self.assertTrue(privileges["export_data"])
        self.assertTrue(privileges["refresh_data"])
        self.assertFalse(privileges["manage_accounts"])
        self.assertFalse(privileges["manage_profiles"])

    def test_audit_records_account_lifecycle_without_credentials(self):
        submitted = self.store.record_ldap_login("new.user")
        self.store.approve_request(submitted["request_id"], "massima", "analyst")

        actions = [entry["action"] for entry in self.store.list_audit()]
        self.assertIn("account_request_submitted", actions)
        self.assertIn("account_request_approved", actions)
        self.assertNotIn("password", str(self.store.list_audit()).lower())


if __name__ == "__main__":
    unittest.main()