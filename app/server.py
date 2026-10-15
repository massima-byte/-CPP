from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import secrets
import threading
from http.cookies import SimpleCookie
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.config import BASE_DIR, SNAPSHOT_PATH, access_database_path, marketing_refresh_interval_seconds
from app.access_control import AccessControlStore
from app.auth import (
    InvalidCredentialsError,
    LDAPUnavailableError,
    authenticate_ldap,
    create_session,
    delete_session,
    session_username,
)
from app.metrics import load_snapshot
from app.presto import RefreshError, is_transient_network_error, refresh_marketing_data


STATIC_DIR = BASE_DIR / "static"
MAX_BODY_BYTES = 1024
_refresh_lock = threading.Lock()
_refresh_state_lock = threading.Lock()
_refresh_state = {
    "in_progress": False,
    "last_attempt_at": None,
    "last_success_at": None,
    "last_error": None,
}
SESSION_COOKIE = "mtn_marketing_session"
access_store = AccessControlStore(access_database_path())


def _remote_access_enabled() -> bool:
    return os.environ.get("MTN_MARKETING_ALLOW_REMOTE", "false").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _allowed_hosts() -> set[str]:
    values = os.environ.get("MTN_MARKETING_ALLOWED_HOSTS", "10.77.60.96")
    return {value.strip().lower() for value in values.split(",") if value.strip()}


def request_address_allowed(
    client_address: str,
    host_header: str,
    *,
    allow_remote: bool,
    allowed_hosts: set[str],
) -> bool:
    try:
        host = urlsplit(f"//{host_header}").hostname
        client_ip = ipaddress.ip_address(client_address)
    except ValueError:
        return False
    if client_ip.is_loopback and host in {"127.0.0.1", "localhost", "::1"}:
        return True
    return allow_remote and host is not None and host.lower() in allowed_hosts


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _refresh_state_snapshot() -> dict:
    with _refresh_state_lock:
        return dict(_refresh_state)


def _refresh_snapshot() -> dict | None:
    if not _refresh_lock.acquire(blocking=False):
        return None
    with _refresh_state_lock:
        _refresh_state.update(
            in_progress=True,
            last_attempt_at=_iso_now(),
            last_error=None,
        )
    try:
        snapshot = refresh_marketing_data()
        with _refresh_state_lock:
            _refresh_state["last_success_at"] = snapshot["generated_at"]
        return snapshot
    except RefreshError as error:
        with _refresh_state_lock:
            _refresh_state["last_error"] = str(error)
        raise
    finally:
        with _refresh_state_lock:
            _refresh_state["in_progress"] = False
        _refresh_lock.release()


def _retry_seconds() -> int:
    """Délai de relance après une panne réseau (5 s minimum, 60 s par défaut)."""
    try:
        value = int(os.environ.get("MTN_MARKETING_RETRY_SECONDS") or 60)
    except ValueError:
        return 60
    return max(5, value)


def refresh_failure_delay(error: Exception, interval_seconds: int) -> float:
    """Attente avant la prochaine tentative après un échec.

    Panne réseau → on retente vite (le bandeau rouge disparaît tout seul) ;
    échec métier → cadence normale, une partition absente ne réapparaîtra pas
    en une minute.
    """
    if is_transient_network_error(error):
        return float(min(interval_seconds, _retry_seconds()))
    return float(interval_seconds)


def _seconds_until_refresh(
    snapshot: dict | None, now: datetime, interval_seconds: int
) -> float:
    generated_at = snapshot.get("generated_at") if snapshot else None
    if not generated_at:
        return 0
    try:
        generated = datetime.fromisoformat(generated_at)
    except (TypeError, ValueError):
        return 0
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return max(0.0, interval_seconds - (now - generated.astimezone(timezone.utc)).total_seconds())


def _refresh_scheduler(stop_event: threading.Event, interval_seconds: int) -> None:
    while not stop_event.is_set():
        try:
            snapshot = load_snapshot()
        except ValueError as error:
            print(f"Snapshot Presto invalide, nouvelle extraction requise : {error}", flush=True)
            snapshot = None
        if snapshot:
            with _refresh_state_lock:
                if _refresh_state["last_success_at"] is None:
                    _refresh_state["last_success_at"] = snapshot.get("generated_at")
        delay = _seconds_until_refresh(snapshot, _utc_now(), interval_seconds)
        if delay and stop_event.wait(delay):
            return
        if stop_event.is_set():
            return
        try:
            refreshed = _refresh_snapshot()
            if refreshed is None:
                stop_event.wait(5)
                continue
            print(f"Presto snapshot actualisé automatiquement · {_iso_now()}", flush=True)
        except RefreshError as error:
            delay = refresh_failure_delay(error, interval_seconds)
            print(
                f"Actualisation Presto automatique impossible : {error} "
                f"— nouvelle tentative dans {delay:g} s",
                flush=True,
            )
            if stop_event.wait(delay):
                return


def snapshot_age_days(snapshot: dict) -> int | None:
    generated_at = snapshot.get("generated_at")
    if not generated_at:
        return None
    try:
        generated = datetime.fromisoformat(generated_at)
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc).date() - generated.date()).days)


class MarketingHandler(BaseHTTPRequestHandler):
    server_version = "MTNMarketingMonitor/1.0"
    sys_version = ""

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; script-src 'self' 'nonce-{getattr(self, 'script_nonce', '')}'; "
            "style-src 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def log_message(self, format_string: str, *args: object) -> None:
        request_line = str(args[0]) if args else self.requestline
        parts = request_line.split(" ", 2)
        method = parts[0] if parts else "?"
        path = urlsplit(parts[1]).path if len(parts) > 1 else "/"
        status = args[1] if len(args) > 1 else "-"
        print(f'{self.client_address[0]} - "{method} {path}" {status}')

    def _json(self, status: int, value: dict, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.end_headers()
        self.wfile.write(body)

    def _session_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def _read_json_object(self) -> dict | None:
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Une requête JSON est requise."})
            return None
        try:
            body_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "Taille de requête invalide."})
            return None
        if body_length < 0 or body_length > MAX_BODY_BYTES:
            self._json(413, {"error": "Requête trop volumineuse."})
            return None
        try:
            payload = json.loads(self.rfile.read(body_length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "Corps JSON invalide."})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": "Un objet JSON est requis."})
            return None
        return payload

    def _session_cookie(self, token: str, timeout: int) -> str:
        value = f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={timeout}"
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            value += "; Secure"
        return value

    def _clear_session_cookie(self) -> str:
        value = f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
            value += "; Secure"
        return value

    def _local_only(self) -> bool:
        return request_address_allowed(
            self.client_address[0],
            self.headers.get("Host", ""),
            allow_remote=_remote_access_enabled(),
            allowed_hosts=_allowed_hosts(),
        )

    def do_GET(self) -> None:
        if not self._local_only():
            self._json(403, {"error": "Cette interface est limitée à la machine locale."})
            return
        path = urlsplit(self.path).path
        if path == "/api/session":
            username = session_username(self._session_token())
            access = access_store.get_access(username) if username else None
            if username and access is None:
                delete_session(self._session_token())
            self._json(
                200,
                {
                    "authenticated": access is not None,
                    "username": access["username"] if access else None,
                    "profile": access["profile_key"] if access else None,
                    "privileges": access["privileges"] if access else {},
                },
            )
            return
        username = session_username(self._session_token())
        access = access_store.get_access(username) if username else None
        if path.startswith("/api/") and access is None:
            if username:
                delete_session(self._session_token())
            self._json(401, {"error": "Authentification LDAP requise."})
            return
        if path.startswith("/api/admin/"):
            if path == "/api/admin/requests":
                allowed = access["privileges"].get("review_account_requests", False)
                required = "review_account_requests"
            elif path == "/api/admin/users":
                allowed = access["privileges"].get("manage_accounts", False)
                required = "manage_accounts"
            elif path == "/api/admin/profiles":
                allowed = any(
                    access["privileges"].get(key, False)
                    for key in ("review_account_requests", "manage_accounts", "manage_profiles")
                )
                required = "review_account_requests, manage_accounts ou manage_profiles"
            elif path == "/api/admin/audit":
                allowed = access["privileges"].get("view_audit_logs", False)
                required = "view_audit_logs"
            else:
                self.send_error(404)
                return
            if not allowed:
                access_store.record_event(username, "authorization_denied", path, "denied", {"privilege": required})
                self._json(403, {"error": "Privilège administrateur requis."})
                return
            if path == "/api/admin/requests":
                self._json(200, {"requests": access_store.list_requests("pending")})
                return
            if path == "/api/admin/users":
                self._json(200, {"users": access_store.list_users()})
                return
            if path == "/api/admin/profiles":
                self._json(200, {"profiles": access_store.list_profiles()})
                return
            if path == "/api/admin/audit":
                self._json(200, {"entries": access_store.list_audit()})
                return
            self.send_error(404)
            return
        if path == "/api/health":
            if not access["privileges"].get("view_dashboard", False):
                self._json(403, {"error": "Privilège de consultation requis."})
                return
            snapshot = load_snapshot()
            refresh_state = _refresh_state_snapshot()
            refresh_interval = marketing_refresh_interval_seconds()
            self._json(
                200,
                {
                    "status": "ok",
                    "refresh_available": True,
                    "snapshot_present": snapshot is not None,
                    "snapshot_age_days": snapshot_age_days(snapshot) if snapshot else None,
                    "snapshot_generated_at": snapshot.get("generated_at") if snapshot else None,
                    "snapshot_path": str(SNAPSHOT_PATH.name) if snapshot else None,
                    "refresh_interval_seconds": refresh_interval,
                    "refresh": refresh_state,
                },
            )
            return
        if path == "/api/metrics":
            if not access["privileges"].get("view_dashboard", False):
                self._json(403, {"error": "Privilège de consultation requis."})
                return
            snapshot = load_snapshot()
            if snapshot is None:
                self._json(
                    503,
                    {"error": "Aucun snapshot Presto disponible. Lancez le rafraîchissement."},
                )
                return
            self._json(200, snapshot)
            return
        self._serve_static(path)

    def _serve_static(self, request_path: str) -> None:
        if request_path == "/":
            file_path = BASE_DIR / "index.html"
            self.script_nonce = secrets.token_urlsafe(18)
        elif request_path.startswith("/static/"):
            relative = unquote(request_path.removeprefix("/static/"))
            file_path = (STATIC_DIR / relative).resolve()
            if STATIC_DIR.resolve() not in file_path.parents:
                self.send_error(404)
                return
        else:
            self.send_error(404)
            return
        if not file_path.is_file():
            self.send_error(404)
            return
        content = file_path.read_bytes()
        if request_path == "/":
            html = content.decode("utf-8")
            if html.count("<script>") != 1:
                self.send_error(500, "La page doit contenir exactement un script applicatif.")
                return
            content = html.replace(
                "<script>",
                f'<script nonce="{self.script_nonce}">',
                1,
            ).encode("utf-8")
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{mime_type}; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self) -> None:
        if not self._local_only():
            self._json(403, {"error": "Cette interface est limitée à la machine locale."})
            return
        path = urlsplit(self.path).path
        if path not in {"/api/login", "/api/logout", "/api/refresh"} and not path.startswith("/api/admin/"):
            self.send_error(404)
            return
        if not self._origin_allowed():
            self._json(403, {"error": "Origine de requête refusée."})
            return
        payload = self._read_json_object()
        if payload is None:
            return

        if path == "/api/login":
            username = payload.get("username")
            password = payload.get("password")
            if not isinstance(username, str) or not isinstance(password, str):
                self._json(400, {"error": "Identifiant et mot de passe requis."})
                return
            try:
                authenticated_username = authenticate_ldap(username, password)
            except InvalidCredentialsError:
                access_store.record_event(username, "login", username, "invalid_credentials")
                self._json(401, {"error": "Identifiants LDAP invalides."})
                return
            except LDAPUnavailableError:
                access_store.record_event(username, "login", username, "ldap_unavailable")
                self._json(503, {"error": "Service LDAP indisponible ou certificat non approuvé."})
                return
            email = username.strip() if "@" in username else ""
            decision = access_store.record_ldap_login(authenticated_username, email)
            if decision["state"] == "pending":
                self._json(
                    202,
                    {
                        "status": "pending",
                        "message": "Demande envoyée aux administrateurs. Votre accès sera activé après approbation.",
                    },
                )
                return
            if decision["state"] == "disabled":
                self._json(403, {"error": "Votre compte est désactivé. Contactez un administrateur."})
                return
            token, timeout = create_session(authenticated_username)
            self._json(
                200,
                {"authenticated": True, "username": authenticated_username},
                {"Set-Cookie": self._session_cookie(token, timeout)},
            )
            return

        if path == "/api/logout":
            username = session_username(self._session_token())
            delete_session(self._session_token())
            if username:
                access_store.record_event(username, "logout", username, "success")
            self._json(
                200,
                {"authenticated": False},
                {"Set-Cookie": self._clear_session_cookie()},
            )
            return

        username = session_username(self._session_token())
        access = access_store.get_access(username) if username else None
        if access is None:
            if username:
                delete_session(self._session_token())
            self._json(401, {"error": "Authentification LDAP requise."})
            return
        if path.startswith("/api/admin/"):
            segments = path.strip("/").split("/")
            if len(segments) >= 3 and segments[2] == "requests":
                required = "review_account_requests"
            elif len(segments) >= 3 and segments[2] == "users":
                required = "manage_accounts"
            elif len(segments) >= 3 and segments[2] == "profiles":
                required = "manage_profiles"
            else:
                self.send_error(404)
                return
            if not access["privileges"].get(required, False):
                access_store.record_event(username, "authorization_denied", path, "denied", {"privilege": required})
                self._json(403, {"error": "Privilège administrateur requis."})
                return
            try:
                if len(segments) == 5 and segments[2] == "requests":
                    request_id = int(segments[3])
                    if segments[4] == "approve":
                        result = access_store.approve_request(
                            request_id, username, str(payload.get("profile_key", "viewer"))
                        )
                        self._json(200, {"user": result})
                        return
                    if segments[4] == "reject":
                        access_store.reject_request(request_id, username, str(payload.get("note", "")))
                        self._json(200, {"status": "rejected"})
                        return
                if len(segments) == 4 and segments[2] == "users":
                    result = access_store.update_user(
                        int(segments[3]),
                        username,
                        str(payload.get("profile_key", "viewer")),
                        str(payload.get("status", "active")),
                        str(payload.get("email", "")),
                        str(payload.get("full_name", "")),
                    )
                    self._json(200, {"user": result})
                    return
                if len(segments) == 5 and segments[2] == "profiles" and segments[4] == "privileges":
                    privileges = payload.get("privileges")
                    if not isinstance(privileges, dict):
                        raise ValueError("Une matrice de privilèges est requise.")
                    access_store.update_profile_privileges(segments[3], username, privileges)
                    self._json(200, {"status": "updated"})
                    return
            except (ValueError, TypeError) as error:
                self._json(400, {"error": str(error)})
                return
            self.send_error(404)
            return
        if path == "/api/refresh" and not access["privileges"].get("refresh_data", False):
            access_store.record_event(username, "refresh_data", "snapshot", "denied")
            self._json(403, {"error": "Votre profil ne permet pas d’actualiser les données."})
            return
        try:
            snapshot = _refresh_snapshot()
        except RefreshError as error:
            access_store.record_event(username, "refresh_data", "snapshot", "failed", {"error": str(error)})
            self._json(502, {"error": str(error)})
            return
        if snapshot is None:
            self._json(409, {"error": "Un rafraîchissement Presto est déjà en cours."})
            return
        self._json(
            200,
            {
                "status": "ok",
                "generated_at": snapshot["generated_at"],
                "source_dates": snapshot["source_dates"],
            },
        )
        access_store.record_event(username, "refresh_data", "snapshot", "success")

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        origin_url = urlsplit(origin)
        request_host = self.headers.get("Host", "").lower()
        return (
            origin_url.scheme in {"http", "https"}
            and origin_url.netloc.lower() == request_host
        )

def serve(host: str = "127.0.0.1", port: int = 8532) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"} and not (
        host == "0.0.0.0" and _remote_access_enabled()
    ):
        raise ValueError("Le bind réseau exige MTN_MARKETING_ALLOW_REMOTE=true.")
    server = ThreadingHTTPServer((host, port), MarketingHandler)
    server.daemon_threads = True
    stop_event = threading.Event()
    refresh_interval = marketing_refresh_interval_seconds()
    scheduler = threading.Thread(
        target=_refresh_scheduler,
        args=(stop_event, refresh_interval),
        name="marketing-refresh",
        daemon=True,
    )
    scheduler.start()
    print(f"MTN Marketing Monitor prêt : http://{host}:{port}", flush=True)
    print(
        f"Rafraîchissement Presto automatique · toutes les {refresh_interval} secondes",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt demandé.", flush=True)
    finally:
        stop_event.set()
        scheduler.join(timeout=5)
        server.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Serveur local du monitoring marketing MTN")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MTN_MARKETING_PORT", "8532")),
    )
    options = parser.parse_args()
    serve(options.host, options.port)
