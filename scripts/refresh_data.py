from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.presto import RefreshError, refresh_marketing_data


def main() -> int:
    try:
        snapshot = refresh_marketing_data()
    except RefreshError as error:
        print(f"ÉCHEC du rafraîchissement : {error}", file=sys.stderr)
        return 1
    print(f"Snapshot Presto mis à jour : {snapshot['generated_at']}")
    for module, data_date in snapshot["source_dates"].items():
        print(f"  {module}: {data_date}")
    print("  Données individuelles/clientèle et churn exclues; aucune PII exportée.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
