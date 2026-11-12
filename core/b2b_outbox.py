"""Durable service notifications. All methods finish SQL before provider IO."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from .checkout_store import CheckoutStore
from .email_sender import DeliveryResult, normalize_address

LEASE_SECONDS = 90
MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class NotificationTicket:
    delivery_id: int
    customer_id: int
    recipient: str = field(repr=False)
    published_at: str
    lease_token: str = field(repr=False)
    lease_until: int


@dataclass(frozen=True)
class ReadyDelivery:
    delivery_id: int
    new_count: int
    change_count: int
    full_count: int


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("Limit must be between 1 and 100")
    return value


class B2BOutbox(CheckoutStore):
    def enqueue_ready(self, delivery_id: int) -> bool:
        if type(delivery_id) is not int or not 1 <= delivery_id < 2**63:
            raise ValueError("Invalid delivery")

        def enqueue(conn):
            return (
                conn.execute(
                    """INSERT OR IGNORE INTO b2b_outbox(delivery_id,customer_id,recipient,published_at)
                SELECT p.delivery_id,o.customer_id,c.email,p.published_at
                FROM package_publications p JOIN b2b_deliveries d USING(delivery_id)
                JOIN b2b_orders o USING(order_id) JOIN b2b_customers c USING(customer_id)
                WHERE p.delivery_id=?""",
                    (delivery_id,),
                ).rowcount
                == 1
            )

        return self._write(enqueue)

    def recover_stale(self, *, now: datetime, limit: int = 100) -> None:
        limit, stamp = _limit(limit), int(now.timestamp())

        def recover(conn):
            for status, recovered, code in (
                ("leased", "pending", "lease_expired_before_send"),
                ("sending", "unknown", "lease_expired_unknown"),
            ):
                conn.execute(
                    """UPDATE b2b_outbox SET status=?,lease_token=NULL,lease_until=NULL,error_code=?,
                    finished_at=CASE WHEN ?='unknown' THEN ? ELSE NULL END
                    WHERE delivery_id IN (SELECT delivery_id FROM b2b_outbox
                        WHERE status=? AND lease_until<=? ORDER BY lease_until LIMIT ?)""",
                    (recovered, code, recovered, stamp, status, stamp, limit),
                )

        self._write(recover)

    def claim(self, *, now: datetime) -> NotificationTicket | None:
        stamp = int(now.timestamp())

        def claim(conn):
            row = conn.execute(
                """SELECT delivery_id,customer_id,recipient,published_at FROM b2b_outbox
                WHERE status='pending' AND next_attempt_at<=?
                ORDER BY next_attempt_at,delivery_id LIMIT 1""",
                (stamp,),
            ).fetchone()
            if row is None:
                return None
            token, until = uuid4().hex, stamp + LEASE_SECONDS
            conn.execute(
                "UPDATE b2b_outbox SET status='leased',lease_token=?,lease_until=? WHERE delivery_id=?",
                (token, until, row["delivery_id"]),
            )
            return NotificationTicket(**dict(row), lease_token=token, lease_until=until)

        return self._write(claim)

    def begin_send(
        self, ticket: NotificationTicket, *, now: datetime
    ) -> ReadyDelivery | None:
        stamp = int(now.timestamp())

        def begin(conn):
            active = conn.execute(
                """SELECT 1 FROM b2b_outbox WHERE delivery_id=? AND status='leased'
                AND lease_token=? AND lease_until>?""",
                (ticket.delivery_id, ticket.lease_token, stamp),
            ).fetchone()
            if active is None:
                return None
            row = conn.execute(
                """SELECT d.delivery_id,
                    COALESCE(SUM(i.kategoria_dostawy='new'),0) AS new_count,
                    COALESCE(SUM(i.kategoria_dostawy='change'),0) AS change_count,
                    COALESCE(SUM(i.kategoria_dostawy='full'),0) AS full_count
                FROM b2b_deliveries d JOIN b2b_orders o USING(order_id)
                JOIN b2b_customers c USING(customer_id) JOIN package_publications p USING(delivery_id)
                LEFT JOIN b2b_delivery_items i USING(delivery_id)
                WHERE d.delivery_id=? AND o.customer_id=? AND c.email=? COLLATE NOCASE
                    AND o.status_platnosci='paid' AND p.published_at=?
                    AND (SELECT count(*) FROM package_artifacts a WHERE a.delivery_id=d.delivery_id
                        AND a.relative_name=CASE a.format WHEN 'xlsx' THEN d.plik_xlsx ELSE d.plik_csv END)=2
                GROUP BY d.delivery_id""",
                (
                    ticket.delivery_id,
                    ticket.customer_id,
                    ticket.recipient,
                    ticket.published_at,
                ),
            ).fetchone()
            try:
                normalize_address("email", ticket.recipient)
            except ValueError:
                row = None
            if row is None:
                conn.execute(
                    """UPDATE b2b_outbox SET status='cancelled',lease_token=NULL,lease_until=NULL,
                    finished_at=?,error_code='authorization_changed' WHERE delivery_id=?""",
                    (stamp, ticket.delivery_id),
                )
                return None
            conn.execute(
                "UPDATE b2b_outbox SET status='sending',attempts=attempts+1 WHERE delivery_id=?",
                (ticket.delivery_id,),
            )
            return ReadyDelivery(**dict(row))

        return self._write(begin)

    def finish(
        self, ticket: NotificationTicket, result: DeliveryResult, *, now: datetime
    ) -> bool:
        stamp = int(now.timestamp())

        def finish(conn):
            row = conn.execute(
                """SELECT attempts FROM b2b_outbox WHERE delivery_id=? AND status='sending'
                AND lease_token=? AND lease_until>?""",
                (ticket.delivery_id, ticket.lease_token, stamp),
            ).fetchone()
            if row is None:
                return False
            status = {
                "accepted": "accepted",
                "failed": "rejected",
                "unknown": "unknown",
                "retry": "pending",
            }.get(result.outcome, "unknown")
            code = None if status == "accepted" else "smtp_" + status
            delay = min(3600, 60 * 2 ** (row["attempts"] - 1))
            retry_after = result.retry_after_seconds
            if type(retry_after) in (int, float) and math.isfinite(retry_after):
                delay = max(delay, min(3600, max(0, int(retry_after))))
            if status == "pending" and row["attempts"] >= MAX_ATTEMPTS:
                status, code = "rejected", "retry_exhausted"
            receipt = result.provider_message_id
            if (
                not isinstance(receipt, str)
                or not 1 <= len(receipt) <= 256
                or not receipt.isascii()
                or any(c in receipt for c in "\r\n")
            ):
                receipt = None
            conn.execute(
                """UPDATE b2b_outbox SET status=?,lease_token=NULL,lease_until=NULL,finished_at=?,
                next_attempt_at=?,error_code=?,provider_receipt=? WHERE delivery_id=?""",
                (
                    status,
                    None if status == "pending" else stamp,
                    stamp + delay if status == "pending" else 0,
                    code,
                    receipt,
                    ticket.delivery_id,
                ),
            )
            return True

        return self._write(finish)
