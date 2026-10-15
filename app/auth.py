from __future__ import annotations

import os
import secrets
import ssl
import threading
import time

from ldap3 import Connection, Server, Tls
from ldap3.core.exceptions import LDAPException

from app.config import load_local_environment


class InvalidCredentialsError(RuntimeError):
    pass


class LDAPUnavailableError(RuntimeError):
    pass


_sessions: dict[str, dict[str, float | str]] = {}
_sessions_lock = threading.Lock()


def _session_timeout() -> int:
    load_local_environment()
    try:
        timeout = int(os.environ.get("MTN_MARKETING_SESSION_TIMEOUT", "3600"))
    except ValueError as error:
        raise ValueError("MTN_MARKETING_SESSION_TIMEOUT doit être un entier.") from error
    if timeout < 60:
        raise ValueError("MTN_MARKETING_SESSION_TIMEOUT doit être au moins 60.")
    return timeout


def _bind_username(username: str) -> str:
    short_username = username.strip().split("@", 1)[0].split("\\")[-1].strip()
    if not short_username:
        raise InvalidCredentialsError
    load_local_environment()
    domain = os.environ.get("MTN_LDAP_DOMAIN", "mtn.local").strip()
    bind_format = os.environ.get("MTN_LDAP_BIND_FORMAT", "{username}@{domain}")
    try:
        return bind_format.format(username=short_username, domain=domain)
    except (KeyError, ValueError) as error:
        raise LDAPUnavailableError("Le format de bind LDAP est invalide.") from error


def authenticate_ldap(username: str, password: str) -> str:
    if not username or not password:
        raise InvalidCredentialsError

    load_local_environment()
    host = os.environ.get("MTN_LDAP_HOST", "10.77.71.15").strip()
    try:
        port = int(os.environ.get("MTN_LDAP_PORT", "389"))
        connect_timeout = float(os.environ.get("MTN_LDAP_CONNECT_TIMEOUT", "5"))
        use_tls = os.environ.get("MTN_LDAP_USE_TLS", "false").strip().lower() in {
            "1", "true", "yes", "on"
        }
        tls = (
            Tls(
                validate=ssl.CERT_REQUIRED,
                ca_certs_file=os.environ.get("MTN_LDAP_CA_CERT_FILE") or None,
            )
            if use_tls
            else None
        )
        server = Server(
            host,
            port=port,
            use_ssl=use_tls,
            tls=tls,
            connect_timeout=connect_timeout,
        )
        connection = Connection(
            server,
            user=_bind_username(username),
            password=password,
            auto_bind=False,
            raise_exceptions=False,
        )
    except (LDAPException, OSError, ValueError) as error:
        raise LDAPUnavailableError("Connexion LDAP indisponible.") from error

    try:
        if connection.bind():
            return username.strip().split("@", 1)[0].split("\\")[-1].strip()
        result = connection.result or {}
        if result.get("result") == 49 or "data 775" in result.get("message", ""):
            raise InvalidCredentialsError
        raise LDAPUnavailableError("Connexion LDAP indisponible.")
    except LDAPException as error:
        raise LDAPUnavailableError("Connexion LDAP indisponible.") from error
    finally:
        connection.unbind()


def create_session(username: str) -> tuple[str, int]:
    timeout = _session_timeout()
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[token] = {"username": username, "expires_at": time.time() + timeout}
    return token, timeout


def session_username(token: str | None) -> str | None:
    if not token:
        return None
    now = time.time()
    with _sessions_lock:
        session = _sessions.get(token)
        if not session:
            return None
        if float(session["expires_at"]) <= now:
            del _sessions[token]
            return None
        session["expires_at"] = now + _session_timeout()
        return str(session["username"])


def delete_session(token: str | None) -> None:
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)