from __future__ import annotations

from datetime import date, datetime, timedelta
import os
import re
import socket
import time
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.engine.url import URL

from app.config import presto_settings
from app.metrics import assemble_snapshot, date_key, save_snapshot


class RefreshError(RuntimeError):
    pass


# Une coupure entre la station et Presto (VPN, coordinateur en redémarrage) ne
# doit pas laisser l'interface rouge jusqu'au prochain cycle de 15 minutes : on
# distingue la panne réseau, réellement rejouable, des règles métier (partition
# absente, extrait vide) qui, elles, n'évolueront pas en quelques secondes.
_TRANSIENT_MESSAGE = re.compile(
    r"ConnectTimeout|ConnectionError|Connection refused|Connection reset|"
    r"Max retries exceeded|NameResolutionError|getaddrinfo|NewConnectionError|"
    r"ReadTimeout|Read timed out|ECONNREFUSED|ETIMEDOUT|EHOSTUNREACH|"
    r"ENETUNREACH|EAI_AGAIN|Temporary failure|handshake|SSLError",
    re.IGNORECASE,
)


def _transient_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [
        TimeoutError,
        ConnectionError,
        ConnectionResetError,
        ConnectionRefusedError,
        socket.timeout,
    ]
    try:
        from requests import exceptions as requests_exceptions
    except Exception:  # pragma: no cover - requests toujours présent en service
        return tuple(types)
    types.extend(
        [
            requests_exceptions.ConnectTimeout,
            requests_exceptions.ConnectionError,
            requests_exceptions.ReadTimeout,
            requests_exceptions.Timeout,
        ]
    )
    return tuple(types)


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        following = current.__cause__ or current.__context__
        if following is None:
            following = getattr(current, "orig", None)
        current = following
    return chain


def is_transient_network_error(error: BaseException | None) -> bool:
    """Vrai si l'échec vient du réseau (à relancer), faux s'il vient des données."""
    if error is None:
        return False
    transient_types = _transient_types()
    for item in _exception_chain(error):
        if isinstance(item, transient_types):
            return True
    return bool(_TRANSIENT_MESSAGE.search(str(error)))


def _non_negative_int(raw: str | None, default: int) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default

class MarketingRefresh:
    def __init__(self) -> None:
        self.settings = presto_settings()
        self.engine = None

    def connect(self):
        settings = self.settings
        password = settings["password"]
        url = URL.create(
            "presto",
            username=str(settings["username"]),
            password=str(password) if password else None,
            host=str(settings["host"]),
            port=int(settings["port"]),
            database=f"{settings['catalog']}/{settings['schema']}",
            query={"protocol": str(settings["protocol"])},
        )
        self.engine = create_engine(
            url,
            connect_args={
                "requests_kwargs": {
                    "verify": settings["tls_verify"],
                    "timeout": (8, 120),
                },
                "session_props": {"query_max_run_time": str(settings["query_timeout"])},
            },
            pool_pre_ping=True,
        )
        return self.engine.connect()

    @staticmethod
    def query(connection: Connection, statement: str) -> list[dict[str, Any]]:
        try:
            result = connection.execute(text(statement))
        except Exception as error:
            source = re.search(r"\bFROM\s+([A-Za-z0-9_.]+)", statement, re.IGNORECASE)
            source_name = source.group(1) if source else "source inconnue"
            raise RefreshError(
                f"Échec d'une requête Presto sur {source_name} "
                f"({type(error).__name__}: {str(error).splitlines()[0][:160]})."
            ) from error
        return [dict(row._mapping) for row in result.fetchall()]

    @staticmethod
    def partition_candidates(today: date, days: int = 14) -> str:
        return ", ".join(
            str(int((today - timedelta(days=offset)).strftime("%Y%m%d")))
            for offset in range(days + 1)
        )

    def refresh(self) -> dict[str, Any]:
        started = datetime.now().astimezone()
        connection = None
        try:
            connection = self.connect()
            today = date.today()
            cutoff = today - timedelta(days=14)
            date_candidates = self.partition_candidates(today, 14)
            revenue_dates = self.query(
                connection,
                "SELECT MAX(date_key) AS date FROM itbi_ess.prod_kpis_revenu "
                f"WHERE date_key >= DATE '{cutoff.isoformat()}'",
            )
            if not revenue_dates or revenue_dates[0]["date"] is None:
                raise RefreshError("Aucune partition de revenu récente disponible dans Presto.")
            revenue_date = datetime.strptime(
                date_key(revenue_dates[0]["date"]), "%Y%m%d"
            ).date()
            revenue_iso = revenue_date.isoformat()
            trend_start = (revenue_date - timedelta(days=6)).isoformat()
            revenue_rows = self.query(
                connection,
                "SELECT date_key AS date, "
                "SUM(CAST(amount AS DOUBLE)) AS revenue, COUNT(*) AS transactions, "
                "COUNT(DISTINCT msisdn) AS customers "
                "FROM itbi_ess.prod_kpis_revenu "
                f"WHERE date_key BETWEEN DATE '{trend_start}' AND DATE '{revenue_iso}' "
                "GROUP BY date_key ORDER BY date_key",
            )
            if not revenue_rows:
                raise RefreshError("La partition récente de revenu est vide.")
            executive_row = next(
                (row for row in revenue_rows if date_key(row["date"]) == revenue_iso.replace("-", "")),
                None,
            )
            if executive_row is None:
                raise RefreshError("La journée la plus récente est absente de la série de revenu.")

            sim_partitions = self.query(
                connection,
                "SELECT MAX(tbl_dt) AS date FROM feeds.sdp_dump_ma "
                f"WHERE tbl_dt IN ({date_candidates})",
            )
            if not sim_partitions or sim_partitions[0]["date"] is None:
                raise RefreshError("Aucun snapshot SIM récent disponible dans Presto.")
            sim_date = date_key(sim_partitions[0]["date"])
            sim_rows = self.query(
                connection,
                "SELECT COUNT(*) AS total, "
                "SUM(CASE WHEN account_activated_flag = '1' THEN 1 ELSE 0 END) AS activated "
                "FROM feeds.sdp_dump_ma "
                f"WHERE tbl_dt = {int(sim_date)}",
            )
            if not sim_rows or int(sim_rows[0]["total"] or 0) == 0:
                raise RefreshError("Le snapshot SIM le plus récent ne contient aucune ligne.")

            registration_partitions = self.query(
                connection,
                "SELECT MAX(tbl_dt) AS date FROM feeds.irm_edit_registration_status",
            )
            if not registration_partitions or registration_partitions[0]["date"] is None:
                raise RefreshError("Aucun snapshot d'inscription récent disponible dans Presto.")
            recruitment_date = date_key(registration_partitions[0]["date"])
            registration_start = (
                datetime.strptime(recruitment_date, "%Y%m%d").date() - timedelta(days=30)
            ).isoformat()
            registration_end = (
                datetime.strptime(recruitment_date, "%Y%m%d").date() + timedelta(days=1)
            ).isoformat()
            registration_rows = self.query(
                connection,
                "SELECT CAST(CAST(registration_date_time AS TIMESTAMP) AS DATE) AS date, "
                "COUNT(*) AS count "
                "FROM feeds.irm_edit_registration_status "
                f"WHERE tbl_dt = {int(recruitment_date)} "
                "AND trx_status_name = 'SUCCESS' "
                f"AND CAST(registration_date_time AS TIMESTAMP) >= TIMESTAMP '{registration_start}' "
                f"AND CAST(registration_date_time AS TIMESTAMP) < TIMESTAMP '{registration_end}' "
                "GROUP BY CAST(CAST(registration_date_time AS TIMESTAMP) AS DATE)",
            )
            registration_totals = self.query(
                connection,
                "SELECT COUNT(*) AS registrations "
                "FROM feeds.irm_edit_registration_status "
                f"WHERE tbl_dt = {int(recruitment_date)}",
            )

            momo_cutoff = (today - timedelta(days=14)).isoformat()
            momo_partitions = self.query(
                connection,
                "SELECT MAX(date_key) AS date FROM mfs_bi.fact_momo_all "
                f"WHERE date_key >= DATE '{momo_cutoff}'",
            )
            if not momo_partitions or momo_partitions[0]["date"] is None:
                raise RefreshError("Aucune partition Mobile Money récente disponible dans Presto.")
            momo_date = datetime.strptime(
                date_key(momo_partitions[0]["date"]), "%Y%m%d"
            ).date()
            momo_rows = self.query(
                connection,
                "SELECT SUM(CAST(value AS DOUBLE)) AS volume, "
                "SUM(CAST(volume AS DOUBLE)) AS transactions, "
                "SUM(CAST(fees AS DOUBLE)) AS fees, COUNT(DISTINCT msisdn) AS users "
                "FROM mfs_bi.fact_momo_all "
                f"WHERE date_key = DATE '{momo_date.isoformat()}'",
            )
            if not momo_rows or int(momo_rows[0]["users"] or 0) == 0:
                raise RefreshError("La dernière partition Mobile Money ne contient aucune activité.")
            momo_operations = self.query(
                connection,
                "SELECT kpi AS operation, "
                "COUNT(DISTINCT msisdn) AS users, "
                "SUM(CAST(volume AS DOUBLE)) AS transactions, "
                "SUM(CAST(value AS DOUBLE)) AS volume, "
                "SUM(CAST(fees AS DOUBLE)) AS fees "
                "FROM mfs_bi.fact_momo_all "
                f"WHERE date_key = DATE '{momo_date.isoformat()}' "
                "GROUP BY kpi ORDER BY volume DESC",
            )
            momo_trend_start = (momo_date - timedelta(days=6)).isoformat()
            momo_trend_rows = self.query(
                connection,
                "SELECT date_key AS date, "
                "COUNT(DISTINCT msisdn) AS users, "
                "SUM(CAST(volume AS DOUBLE)) AS transactions, "
                "SUM(CAST(value AS DOUBLE)) AS amount, "
                "SUM(CAST(fees AS DOUBLE)) AS fees "
                "FROM mfs_bi.fact_momo_all "
                f"WHERE date_key BETWEEN DATE '{momo_trend_start}' "
                f"AND DATE '{momo_date.isoformat()}' "
                "GROUP BY date_key ORDER BY date_key",
            )

            air_partitions = self.query(
                connection,
                "SELECT MAX(tbl_dt) AS date FROM feeds.air_refill_ma "
                f"WHERE tbl_dt IN ({self.partition_candidates(today, 2)})",
            )
            if not air_partitions or air_partitions[0]["date"] is None:
                raise RefreshError("Aucune partition d'offres et services récente disponible dans Presto.")
            air_date = date_key(air_partitions[0]["date"])
            services = self.query(
                connection,
                "SELECT transaction_type_cd AS code, COUNT(*) AS activations, "
                "APPROX_DISTINCT(subscriber_nr) AS clients, "
                "SUM(CAST(transaction_amt AS DOUBLE)) AS amount "
                "FROM feeds.air_refill_ma "
                f"WHERE tbl_dt = {int(air_date)} "
                "AND CAST(transaction_amt AS DOUBLE) >= 100 "
                "GROUP BY transaction_type_cd "
                "ORDER BY amount DESC LIMIT 24",
            )
            services = [dict(row, date=air_date) for row in services]
            service_trend_start = (
                datetime.strptime(air_date, "%Y%m%d").date() - timedelta(days=6)
            )
            service_trend_partitions = ", ".join(
                str(int((service_trend_start + timedelta(days=offset)).strftime("%Y%m%d")))
                for offset in range(7)
            )
            service_trend = self.query(
                connection,
                "SELECT tbl_dt AS date, COUNT(*) AS activations, "
                "SUM(CAST(transaction_amt AS DOUBLE)) AS amount "
                "FROM feeds.air_refill_ma "
                f"WHERE tbl_dt IN ({service_trend_partitions}) "
                "AND CAST(transaction_amt AS DOUBLE) >= 100 "
                "GROUP BY tbl_dt ORDER BY tbl_dt",
            )

            hourly_partitions = ", ".join(
                str(int((today - timedelta(days=offset)).strftime("%Y%m%d")))
                for offset in range(3)
            )
            hourly_rows = self.query(
                connection,
                "SELECT CAST(event_dt AS DATE) AS date, hour(event_dt) AS hour, "
                "SUM(CAST(transaction_amt AS DOUBLE)) AS revenue, COUNT(*) AS transactions "
                "FROM feeds.air_refill_ma "
                f"WHERE tbl_dt IN ({hourly_partitions}) "
                "GROUP BY CAST(event_dt AS DATE), hour(event_dt) "
                "ORDER BY CAST(event_dt AS DATE), hour(event_dt)",
            )

            geographic_partitions = self.partition_candidates(today, 2)
            geographic_rows = self.query(
                connection,
                "SELECT tbl_dt AS date, "
                "COALESCE(NULLIF(UPPER(TRIM(depatement)), ''), 'NON RENSEIGNE') AS department, "
                "SUM(CAST(amount AS DOUBLE)) AS revenue, "
                "COUNT(DISTINCT msisdn_key) AS customers "
                "FROM itbi_ess.datamart_refill_loc_vw "
                f"WHERE tbl_dt IN ({geographic_partitions}) "
                "GROUP BY tbl_dt, "
                "COALESCE(NULLIF(UPPER(TRIM(depatement)), ''), 'NON RENSEIGNE')",
            )
            if not geographic_rows:
                raise RefreshError("Aucune partition récente de revenu géographique disponible.")
            geographic_date = max(date_key(row["date"]) for row in geographic_rows)
            geographic_rows = [
                row for row in geographic_rows
                if date_key(row["date"]) == geographic_date
            ]
            geographic_customers = self.query(
                connection,
                "SELECT COUNT(DISTINCT msisdn_key) AS customers "
                "FROM itbi_ess.datamart_refill_loc_vw "
                f"WHERE tbl_dt = {int(geographic_date)} AND msisdn_key IS NOT NULL",
            )

            support_start = today - timedelta(days=30)
            support_end = today - timedelta(days=1)
            support_start_key = int(support_start.strftime("%Y%m%d"))
            support_end_key = int(support_end.strftime("%Y%m%d"))
            support_tickets = self.query(
                connection,
                "SELECT COUNT(*) AS tickets, COUNT(DISTINCT customerid) AS clients, "
                "MAX(tbl_dt) AS source_partition "
                "FROM feeds.trouble_ticket "
                f"WHERE tbl_dt BETWEEN {support_start_key} AND {support_end_key} "
                "AND createddate <> '' "
                f"AND TRY_CAST(FROM_ISO8601_TIMESTAMP(createddate) AS DATE) >= DATE '{support_start.isoformat()}' "
                f"AND TRY_CAST(FROM_ISO8601_TIMESTAMP(createddate) AS DATE) < DATE '{today.isoformat()}'",
            )
            support_ticket_statuses = self.query(
                connection,
                "SELECT COALESCE(NULLIF(TRIM(status), ''), 'NON RENSEIGNE') AS status, "
                "COUNT(*) AS tickets "
                "FROM feeds.trouble_ticket "
                f"WHERE tbl_dt BETWEEN {support_start_key} AND {support_end_key} "
                "AND createddate <> '' "
                f"AND TRY_CAST(FROM_ISO8601_TIMESTAMP(createddate) AS DATE) >= DATE '{support_start.isoformat()}' "
                f"AND TRY_CAST(FROM_ISO8601_TIMESTAMP(createddate) AS DATE) < DATE '{today.isoformat()}' "
                "GROUP BY COALESCE(NULLIF(TRIM(status), ''), 'NON RENSEIGNE') "
                "ORDER BY tickets DESC",
            )
            support_ticket_trend = self.query(
                connection,
                "SELECT TRY_CAST(FROM_ISO8601_TIMESTAMP(createddate) AS DATE) AS date, "
                "COUNT(*) AS tickets "
                "FROM feeds.trouble_ticket "
                f"WHERE tbl_dt BETWEEN {support_start_key} AND {support_end_key} "
                "AND createddate <> '' "
                f"AND TRY_CAST(FROM_ISO8601_TIMESTAMP(createddate) AS DATE) >= DATE '{support_start.isoformat()}' "
                f"AND TRY_CAST(FROM_ISO8601_TIMESTAMP(createddate) AS DATE) < DATE '{today.isoformat()}' "
                "GROUP BY TRY_CAST(FROM_ISO8601_TIMESTAMP(createddate) AS DATE) "
                "ORDER BY date",
            )
            support_conversations = self.query(
                connection,
                "SELECT COUNT(*) AS conversations, COUNT(DISTINCT msisdn) AS clients, "
                "MAX(tbl_dt) AS source_partition "
                "FROM feeds.yellochat_conversations "
                f"WHERE tbl_dt BETWEEN {support_start_key} AND {support_end_key} "
                "AND TRY_CAST(created_date AS BIGINT) IS NOT NULL "
                f"AND CAST(FROM_UNIXTIME(CAST(created_date AS BIGINT) / 1000) AS DATE) >= DATE '{support_start.isoformat()}' "
                f"AND CAST(FROM_UNIXTIME(CAST(created_date AS BIGINT) / 1000) AS DATE) < DATE '{today.isoformat()}'",
            )
            support_conversation_trend = self.query(
                connection,
                "SELECT CAST(FROM_UNIXTIME(CAST(created_date AS BIGINT) / 1000) AS DATE) AS date, "
                "COUNT(*) AS conversations "
                "FROM feeds.yellochat_conversations "
                f"WHERE tbl_dt BETWEEN {support_start_key} AND {support_end_key} "
                "AND TRY_CAST(created_date AS BIGINT) IS NOT NULL "
                f"AND CAST(FROM_UNIXTIME(CAST(created_date AS BIGINT) / 1000) AS DATE) >= DATE '{support_start.isoformat()}' "
                f"AND CAST(FROM_UNIXTIME(CAST(created_date AS BIGINT) / 1000) AS DATE) < DATE '{today.isoformat()}' "
                "GROUP BY CAST(FROM_UNIXTIME(CAST(created_date AS BIGINT) / 1000) AS DATE) "
                "ORDER BY date",
            )

            snapshot = assemble_snapshot(
                executive=executive_row,
                trend=[
                    {
                        "date": row["date"],
                        "revenue": row["revenue"],
                        "customers": row["customers"],
                    }
                    for row in revenue_rows
                ],
                sims={
                    "date": sim_date,
                    "total": sim_rows[0]["total"],
                    "activated": sim_rows[0]["activated"] or 0,
                },
                recruitment={
                    "date": recruitment_date,
                    "daily_counts": registration_rows,
                    "registrations": registration_totals[0]["registrations"],
                },
                momo={
                    "date": momo_date,
                    "users": momo_rows[0]["users"],
                    "transactions": momo_rows[0]["transactions"],
                    "volume": momo_rows[0]["volume"],
                    "fees": momo_rows[0]["fees"],
                    "operations": momo_operations,
                    "trend": momo_trend_rows,
                },
                services=services,
                services_trend=service_trend,
                hourly_revenue=hourly_rows,
                geographic_revenue={
                    "date": geographic_date,
                    "rows": geographic_rows,
                    "total_customers": geographic_customers[0]["customers"],
                },
                complaints={
                    "period_start": support_start.isoformat(),
                    "period_end": support_end.isoformat(),
                    "tickets": support_tickets[0]["tickets"],
                    "ticket_clients": support_tickets[0]["clients"],
                    "ticket_partition": support_tickets[0]["source_partition"],
                    "ticket_statuses": support_ticket_statuses,
                    "ticket_trend": support_ticket_trend,
                    "conversations": support_conversations[0]["conversations"],
                    "conversation_clients": support_conversations[0]["clients"],
                    "conversation_partition": support_conversations[0]["source_partition"],
                    "conversation_trend": support_conversation_trend,
                },
                refresh_started_at=started,
            )
            save_snapshot(snapshot)
            return snapshot
        except RefreshError:
            raise
        except Exception as error:
            raise RefreshError(
                f"Rafraîchissement Presto impossible ({type(error).__name__}: "
                f"{str(error).splitlines()[0][:240]}). "
                "La dernière extraction valide a été conservée."
            ) from error
        finally:
            if connection is not None:
                connection.close()
            if self.engine is not None:
                self.engine.dispose()


def refresh_marketing_data() -> dict[str, Any]:
    """Extraction Presto avec relance limitée aux pannes réseau.

    `MTN_PRESTO_RETRIES` relances (2 par défaut) espacées de
    `MTN_PRESTO_RETRY_DELAY` × essai secondes (5 s par défaut). Un échec
    métier sort immédiatement, sans attente : le dernier snapshot valide reste
    en place dans tous les cas.
    """
    attempts = 1 + _non_negative_int(os.environ.get("MTN_PRESTO_RETRIES"), 2)
    try:
        delay = float(os.environ.get("MTN_PRESTO_RETRY_DELAY") or 5)
    except ValueError:
        delay = 5.0
    delay = max(0.0, delay)
    last_error: RefreshError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return MarketingRefresh().refresh()
        except RefreshError as error:
            last_error = error
            if attempt >= attempts or not is_transient_network_error(error):
                raise
            reason = str(error).splitlines()[0][:160]
            print(
                f"Actualisation Presto : tentative {attempt}/{attempts} échouée "
                f"({reason}) — nouvelle tentative dans {delay * attempt:g} s",
                flush=True,
            )
            time.sleep(delay * attempt)
    raise last_error if last_error else RefreshError("Actualisation Presto impossible.")
