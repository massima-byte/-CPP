from __future__ import annotations

import ipaddress
import json
import mimetypes
import os
import secrets
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.config import BASE_DIR, SNAPSHOT_PATH, marketing_refresh_interval_seconds
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
        print(f"{self.client_address[0]} - {format_string % args}")

    def _json(self, status: int, value: dict) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local_only(self) -> bool:
        try:
            host = urlsplit(f"//{self.headers.get('Host', '')}").hostname
            return (
                ipaddress.ip_address(self.client_address[0]).is_loopback
                and host in {"127.0.0.1", "localhost", "::1"}
            )
        except ValueError:
            return False

    def do_GET(self) -> None:
        if not self._local_only():
            self._json(403, {"error": "Cette interface est limitée à la machine locale."})
            return
        path = urlsplit(self.path).path
        if path == "/api/health":
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
        if urlsplit(self.path).path != "/api/refresh":
            self.send_error(404)
            return
        if not self._origin_allowed():
            self._json(403, {"error": "Origine de requête refusée."})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(415, {"error": "Le rafraîchissement exige une requête JSON."})
            return
        try:
            body_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "Taille de requête invalide."})
            return
        if body_length > MAX_BODY_BYTES:
            self._json(413, {"error": "Requête trop volumineuse."})
            return
        try:
            snapshot = _refresh_snapshot()
        except RefreshError as error:
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

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        origin_url = urlsplit(origin)
        request_host = self.headers.get("Host", "").lower()
        return (
            origin_url.hostname in {"127.0.0.1", "localhost", "::1"}
            and origin_url.netloc.lower() == request_host
        )

def serve(host: str = "127.0.0.1", port: int = 8532) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Le serveur n'accepte que les interfaces loopback.")
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
