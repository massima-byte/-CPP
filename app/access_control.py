from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


BOOTSTRAP_ADMIN = "massima"
PROFILE_DEFINITIONS = {
    "viewer": ("Viewer", "Consultation des indicateurs marketing."),
    "analyst": ("Analyste", "Consultation, export et actualisation des indicateurs."),
    "admin": ("Administrateur", "Gestion des demandes, comptes, profils, droits et journaux."),
}
PRIVILEGE_DEFINITIONS = {
    "view_dashboard": ("Consulter le tableau de bord", "Accéder aux indicateurs agrégés."),
    "export_data": ("Exporter les données", "Télécharger un export CSV depuis les modules."),
    "refresh_data": ("Actualiser les données", "Lancer une extraction Presto."),
    "review_account_requests": ("Traiter les demandes", "Approuver ou refuser les demandes LDAP."),
    "manage_accounts": ("Gérer les comptes", "Modifier les comptes, profils attribués et états."),
    "manage_profiles": ("Gérer les profils", "Modifier les privilèges Viewer et Analyste."),
    "view_audit_logs": ("Consulter les journaux", "Consulter l’historique des actions."),
}
PROFILE_PRIVILEGES = {
    "viewer": {"view_dashboard"},
    "analyst": {"view_dashboard", "export_data", "refresh_data"},
    "admin": set(PRIVILEGE_DEFINITIONS),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _username(value: str) -> str:
    return value.strip().split("@", 1)[0].split("\\")[-1].strip().casefold()


class AccessControlStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS profiles (
                    id INTEGER PRIMARY KEY,
                    profile_key TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    is_system INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS privileges (
                    privilege_key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profile_privileges (
                    profile_id INTEGER NOT NULL REFERENCES profiles(id),
                    privilege_key TEXT NOT NULL REFERENCES privileges(privilege_key),
                    can_access INTEGER NOT NULL CHECK (can_access IN (0, 1)),
                    assigned_at TEXT NOT NULL,
                    assigned_by TEXT NOT NULL,
                    PRIMARY KEY (profile_id, privilege_key)
                );
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    full_name TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL DEFAULT '',
                    profile_id INTEGER NOT NULL REFERENCES profiles(id),
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_requests (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE,
                    email TEXT NOT NULL DEFAULT '',
                    requested_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
                    reviewed_at TEXT,
                    reviewed_by TEXT,
                    review_note TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_request_per_user
                    ON account_requests(username) WHERE status = 'pending';
                CREATE INDEX IF NOT EXISTS account_requests_status_date
                    ON account_requests(status, requested_at DESC);
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS audit_logs_date
                    ON audit_logs(occurred_at DESC);
                """
            )
            for key, (name, description) in PROFILE_DEFINITIONS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO profiles(profile_key, name, description, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (key, name, description, _now()),
                )
                connection.execute(
                    "UPDATE profiles SET name = ?, description = ? WHERE profile_key = ?",
                    (name, description, key),
                )
            for key, (name, description) in PRIVILEGE_DEFINITIONS.items():
                connection.execute(
                    "INSERT OR IGNORE INTO privileges(privilege_key, name, description) "
                    "VALUES (?, ?, ?)",
                    (key, name, description),
                )
                connection.execute(
                    "UPDATE privileges SET name = ?, description = ? WHERE privilege_key = ?",
                    (name, description, key),
                )
            for profile_key, privilege_keys in PROFILE_PRIVILEGES.items():
                profile_id = connection.execute(
                    "SELECT id FROM profiles WHERE profile_key = ?", (profile_key,)
                ).fetchone()["id"]
                for privilege_key in PRIVILEGE_DEFINITIONS:
                    connection.execute(
                        "INSERT OR IGNORE INTO profile_privileges "
                        "(profile_id, privilege_key, can_access, assigned_at, assigned_by) "
                        "VALUES (?, ?, ?, ?, 'system')",
                        (profile_id, privilege_key, int(privilege_key in privilege_keys), _now()),
                    )
            admin_profile_id = connection.execute(
                "SELECT id FROM profiles WHERE profile_key = 'admin'"
            ).fetchone()["id"]
            admin = connection.execute(
                "SELECT id, profile_id, status FROM users WHERE username = ?",
                (BOOTSTRAP_ADMIN,),
            ).fetchone()
            if admin is None:
                now = _now()
                connection.execute(
                    "INSERT INTO users "
                    "(username, full_name, profile_id, status, created_at, updated_at, created_by, updated_by) "
                    "VALUES (?, ?, ?, 'active', ?, ?, 'system', 'system')",
                    (BOOTSTRAP_ADMIN, "Massima", admin_profile_id, now, now),
                )
                self._audit(connection, "system", "bootstrap_admin", BOOTSTRAP_ADMIN, "success")
            elif admin["profile_id"] != admin_profile_id or admin["status"] != "active":
                connection.execute(
                    "UPDATE users SET profile_id = ?, status = 'active', updated_at = ?, updated_by = 'system' "
                    "WHERE id = ?",
                    (admin_profile_id, _now(), admin["id"]),
                )
                self._audit(connection, "system", "restore_bootstrap_admin", BOOTSTRAP_ADMIN, "success")

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        actor: str,
        action: str,
        target: str = "",
        outcome: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_logs(occurred_at, actor, action, target, outcome, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), actor, action, target, outcome, json.dumps(details or {}, ensure_ascii=False)),
        )

    def record_ldap_login(self, username: str, email: str = "") -> dict[str, Any]:
        username = _username(username)
        if not username:
            raise ValueError("Identifiant utilisateur invalide.")
        with self._connection() as connection:
            user = connection.execute(
                "SELECT id, status FROM users WHERE username = ?", (username,)
            ).fetchone()
            if user and user["status"] == "active":
                self._audit(connection, username, "login", username, "success")
                return {"state": "active", "access": self._get_access(connection, username)}
            if user:
                self._audit(connection, username, "login", username, "denied", {"reason": "disabled"})
                return {"state": "disabled"}

            request = connection.execute(
                "SELECT id, status FROM account_requests WHERE username = ? "
                "ORDER BY id DESC LIMIT 1",
                (username,),
            ).fetchone()
            if request and request["status"] == "pending":
                if email:
                    connection.execute(
                        "UPDATE account_requests SET email = ? WHERE id = ? AND email = ''",
                        (email, request["id"]),
                    )
                self._audit(connection, username, "login", username, "pending")
                return {"state": "pending", "request_id": request["id"]}

            cursor = connection.execute(
                "INSERT INTO account_requests(username, email, requested_at, status) "
                "VALUES (?, ?, ?, 'pending')",
                (username, email, _now()),
            )
            request_id = cursor.lastrowid
            self._audit(
                connection,
                username,
                "account_request_submitted",
                username,
                "pending",
                {"request_id": request_id},
            )
            return {"state": "pending", "request_id": request_id}

    def record_event(
        self,
        actor: str,
        action: str,
        target: str = "",
        outcome: str = "success",
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._connection() as connection:
            self._audit(connection, _username(actor) or "anonymous", action, target, outcome, details)

    def get_access(self, username: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            return self._get_access(connection, _username(username))

    @staticmethod
    def _get_access(connection: sqlite3.Connection, username: str) -> dict[str, Any] | None:
        user = connection.execute(
            "SELECT users.id, users.username, users.full_name, users.email, users.status, users.profile_id, "
            "profiles.profile_key, profiles.name AS profile_name "
            "FROM users JOIN profiles ON profiles.id = users.profile_id "
            "WHERE users.username = ?",
            (username,),
        ).fetchone()
        if user is None or user["status"] != "active":
            return None
        privileges = connection.execute(
            "SELECT privilege_key, can_access FROM profile_privileges WHERE profile_id = ?",
            (user["profile_id"],),
        ).fetchall()
        access = {row["privilege_key"]: bool(row["can_access"]) for row in privileges}
        if username == BOOTSTRAP_ADMIN:
            access = {key: True for key in PRIVILEGE_DEFINITIONS}
        result = dict(user)
        result["privileges"] = access
        return result

    def list_requests(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        if status not in {"pending", "approved", "rejected", "all"}:
            raise ValueError("Filtre de demandes invalide.")
        with self._connection() as connection:
            condition = "" if status == "all" else "WHERE status = ?"
            parameters: tuple[Any, ...] = () if status == "all" else (status,)
            rows = connection.execute(
                f"SELECT * FROM account_requests {condition} ORDER BY requested_at DESC LIMIT ?",
                (*parameters, min(max(limit, 1), 500)),
            ).fetchall()
            return [dict(row) for row in rows]

    def list_users(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT users.id, users.username, users.full_name, users.email, users.status, "
                "users.created_at, users.updated_at, profiles.profile_key, profiles.name AS profile_name "
                "FROM users JOIN profiles ON profiles.id = users.profile_id "
                "ORDER BY users.username COLLATE NOCASE"
            ).fetchall()
            return [dict(row) for row in rows]

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            profiles = connection.execute("SELECT * FROM profiles ORDER BY id").fetchall()
            result = []
            for profile in profiles:
                privileges = connection.execute(
                    "SELECT privileges.privilege_key, privileges.name, privileges.description, "
                    "profile_privileges.can_access, profile_privileges.assigned_at, "
                    "profile_privileges.assigned_by FROM privileges JOIN profile_privileges "
                    "ON profile_privileges.privilege_key = privileges.privilege_key "
                    "WHERE profile_privileges.profile_id = ? ORDER BY privileges.privilege_key",
                    (profile["id"],),
                ).fetchall()
                result.append({**dict(profile), "privileges": [dict(row) for row in privileges]})
            return result

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 500),),
            ).fetchall()
            result = []
            for row in rows:
                entry = dict(row)
                entry["details"] = json.loads(entry["details"])
                result.append(entry)
            return result

    def approve_request(self, request_id: int, actor: str, profile_key: str) -> dict[str, Any]:
        if profile_key not in PROFILE_DEFINITIONS:
            raise ValueError("Profil inconnu.")
        actor = _username(actor)
        with self._connection() as connection:
            request = connection.execute(
                "SELECT * FROM account_requests WHERE id = ? AND status = 'pending'",
                (request_id,),
            ).fetchone()
            if request is None:
                raise ValueError("Cette demande n’est plus en attente.")
            if request["username"] == BOOTSTRAP_ADMIN:
                profile_key = "admin"
            profile_id = connection.execute(
                "SELECT id FROM profiles WHERE profile_key = ?", (profile_key,)
            ).fetchone()["id"]
            now = _now()
            existing = connection.execute(
                "SELECT id FROM users WHERE username = ?", (request["username"],)
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE users SET profile_id = ?, status = 'active', email = ?, "
                    "updated_at = ?, updated_by = ? WHERE id = ?",
                    (profile_id, request["email"], now, actor, existing["id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO users "
                    "(username, email, profile_id, status, created_at, updated_at, created_by, updated_by) "
                    "VALUES (?, ?, ?, 'active', ?, ?, ?, ?)",
                    (request["username"], request["email"], profile_id, now, now, actor, actor),
                )
            connection.execute(
                "UPDATE account_requests SET status = 'approved', reviewed_at = ?, reviewed_by = ? "
                "WHERE id = ?",
                (now, actor, request_id),
            )
            self._audit(
                connection,
                actor,
                "account_request_approved",
                request["username"],
                "success",
                {"request_id": request_id, "profile": profile_key},
            )
            return self._get_access(connection, request["username"])

    def reject_request(self, request_id: int, actor: str, note: str = "") -> None:
        actor = _username(actor)
        with self._connection() as connection:
            request = connection.execute(
                "SELECT username FROM account_requests WHERE id = ? AND status = 'pending'",
                (request_id,),
            ).fetchone()
            if request is None:
                raise ValueError("Cette demande n’est plus en attente.")
            now = _now()
            connection.execute(
                "UPDATE account_requests SET status = 'rejected', reviewed_at = ?, reviewed_by = ?, "
                "review_note = ? WHERE id = ?",
                (now, actor, note.strip()[:500], request_id),
            )
            self._audit(
                connection,
                actor,
                "account_request_rejected",
                request["username"],
                "success",
                {"request_id": request_id, "note": note.strip()[:500]},
            )

    def update_user(
        self,
        user_id: int,
        actor: str,
        profile_key: str,
        status: str,
        email: str = "",
        full_name: str = "",
    ) -> dict[str, Any]:
        if profile_key not in PROFILE_DEFINITIONS or status not in {"active", "disabled"}:
            raise ValueError("Profil ou état utilisateur invalide.")
        actor = _username(actor)
        with self._connection() as connection:
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                raise ValueError("Compte introuvable.")
            if user["username"] == BOOTSTRAP_ADMIN and (profile_key != "admin" or status != "active"):
                raise ValueError("Le compte massima doit rester administrateur actif.")
            profile_id = connection.execute(
                "SELECT id FROM profiles WHERE profile_key = ?", (profile_key,)
            ).fetchone()["id"]
            connection.execute(
                "UPDATE users SET profile_id = ?, status = ?, full_name = ?, email = ?, updated_at = ?, updated_by = ? "
                "WHERE id = ?",
                (profile_id, status, full_name.strip()[:160], email.strip()[:254], _now(), actor, user_id),
            )
            self._audit(
                connection,
                actor,
                "user_updated",
                user["username"],
                "success",
                {"profile": profile_key, "status": status, "full_name": full_name.strip()[:160]},
            )
            return self._get_access(connection, user["username"])

    def update_profile_privileges(
        self, profile_key: str, actor: str, privileges: dict[str, bool]
    ) -> None:
        if profile_key not in PROFILE_DEFINITIONS:
            raise ValueError("Profil inconnu.")
        if profile_key == "admin":
            raise ValueError("Les privilèges du profil Administrateur sont fixes.")
        if not privileges or any(
            key not in PRIVILEGE_DEFINITIONS or type(value) is not bool
            for key, value in privileges.items()
        ):
            raise ValueError("Matrice de privilèges invalide.")
        actor = _username(actor)
        with self._connection() as connection:
            profile = connection.execute(
                "SELECT id FROM profiles WHERE profile_key = ?", (profile_key,)
            ).fetchone()
            now = _now()
            for privilege_key, can_access in privileges.items():
                connection.execute(
                    "UPDATE profile_privileges SET can_access = ?, assigned_at = ?, assigned_by = ? "
                    "WHERE profile_id = ? AND privilege_key = ?",
                    (int(can_access), now, actor, profile["id"], privilege_key),
                )
            self._audit(
                connection,
                actor,
                "profile_privileges_updated",
                profile_key,
                "success",
                privileges,
            )
