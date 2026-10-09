from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, SNAPSHOT_PATH


DEPARTMENT_NAMES = {
    "ATLANTIQUE": "Atlantique",
    "LITTORAL": "Littoral",
    "OUEME": "Ouémé",
    "PLATEAU": "Plateau",
    "COLLINES": "Collines",
    "ZOU": "Zou",
    "BORGOU": "Borgou",
    "ALIBORI": "Alibori",
    "ATACORA": "Atacora",
    "DONGA": "Donga",
    "MONO": "Mono",
    "COUFFO": "Couffo",
    "NON RENSEIGNE": "Non renseigné",
}


SERVICE_LABELS = {
    "ECW": "ECW",
    "MOMO": "MoMo",
    "USSD": "USSD",
    "APP": "App",
    "GIFT": "Gift",
}


def iso_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    result = str(value or "").replace("-", "")[:8]
    if len(result) != 8 or not result.isdigit():
        raise ValueError("Presto returned an invalid date partition")
    return f"{result[:4]}-{result[4:6]}-{result[6:8]}"


def date_key(value: Any) -> str:
    return iso_date(value).replace("-", "")


def finite_number(value: Any, field: str) -> float:
    if value is None:
        raise ValueError(f"Presto returned a missing numeric value for {field}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"Presto returned an invalid numeric value for {field}")
    return result


def service_name(code: Any) -> str:
    product_code = str(code or "").upper()
    if product_code.startswith("3GB"):
        category = "Forfait data"
    elif product_code.startswith("MX"):
        category = "Bundle services"
    elif product_code.startswith("SM"):
        category = "SMS"
    elif product_code.startswith("VO"):
        category = "Voix"
    elif product_code.startswith("2G"):
        category = "Forfait 2G"
    else:
        category = "Service"
    channel = next(
        (label for marker, label in SERVICE_LABELS.items() if marker in product_code),
        "",
    )
    return f"{category} ({channel})" if channel else category


def normalize_department(raw: Any) -> str:
    key = str(raw or "").strip().upper()
    if not key:
        return "Non renseigné"
    return DEPARTMENT_NAMES.get(key, key.title())


def freshness_state(partition: Any, reference: date, current_state: str) -> str:
    partition_date = date.fromisoformat(iso_date(partition))
    lag_days = max(0, (reference - partition_date).days)
    if lag_days > 1:
        return f"Retard de {lag_days} j · dernière partition disponible"
    return current_state


def build_hourly_comparison(
    current: dict[str, Any], previous: dict[str, Any]
) -> dict[str, Any] | None:
    if (
        current.get("transactions") is None
        or current.get("revenue") is None
        or previous.get("transactions") is None
        or previous.get("revenue") is None
    ):
        return None
    revenue_delta = current["revenue"] - previous["revenue"]
    transaction_delta = current["transactions"] - previous["transactions"]
    return {
        "previousTimestamp": previous["timestamp"],
        "previousRevenue": previous["revenue"],
        "revenueDelta": revenue_delta,
        "revenuePercentChange": (
            revenue_delta / previous["revenue"] if previous["revenue"] else None
        ),
        "previousTransactions": previous["transactions"],
        "transactionsDelta": transaction_delta,
        "transactionsPercentChange": (
            transaction_delta / previous["transactions"]
            if previous["transactions"]
            else None
        ),
    }


def build_hourly_trend(rows: list[dict[str, Any]], reference: datetime) -> dict[str, Any]:
    end_hour = reference.replace(minute=0, second=0, microsecond=0)
    first_hour = end_hour - timedelta(hours=24)
    by_hour: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        source_date = iso_date(row["date"])
        hour = int(row["hour"])
        if not 0 <= hour <= 23:
            continue
        key = (source_date, hour)
        bucket = by_hour.setdefault(key, {"revenue": 0.0, "transactions": 0})
        bucket["revenue"] += finite_number(row["revenue"], "hourly revenue")
        bucket["transactions"] += int(row["transactions"] or 0)

    hours = []
    for offset in range(24):
        hour_start = first_hour + timedelta(hours=offset)
        observed = by_hour.get((hour_start.date().isoformat(), hour_start.hour))
        hours.append(
            {
                "timestamp": hour_start.isoformat(timespec="minutes"),
                "date": hour_start.date().isoformat(),
                "hour": hour_start.hour,
                "revenue": observed["revenue"] if observed else None,
                "transactions": observed["transactions"] if observed else None,
            }
        )
    previous = hours[-1]
    previous_hour = previous if previous["transactions"] is not None else None
    return {
        "hours": hours,
        "previous_hour": previous_hour,
        "hour_over_hour": build_hourly_comparison(previous, hours[-2]),
    }


def build_day_over_day(
    current_date: str,
    current_revenue: float,
    trend_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    previous_date = (date.fromisoformat(current_date) - timedelta(days=1)).isoformat()
    previous_revenue = next(
        (row["revenue"] for row in trend_rows if row["date"] == previous_date),
        None,
    )
    if previous_revenue is None:
        return None
    return {
        "previousDate": previous_date,
        "previousRevenue": previous_revenue,
        "revenueDelta": current_revenue - previous_revenue,
        "revenuePercentChange": (
            (current_revenue - previous_revenue) / previous_revenue
            if previous_revenue
            else None
        ),
    }


def assemble_snapshot(
    *,
    executive: dict[str, Any],
    trend: list[dict[str, Any]],
    sims: dict[str, Any],
    recruitment: dict[str, Any],
    momo: dict[str, Any],
    services: list[dict[str, Any]],
    hourly_revenue: list[dict[str, Any]],
    geographic_revenue: dict[str, Any],
    refresh_started_at: datetime,
    complaints: dict[str, Any] | None = None,
    services_trend: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    generated = datetime.now(timezone.utc)
    executive_date = iso_date(executive["date"])
    sim_date = date_key(sims["date"])
    recruitment_date = date_key(recruitment["date"])
    momo_date = iso_date(momo["date"])
    services_date = date_key(services[0]["date"] if services else geographic_revenue["date"])
    geographic_date = date_key(geographic_revenue["date"])
    reference_date = refresh_started_at.date()

    trend_rows = [
        {
            "date": iso_date(row["date"]),
            "revenue": finite_number(row["revenue"], "daily revenue"),
            "customers": int(row["customers"] or 0),
        }
        for row in trend
    ]
    daily_revenue = finite_number(executive["revenue"], "revenue")
    daily_transactions = int(executive["transactions"] or 0)
    unique_customers = int(executive["customers"] or 0)
    day_over_day = build_day_over_day(executive_date, daily_revenue, trend_rows)
    average_transaction = daily_revenue / daily_transactions if daily_transactions > 0 else None
    registration_by_day = {
        iso_date(row["date"]): int(row["count"] or 0)
        for row in recruitment["daily_counts"]
    }
    recruitment_iso = iso_date(recruitment["date"])
    reference = date.fromisoformat(recruitment_iso)
    week_start = reference.fromordinal(reference.toordinal() - 6).isoformat()
    month_start = reference.replace(day=1).isoformat()
    weekly_registrations = sum(
        amount for day, amount in registration_by_day.items()
        if week_start <= day <= reference.isoformat()
    )
    monthly_registrations = sum(
        amount for day, amount in registration_by_day.items()
        if month_start <= day <= reference.isoformat()
    )
    department_revenue: dict[str, float] = {}
    for row in geographic_revenue["rows"]:
        department = normalize_department(row["department"])
        department_revenue[department] = department_revenue.get(department, 0.0) + finite_number(
            row["revenue"], "department revenue"
        )
    normalized_services = [
        {
            "code": str(row["code"] or "(inconnu)"),
            "name": service_name(row["code"]),
            "activations": int(row["activations"] or 0),
            "clients": int(row["clients"] or 0),
            "amount": finite_number(row["amount"], "service amount"),
        }
        for row in services
    ]
    normalized_services.sort(key=lambda row: row["amount"], reverse=True)
    normalized_services = normalized_services[:12]
    if normalized_services:
        services_date = date_key(services[0]["date"])
    service_daily_by_date = {
        date_key(row["date"]): row for row in (services_trend or [])
    }
    service_trend_end = date.fromisoformat(iso_date(services_date))
    service_daily_trend = []
    for offset in range(6, -1, -1):
        day = (service_trend_end - timedelta(days=offset)).strftime("%Y%m%d")
        row = service_daily_by_date.get(day)
        service_daily_trend.append(
            {
                "date": iso_date(day),
                "activations": int(row["activations"] or 0) if row else None,
                "amount": finite_number(row["amount"], "daily service amount") if row else None,
            }
        )
    momo_operations = [
        {
            "type": str(row["operation"] or "Non renseigné"),
            "users": int(row["users"] or 0),
            "transactions": int(row["transactions"]),
            "volume": finite_number(row["volume"], "Mobile Money operation volume"),
            "fees": finite_number(row["fees"], "Mobile Money operation fees"),
        }
        for row in momo.get("operations", [])
    ]
    momo_operations.sort(key=lambda row: row["volume"], reverse=True)
    momo_trend_by_date = {
        iso_date(row["date"]): row
        for row in momo.get("trend", [])
    }
    momo_trend_end = date.fromisoformat(iso_date(momo_date))
    momo_trend = []
    for offset in range(6, -1, -1):
        day = (momo_trend_end - timedelta(days=offset)).isoformat()
        row = momo_trend_by_date.get(day)
        momo_trend.append(
            {
                "date": day,
                "users": int(row["users"] or 0) if row else None,
                "transactions": int(row["transactions"] or 0) if row else None,
                "amount": finite_number(row["amount"], "Mobile Money daily amount") if row else None,
                "fees": finite_number(row["fees"], "Mobile Money daily fees") if row else None,
            }
        )
    department_customers: dict[str, int] = {}
    for row in geographic_revenue["rows"]:
        department = normalize_department(row["department"])
        department_customers[department] = department_customers.get(department, 0) + int(
            row.get("customers") or 0
        )
    complaints_data = complaints or {}
    complaint_start = complaints_data.get("period_start")
    complaint_end = complaints_data.get("period_end")
    ticket_partition = complaints_data.get("ticket_partition")
    conversation_partition = complaints_data.get("conversation_partition")
    complaints_available = all(
        value is not None
        for value in (
            complaints_data.get("tickets"),
            complaints_data.get("ticket_clients"),
            complaints_data.get("conversations"),
            complaints_data.get("conversation_clients"),
            complaint_start,
            complaint_end,
            ticket_partition,
            conversation_partition,
        )
    )
    ticket_statuses = [
        {
            "status": str(row.get("status") or "NON RENSEIGNE"),
            "tickets": int(row.get("tickets") or 0),
        }
        for row in complaints_data.get("ticket_statuses", [])
    ]
    if ticket_statuses and sum(row["tickets"] for row in ticket_statuses) != int(
        complaints_data.get("tickets") or 0
    ):
        raise ValueError("Ticket status totals do not match the support ticket total")
    complaint_end_date = date.fromisoformat(iso_date(complaint_end)) if complaint_end else None
    complaint_start_date = date.fromisoformat(iso_date(complaint_start)) if complaint_start else None
    ticket_trend_by_date = {
        iso_date(row["date"]): int(row.get("tickets") or 0)
        for row in complaints_data.get("ticket_trend", [])
    }
    conversation_trend_by_date = {
        iso_date(row["date"]): int(row.get("conversations") or 0)
        for row in complaints_data.get("conversation_trend", [])
    }
    ticket_trend = []
    conversation_trend = []
    if complaint_start_date and complaint_end_date:
        for offset in range((complaint_end_date - complaint_start_date).days + 1):
            day = (complaint_start_date + timedelta(days=offset)).isoformat()
            ticket_trend.append(
                {"date": day, "count": ticket_trend_by_date.get(day)}
            )
            conversation_trend.append(
                {"date": day, "count": conversation_trend_by_date.get(day)}
            )
    for field, rows, expected in (
        ("tickets", ticket_trend_by_date, complaints_data.get("tickets")),
        ("conversations", conversation_trend_by_date, complaints_data.get("conversations")),
    ):
        if rows and expected is not None and sum(rows.values()) != int(expected):
            raise ValueError(f"Daily {field} totals do not match the support period total")

    modules = [
        {
            "module": "Executive",
            "table": "itbi_ess.prod_kpis_revenu",
            "date": executive_date,
            "state": freshness_state(executive_date, reference_date, "À jour · agrégat Presto"),
        },
        {
            "module": "Population SIM",
            "table": "feeds.sdp_dump_ma",
            "date": iso_date(sim_date),
            "state": freshness_state(sim_date, reference_date, "À jour · snapshot SIM"),
        },
        {
            "module": "Recruitment",
            "table": "feeds.irm_edit_registration_status",
            "date": iso_date(recruitment_date),
            "state": freshness_state(
                recruitment_date,
                reference_date,
                "À jour · lignes source, non dédupliquées en personnes",
            ),
        },
        {
            "module": "Mobile Money",
            "table": "mfs_bi.fact_momo_all",
            "date": momo_date,
            "state": freshness_state(momo_date, reference_date, "À jour · dernière partition"),
        },
        {
            "module": "RGS90",
            "table": "feeds.air_refill_ma",
            "date": None,
            "state": "Non disponible · le calcul distinct sur 90 jours dépasse la limite Presto (90 s)",
        },
        {
            "module": "Services",
            "table": "feeds.air_refill_ma",
            "date": iso_date(services_date),
            "state": freshness_state(
                services_date,
                reference_date,
                "Journée partielle · agrégat journalier"
                if services_date == reference_date.strftime("%Y%m%d")
                else "À jour · agrégat journalier",
            ),
        },
        {
            "module": "Geographic · revenus",
            "table": "itbi_ess.datamart_refill_loc_vw",
            "date": iso_date(geographic_date),
            "state": freshness_state(
                geographic_date,
                reference_date,
                "Journée partielle" if geographic_date == reference_date.strftime("%Y%m%d") else "Dernière partition disponible",
            ),
        },
        {
            "module": "Geographic · clientèle",
            "table": "itbi_ess.datamart_refill_loc_vw",
            "date": iso_date(geographic_date),
            "state": freshness_state(
                geographic_date,
                reference_date,
                "Journée partielle · distincts par département",
            ),
        },
        {
            "module": "Plaintes · tickets",
            "table": "feeds.trouble_ticket",
            "date": iso_date(ticket_partition) if ticket_partition is not None else "—",
            "state": freshness_state(
                ticket_partition,
                reference_date,
                "Fenêtre de 30 jours complets",
            ) if ticket_partition is not None else "Non disponible · aucune partition récente",
        },
        {
            "module": "Plaintes · YelloChat",
            "table": "feeds.yellochat_conversations",
            "date": iso_date(conversation_partition) if conversation_partition is not None else "—",
            "state": freshness_state(
                conversation_partition,
                reference_date,
                "Fenêtre de 30 jours complets",
            ) if conversation_partition is not None else "Non disponible · aucune partition récente",
        },
    ]
    return {
        "schema_version": 1,
        "generated_at": generated.isoformat(timespec="seconds"),
        "refresh_started_at": refresh_started_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "status": "fresh",
        "source_dates": {
            "executive": executive_date,
            "sims": iso_date(sim_date),
            "recruitment": iso_date(recruitment_date),
            "momo": momo_date,
            "services": iso_date(services_date),
            "geographic_revenue": iso_date(geographic_date),
            "geographic_customers": iso_date(geographic_date),
            "complaint_tickets": iso_date(ticket_partition) if ticket_partition is not None else None,
            "complaint_conversations": iso_date(conversation_partition) if conversation_partition is not None else None,
        },
        "executive": {
            "date": date_key(executive_date),
            "totalRevenue": daily_revenue,
            "totalTransactions": daily_transactions,
            "uniqueCustomers": unique_customers,
            "avgTransaction": average_transaction,
            "arpu": daily_revenue / unique_customers if unique_customers else None,
            "totalSims": int(sims["total"]),
            "activatedSims": int(sims["activated"]),
            "trend": trend_rows,
            "dayOverDay": day_over_day,
        },
        "realtime_hourly": build_hourly_trend(hourly_revenue, refresh_started_at),
        "recruitment": {
            "date": recruitment_date,
            "daily": registration_by_day.get(recruitment_iso, 0),
            "weekly": weekly_registrations,
            "monthly": monthly_registrations,
            "totalRegistrations": int(recruitment["registrations"]),
            "targetMonthly": None,
        },
        "momo": {
            "date": date_key(momo_date),
            "mau": int(momo["users"]),
            "transactions": int(momo["transactions"]),
            "volume": finite_number(momo["volume"], "Mobile Money volume"),
            "merchants": None,
            "operations": momo_operations,
            "trend": momo_trend,
            "avgTransaction": finite_number(momo["volume"], "Mobile Money volume") / int(momo["transactions"]) if int(momo["transactions"]) else None,
            "feeRate": None,
            "fees": finite_number(momo["fees"], "Mobile Money fees"),
            "txPerUser": int(momo["transactions"]) / int(momo["users"]) if int(momo["users"]) else None,
            "revenuePerUser": finite_number(momo["fees"], "Mobile Money fees") / int(momo["users"]) if int(momo["users"]) else None,
        },
        "geographic": {
            "date": geographic_date,
            "revenueDate": geographic_date,
            "customerDate": geographic_date,
            "revenuePartial": geographic_date == date.today().strftime("%Y%m%d"),
            "totalCustomers": int(geographic_revenue["total_customers"]) if geographic_revenue.get("total_customers") is not None else None,
            "customers": department_customers,
            "revenue": department_revenue,
            "customersFresh": geographic_revenue.get("total_customers") is not None and bool(department_customers),
        },
        "services": {
            "date": services_date,
            "partial": services_date == reference_date.strftime("%Y%m%d"),
            "items": normalized_services,
            "trend": service_daily_trend,
        },
        "complaints": {
            "date": complaint_end,
            "periodStart": complaint_start,
            "periodEnd": complaint_end,
            "tickets": int(complaints_data["tickets"]) if complaints_data.get("tickets") is not None else None,
            "ticketClients": int(complaints_data["ticket_clients"]) if complaints_data.get("ticket_clients") is not None else None,
            "ticketSourceDate": iso_date(ticket_partition) if ticket_partition is not None else None,
            "ticketStatuses": ticket_statuses,
            "ticketTrend": ticket_trend,
            "conversations": int(complaints_data["conversations"]) if complaints_data.get("conversations") is not None else None,
            "conversationClients": int(complaints_data["conversation_clients"]) if complaints_data.get("conversation_clients") is not None else None,
            "conversationSourceDate": iso_date(conversation_partition) if conversation_partition is not None else None,
            "conversationTrend": conversation_trend,
            "available": complaints_available,
        },
        "sources": modules,
    }


def save_snapshot(snapshot: dict[str, Any], destination: Path = SNAPSHOT_PATH) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=destination.parent, prefix=".marketing_metrics_", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Marketing snapshot has an unsupported schema")
    realtime = value.get("realtime_hourly")
    if isinstance(realtime, dict) and "hour_over_hour" not in realtime:
        hours = realtime.get("hours")
        if isinstance(hours, list) and len(hours) >= 2:
            current, previous = hours[-1], hours[-2]
            if isinstance(current, dict) and isinstance(previous, dict):
                realtime["hour_over_hour"] = build_hourly_comparison(
                    current, previous
                )
    return value
