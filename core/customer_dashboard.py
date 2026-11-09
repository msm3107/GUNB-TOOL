"""Bounded customer-owned projections and feedback; no HTTP, SDK or migrations."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime

from .checkout_store import CheckoutStore, CheckoutUnavailable
from .instance import AlreadyRunning, database_replacement_lock
from .issue_tracker import IssueNotAllowed, IssueTracker
from .stages import get_trade
from .storage import SCHEMA_VERSION
from .teryt import VOIVODESHIPS

PAGE_SIZE = 20


@dataclass(frozen=True)
class DashboardOrder:
    order_id: int
    kind: str
    payment: str
    scope: str


@dataclass(frozen=True)
class DashboardDelivery:
    delivery_id: int
    order_id: int
    delivered_at: str | None
    new_count: int
    change_count: int
    full_count: int
    formats: tuple[str, ...] = ()
    published_at: str | None = None


@dataclass(frozen=True)
class DashboardSubscription:
    order_id: int
    status: str
    period_end: int
    cancel_at_period_end: bool


@dataclass(frozen=True)
class DashboardSnapshot:
    email: str
    company: str
    billing_linked: bool
    orders: tuple[DashboardOrder, ...]
    deliveries: tuple[DashboardDelivery, ...]
    subscriptions: tuple[DashboardSubscription, ...]
    orders_next: int | None
    deliveries_next: int | None
    subscriptions_more: bool


def _scope(raw: str) -> str:
    try:
        value = json.loads(raw)
        voivodeship = next(
            v.name for v in VOIVODESHIPS if v.code == value.get("wojewodztwo")
        )
        trade = get_trade(value.get("branza"))
        if trade is None:
            raise ValueError
        counties = value.get("powiaty", [])
        radius = value.get("promien_km")
        detail = (
            f", powiaty: {len(counties)}"
            if isinstance(counties, list) and counties
            else ""
        )
        if type(radius) in (int, float) and 1 <= radius <= 200:
            detail += f", promień {radius:g} km"
        return f"{voivodeship} · {trade.label}{detail}"
    except (ValueError, TypeError, AttributeError, StopIteration, RecursionError):
        return "Zakres zapisany w zamówieniu"


@dataclass
class _ReportTransaction:
    """IssueTracker port inside CheckoutStore's already-open write transaction."""

    connection: sqlite3.Connection
    timestamp: datetime

    def now(self) -> datetime:
        return self.timestamp

    def transaction(self):
        return nullcontext()


class CustomerDashboardStore(CheckoutStore):
    def __init__(self, path, *, query_seconds: float = 0.5):
        super().__init__(path)
        self.query_seconds = query_seconds
        self._slots = threading.BoundedSemaphore(4)

    def snapshot(
        self,
        customer_id: int,
        *,
        now: datetime,
        orders_before: int | None = None,
        deliveries_before: int | None = None,
    ) -> DashboardSnapshot:
        for value in (customer_id, orders_before, deliveries_before):
            if value is not None and (type(value) is not int or not 1 <= value < 2**63):
                raise ValueError("Niepoprawny identyfikator")
        if self.path is None or not self._slots.acquire(blocking=False):
            raise CheckoutUnavailable
        try:
            path = self.path.resolve(strict=True)
            with database_replacement_lock(path):
                conn = sqlite3.connect(
                    path.as_uri() + "?mode=ro",
                    uri=True,
                    timeout=0.05,
                    isolation_level=None,
                )
                try:
                    conn.row_factory = sqlite3.Row
                    deadline = time.monotonic() + self.query_seconds
                    conn.set_progress_handler(
                        lambda: int(time.monotonic() >= deadline), 1000
                    )
                    conn.execute("PRAGMA query_only=ON")
                    conn.execute("BEGIN")
                    if (
                        conn.execute("PRAGMA user_version").fetchone()[0]
                        != SCHEMA_VERSION
                        or conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
                    ):
                        raise CheckoutUnavailable
                    return self._snapshot(
                        conn, customer_id, now, orders_before, deliveries_before
                    )
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, AlreadyRunning):
            raise CheckoutUnavailable from None
        finally:
            self._slots.release()

    @staticmethod
    def _snapshot(conn, customer_id, now, orders_before, deliveries_before):
        customer = conn.execute(
            "SELECT email,nazwa_firmy,stripe_customer_id FROM b2b_customers WHERE customer_id=?",
            (customer_id,),
        ).fetchone()
        if customer is None:
            raise CheckoutUnavailable
        orders = conn.execute(
            """SELECT order_id,typ_pakietu,status_platnosci,substr(filtry_json,1,4096) AS filters
            FROM b2b_orders WHERE customer_id=? AND order_id<=? ORDER BY order_id DESC LIMIT ?""",
            (
                customer_id,
                orders_before - 1 if orders_before else 2**63 - 1,
                PAGE_SIZE + 1,
            ),
        ).fetchall()
        # Limit deliveries BEFORE joining their immutable items, never aggregate other tenants.
        deliveries = conn.execute(
            """WITH page AS MATERIALIZED (
                SELECT d.delivery_id,d.order_id,d.doreczono_utc,o.status_platnosci,pub.published_at FROM b2b_orders o
                JOIN b2b_deliveries d USING(order_id)
                LEFT JOIN package_publications pub USING(delivery_id)
                WHERE o.customer_id=? AND d.delivery_id<=? ORDER BY d.delivery_id DESC LIMIT ?)
            SELECT p.*,COALESCE(SUM(i.kategoria_dostawy='new'),0) AS new_count,
                COALESCE(SUM(i.kategoria_dostawy='change'),0) AS change_count,
                COALESCE(SUM(i.kategoria_dostawy='full'),0) AS full_count,
                CASE WHEN p.status_platnosci='paid' AND (p.doreczono_utc IS NOT NULL OR p.published_at IS NOT NULL)
                THEN (SELECT group_concat(a.format,',') FROM package_artifacts a
                    JOIN b2b_deliveries original USING(delivery_id)
                    WHERE a.delivery_id=p.delivery_id AND a.relative_name=CASE a.format WHEN 'xlsx' THEN original.plik_xlsx ELSE original.plik_csv END)
                END AS formats
            FROM page p LEFT JOIN b2b_delivery_items i USING(delivery_id)
            GROUP BY p.delivery_id ORDER BY p.delivery_id DESC""",
            (
                customer_id,
                deliveries_before - 1 if deliveries_before else 2**63 - 1,
                PAGE_SIZE + 1,
            ),
        ).fetchall()
        subscriptions = conn.execute(
            """SELECT s.order_id,s.status,s.current_period_end,s.cancel_at_period_end,
                s.paused,s.terminal,o.status_platnosci
            FROM b2b_subscriptions s JOIN b2b_customers c USING(customer_id)
            JOIN b2b_orders o ON o.order_id=s.order_id AND o.customer_id=s.customer_id
            JOIN checkout_billing_links l ON l.order_id=s.order_id
                AND l.stripe_customer_id=s.stripe_customer_id AND l.subscription_id=s.subscription_id
            JOIN checkout_attempts a ON a.order_id=s.order_id AND a.key_hash=s.checkout_key
            WHERE s.customer_id=? AND s.stripe_customer_id=c.stripe_customer_id
            ORDER BY s.order_id DESC LIMIT ?""",
            (customer_id, PAGE_SIZE + 1),
        ).fetchall()
        states = []
        for row in subscriptions[:PAGE_SIZE]:
            status = row["status"]
            if row["terminal"]:
                status = "canceled"
            elif row["status_platnosci"] != "paid":
                status = "unconfirmed"
            elif row["paused"]:
                status = "paused"
            elif status == "active" and row["current_period_end"] <= int(
                now.timestamp()
            ):
                status = "expired"
            states.append(
                DashboardSubscription(
                    row["order_id"],
                    status,
                    row["current_period_end"],
                    bool(row["cancel_at_period_end"]),
                )
            )
        return DashboardSnapshot(
            customer["email"],
            customer["nazwa_firmy"] or "",
            customer["stripe_customer_id"] is not None,
            tuple(
                DashboardOrder(
                    r["order_id"],
                    r["typ_pakietu"],
                    r["status_platnosci"],
                    _scope(r["filters"]),
                )
                for r in orders[:PAGE_SIZE]
            ),
            tuple(
                DashboardDelivery(
                    r["delivery_id"],
                    r["order_id"],
                    r["doreczono_utc"],
                    r["new_count"],
                    r["change_count"],
                    r["full_count"],
                    tuple(r["formats"].split(',')) if r["formats"] else (),
                    r["published_at"],
                )
                for r in deliveries[:PAGE_SIZE]
            ),
            tuple(states),
            orders[PAGE_SIZE - 1]["order_id"] if len(orders) > PAGE_SIZE else None,
            deliveries[PAGE_SIZE - 1]["delivery_id"]
            if len(deliveries) > PAGE_SIZE
            else None,
            len(subscriptions) > PAGE_SIZE,
        )

    def report_issue(
        self,
        customer_id: int,
        case_id: str,
        kind: str,
        description: str,
        *,
        now: datetime,
        delivery_id: int | None = None,
        artifact_sha256: str | None = None,
    ):
        def report(conn):
            try:
                return IssueTracker(_ReportTransaction(conn, now)).report_issue(
                    customer_id, case_id, kind, description,
                    delivery_id=delivery_id, artifact_sha256=artifact_sha256,
                )
            except IssueNotAllowed:
                # Its savepoint already rolled back; keep PermissionError out of
                # CheckoutStore's OS-error translation and preserve the denial.
                return None

        result = self._write(report)
        if result is None:
            raise IssueNotAllowed("Sprawa nie została doręczona temu klientowi")
        return result
