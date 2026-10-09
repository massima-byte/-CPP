from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SNAPSHOT_PATH = DATA_DIR / "marketing_metrics.json"
DEFAULT_SOURCE_ENV = BASE_DIR.parent / "churn" / ".env"


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def load_local_environment() -> None:
    local_env = BASE_DIR / ".env"
    if local_env.is_file():
        _load_env_file(local_env)
    source_env = os.environ.get("MTN_PRESTO_ENV_FILE")
    if source_env:
        _load_env_file(Path(source_env).expanduser())
    elif DEFAULT_SOURCE_ENV.is_file():
        _load_env_file(DEFAULT_SOURCE_ENV)


def tls_verify_setting() -> bool | str:
    value = os.environ.get("MTN_PRESTO_TLS_VERIFY", "false").strip()
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off", ""}:
        return False
    return str(Path(value).expanduser())


def presto_settings() -> dict[str, object]:
    load_local_environment()
    return {
        "host": os.environ.get("MTN_PRESTO_HOST", "lnx-eva-master01.mtn.bj"),
        "port": int(os.environ.get("MTN_PRESTO_PORT", "8443")),
        "username": os.environ.get("MTN_PRESTO_USER", "itbireport_svc"),
        "password": os.environ.get("MTN_PRESTO_PASSWORD") or None,
        "catalog": os.environ.get("MTN_PRESTO_CATALOG", "hive"),
        "schema": os.environ.get("MTN_PRESTO_SCHEMA", "mfs_bi"),
        "protocol": os.environ.get("MTN_PRESTO_PROTOCOL", "https"),
        "tls_verify": tls_verify_setting(),
        "query_timeout": os.environ.get("MTN_PRESTO_QUERY_TIMEOUT", "90s"),
    }


def marketing_refresh_interval_seconds() -> int:
    load_local_environment()
    value = int(os.environ.get("MTN_MARKETING_REFRESH_INTERVAL_SECONDS", "900"))
    if value < 60:
        raise ValueError("MTN_MARKETING_REFRESH_INTERVAL_SECONDS doit être au moins 60.")
    return value
