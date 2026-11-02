"""Durable test-payment receipt and preparation queue, without Stripe or HTTP."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from .checkout_store import CheckoutConflict, CheckoutStore
from .delivery_memory import ClassifiedDeliveryItem


@dataclass(frozen=True)
class PaidCheckout:
    key_hash: str
    session_id: str
    order_id: str
    customer_id: str
    mode: str
    currency: str
    amount_total: int
    session_created: int


@dataclass(frozen=True)
class PackageJob:
    order_id: int
    checkout_key: str
    token: str
    lease_until: int
    filters_json: str


class PaymentStore(CheckoutStore):
    def record_event(
        self,
        *,
        event_id: str,
        event_type: str,
        completion: PaidCheckout | None,
        now: datetime,
    ) -> int | None:
        """ACK only committed receipts; return an order only for a new job."""

        def record(conn):
            if conn.execute(
                "SELECT 1 FROM stripe_events WHERE event_id=?", (event_id,)
            ).fetchone():
                return None
            order_id, outcome, new_job = None, "ignored", False
            if completion is not None:
                p = completion
                row = conn.execute(
                    """SELECT a.*,o.status_platnosci,o.filtry_json,o.typ_pakietu,
                    o.customer_id AS order_customer FROM checkout_attempts a
                    JOIN b2b_orders o USING(order_id) WHERE a.key_hash=?""",
                    (p.key_hash,),
                ).fetchone()
                if row is None:
                    raise CheckoutConflict
                offer = json.loads(row["offer_json"])
                if (
                    p.order_id != str(row["order_id"])
                    or p.customer_id != str(row["customer_id"])
                    or row["customer_id"] != row["order_customer"]
                    or row["filters_json"] != row["filtry_json"]
                    or row["kind"] != row["typ_pakietu"]
                    or p.mode
                    != ("payment" if row["kind"] == "jednorazowy" else "subscription")
                    or p.currency != "pln"
                    or type(p.amount_total) is not int
                    or p.amount_total != offer["amount_grosze"]
                    or type(p.session_created) is not int
                    or not row["created_at"] - 300
                    <= p.session_created
                    < row["expires_at"]
                    or row["session_id"] not in (None, p.session_id)
                    or row["status_platnosci"] not in ("pending", "paid")
                ):
                    raise CheckoutConflict
                job = conn.execute(
                    "SELECT session_id FROM paid_package_jobs WHERE order_id=?",
                    (row["order_id"],),
                ).fetchone()
                if job is not None and job["session_id"] != p.session_id:
                    raise CheckoutConflict
                if job is None and row["status_platnosci"] != "pending":
                    raise CheckoutConflict
                order_id = row["order_id"]
                new_job = job is None
                outcome = "paid" if new_job else "duplicate"
                if new_job:
                    conn.execute(
                        "UPDATE b2b_orders SET status_platnosci='paid' WHERE order_id=?",
                        (order_id,),
                    )
                    conn.execute(
                        """INSERT INTO paid_package_jobs(order_id,checkout_key,session_id,created_at)
                        VALUES(?,?,?,?)""",
                        (order_id, p.key_hash, p.session_id, int(now.timestamp())),
                    )
            conn.execute(
                "INSERT INTO stripe_events(event_id,event_type,received_at,outcome,order_id) VALUES(?,?,?,?,?)",
                (event_id, event_type, int(now.timestamp()), outcome, order_id),
            )
            return order_id if new_job else None

        return self._write(record)

    def pending_packages(self, *, now: datetime, limit: int) -> tuple[int, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        return self._write(
            lambda conn: tuple(
                row[0]
                for row in conn.execute(
                    """SELECT order_id
            FROM paid_package_jobs WHERE state='pending' OR (state='building' AND lease_until<=?)
            ORDER BY created_at,order_id LIMIT ?""",
                    (int(now.timestamp()), limit),
                )
            )
        )

    def claim_package(self, order_id: int, *, now: datetime) -> PackageJob | None:
        stamp, token = int(now.timestamp()), uuid4().hex

        def claim(conn):
            row = conn.execute(
                """SELECT j.checkout_key,a.filters_json FROM paid_package_jobs j
                JOIN checkout_attempts a ON a.key_hash=j.checkout_key
                JOIN b2b_orders o ON o.order_id=j.order_id
                WHERE j.order_id=? AND o.status_platnosci='paid'
                  AND a.order_id=o.order_id AND a.customer_id=o.customer_id
                  AND a.filters_json=o.filtry_json AND a.kind=o.typ_pakietu
                  AND (j.state='pending' OR (j.state='building' AND j.lease_until<=?))""",
                (order_id, stamp),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE paid_package_jobs SET state='building',lease_token=?,lease_until=?,error_code=NULL
                WHERE order_id=?""",
                (token, stamp + 600, order_id),
            )
            return PackageJob(
                order_id, row["checkout_key"], token, stamp + 600, row["filters_json"]
            )

        return self._write(claim)

    def fail_package(self, job: PackageJob, code: str) -> None:
        self._write(
            lambda conn: conn.execute(
                """UPDATE paid_package_jobs
            SET state='pending',lease_token=NULL,lease_until=NULL,error_code=?
            WHERE order_id=? AND state='building' AND lease_token=?""",
                (code, job.order_id, job.token),
            )
        )

    def finish_package(
        self,
        job: PackageJob,
        items: Sequence[ClassifiedDeliveryItem],
        xlsx: str,
        csv: str,
        *,
        now: datetime,
    ) -> bool:
        def finish(conn):
            current = conn.execute(
                """SELECT 1 FROM paid_package_jobs j JOIN b2b_orders o USING(order_id)
                JOIN checkout_attempts a ON a.key_hash=j.checkout_key WHERE j.order_id=?
                  AND j.checkout_key=? AND j.state='building' AND j.lease_token=? AND j.lease_until>?
                  AND o.status_platnosci='paid' AND o.filtry_json=? AND a.filters_json=o.filtry_json
                  AND a.order_id=o.order_id AND a.customer_id=o.customer_id AND a.kind=o.typ_pakietu""",
                (
                    job.order_id,
                    job.checkout_key,
                    job.token,
                    int(now.timestamp()),
                    job.filters_json,
                ),
            ).fetchone()
            if current is None:
                return False
            delivery_id = conn.execute(
                """INSERT INTO b2b_deliveries(order_id,typ_dostawy,plik_xlsx,plik_csv)
                VALUES(?,'delta',?,?)""",
                (job.order_id, xlsx, csv),
            ).lastrowid
            conn.executemany(
                "INSERT INTO b2b_delivery_items VALUES(?,?,?,?)",
                (
                    (
                        delivery_id,
                        item.investment.id_sprawy,
                        item.revision,
                        item.category.value,
                    )
                    for item in items
                ),
            )
            conn.execute(
                """UPDATE paid_package_jobs SET state='ready',lease_token=NULL,lease_until=NULL,
                delivery_id=?,error_code=NULL WHERE order_id=?""",
                (delivery_id, job.order_id),
            )
            return True

        return self._write(finish)
