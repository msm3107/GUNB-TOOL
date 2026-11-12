"""Test billing state; trusted customer mapping is never derived from guest email."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from .checkout_store import CheckoutConflict, CheckoutStore, CheckoutUnavailable

STATUSES = frozenset(
    {
        "active",
        "past_due",
        "canceled",
        "unpaid",
        "paused",
        "trialing",
        "incomplete",
        "incomplete_expired",
    }
)


@dataclass(frozen=True)
class SubscriptionNotice:
    subscription_id: str
    stripe_customer_id: str
    key_hash: str
    order_id: str
    customer_id: str
    deleted: bool = False


@dataclass(frozen=True)
class SubscriptionState:
    status: str
    period_end: int
    cancel_at_period_end: bool
    paused: bool

    def __post_init__(self):
        if (
            self.status not in STATUSES
            or type(self.period_end) is not int
            or not 0 <= self.period_end < 2**63
            or type(self.cancel_at_period_end) is not bool
            or type(self.paused) is not bool
        ):
            raise ValueError("Invalid subscription state")


@dataclass(frozen=True)
class RefreshTicket:
    notice: SubscriptionNotice
    event_id: str
    token: str


def _receipt(conn, event_id, notice, stamp, outcome):
    event_type = (
        "local.subscription.reconciled"
        if event_id.startswith("local_reconcile_")
        else "customer.subscription.deleted"
        if notice.deleted
        else "customer.subscription.updated"
    )
    conn.execute(
        "INSERT INTO stripe_events VALUES(?,?,?,?,?)",
        (
            event_id,
            event_type,
            stamp,
            outcome,
            int(notice.order_id) if outcome == "subscription" else None,
        ),
    )


def _customer_status(conn, customer_id: int, stamp: int):
    # Cached projection is informative; authorization below always rechecks expiry.
    row = conn.execute(
        """SELECT CASE WHEN s.status='active' AND s.terminal=0 AND s.paused=0
            AND s.current_period_end>? THEN 'active' WHEN s.paused=1 THEN 'paused'
            WHEN s.status='active' THEN 'expired' ELSE s.status END AS projected
        FROM b2b_subscriptions s JOIN b2b_customers c USING(customer_id)
        WHERE s.customer_id=? AND s.stripe_customer_id=c.stripe_customer_id
        ORDER BY CASE WHEN s.status='active' AND s.terminal=0 AND s.paused=0 AND s.current_period_end>?
            THEN 0 WHEN s.status='past_due' THEN 1 ELSE 2 END,s.rowid DESC LIMIT 1""",
        (stamp, customer_id, stamp),
    ).fetchone()
    conn.execute(
        "UPDATE b2b_customers SET status_subskrypcji=? WHERE customer_id=?",
        (row[0] if row else "nieaktywna", customer_id),
    )


class SubscriptionStore(CheckoutStore):
    def claim_event(
        self, notice: SubscriptionNotice, *, event_id: str, now: datetime, session_hash: str | None = None
    ) -> RefreshTicket | None:
        stamp = int(now.timestamp())

        def claim(conn):
            self._check_session(conn, int(notice.customer_id), session_hash, stamp)
            if conn.execute(
                "SELECT 1 FROM stripe_events WHERE event_id=?", (event_id,)
            ).fetchone():
                return None
            customer = conn.execute(
                "SELECT customer_id FROM b2b_customers WHERE stripe_customer_id=?",
                (notice.stripe_customer_id,),
            ).fetchone()
            if customer is None:
                _receipt(conn, event_id, notice, stamp, "ignored")
                return None
            if str(customer[0]) != notice.customer_id:
                raise CheckoutConflict
            link = conn.execute(
                """SELECT l.*,o.customer_id,o.status_platnosci,a.key_hash,a.kind
                FROM checkout_billing_links l JOIN b2b_orders o USING(order_id)
                JOIN checkout_attempts a USING(order_id) WHERE l.order_id=?""",
                (notice.order_id,),
            ).fetchone()
            if link is None:
                if conn.execute(
                    "SELECT 1 FROM b2b_orders WHERE order_id=?", (notice.order_id,)
                ).fetchone():
                    raise CheckoutUnavailable  # callback may precede paid Checkout receipt
                raise CheckoutConflict
            if (
                link["stripe_customer_id"] != notice.stripe_customer_id
                or link["subscription_id"] != notice.subscription_id
                or str(link["customer_id"]) != notice.customer_id
                or link["key_hash"] != notice.key_hash
                or link["kind"] != "abonament"
                or link["status_platnosci"] != "paid"
            ):
                raise CheckoutConflict
            conn.execute(
                """INSERT INTO b2b_subscriptions(subscription_id,customer_id,stripe_customer_id,order_id,checkout_key)
                VALUES(?,?,?,?,?) ON CONFLICT(subscription_id) DO NOTHING""",
                (
                    notice.subscription_id,
                    customer[0],
                    notice.stripe_customer_id,
                    int(notice.order_id),
                    notice.key_hash,
                ),
            )
            row = conn.execute(
                "SELECT * FROM b2b_subscriptions WHERE subscription_id=?",
                (notice.subscription_id,),
            ).fetchone()
            if (
                row["customer_id"] != customer[0]
                or row["stripe_customer_id"] != notice.stripe_customer_id
                or row["order_id"] != int(notice.order_id)
                or row["checkout_key"] != notice.key_hash
            ):
                raise CheckoutConflict
            if notice.deleted:
                conn.execute(
                    """UPDATE b2b_subscriptions SET status='canceled',terminal=1,paused=0,cancel_at_period_end=0,
                    refresh_token=NULL,refresh_until=NULL WHERE subscription_id=?""",
                    (notice.subscription_id,),
                )
                _customer_status(conn, customer[0], stamp)
                _receipt(conn, event_id, notice, stamp, "subscription")
                return None
            if row["terminal"]:
                _receipt(conn, event_id, notice, stamp, "ignored")
                return None
            if row["refresh_until"] is not None and row["refresh_until"] > stamp:
                raise CheckoutUnavailable
            token = uuid4().hex
            conn.execute(
                "UPDATE b2b_subscriptions SET refresh_token=?,refresh_until=? WHERE subscription_id=?",
                (token, stamp + 30, notice.subscription_id),
            )
            return RefreshTicket(notice, event_id, token)

        return self._write(claim)

    def fail_refresh(self, ticket: RefreshTicket) -> None:
        self._write(
            lambda conn: conn.execute(
                """UPDATE b2b_subscriptions SET refresh_token=NULL,refresh_until=NULL
            WHERE subscription_id=? AND refresh_token=?""",
                (ticket.notice.subscription_id, ticket.token),
            )
        )

    def finish_refresh(
        self, ticket: RefreshTicket, state: SubscriptionState, *, now: datetime, session_hash: str | None = None
    ) -> None:
        stamp = int(now.timestamp())
        n = ticket.notice

        def finish(conn):
            self._check_session(conn, int(n.customer_id), session_hash, stamp)
            if conn.execute(
                "SELECT 1 FROM stripe_events WHERE event_id=?", (ticket.event_id,)
            ).fetchone():
                return
            row = conn.execute(
                "SELECT * FROM b2b_subscriptions WHERE subscription_id=?",
                (n.subscription_id,),
            ).fetchone()
            if row is None:
                raise CheckoutUnavailable
            if row["terminal"]:
                _receipt(conn, ticket.event_id, n, stamp, "ignored")
                return
            if row["refresh_token"] != ticket.token or row["refresh_until"] <= stamp:
                raise CheckoutUnavailable
            owner = conn.execute(
                "SELECT stripe_customer_id FROM b2b_customers WHERE customer_id=?",
                (int(n.customer_id),),
            ).fetchone()
            if owner is None or owner[0] != n.stripe_customer_id:
                raise CheckoutConflict
            conn.execute(
                """UPDATE b2b_subscriptions SET status=?,current_period_end=?,cancel_at_period_end=?,paused=?,
                terminal=?,refresh_token=NULL,refresh_until=NULL WHERE subscription_id=?""",
                (
                    state.status,
                    state.period_end,
                    int(state.cancel_at_period_end),
                    int(state.paused),
                    int(state.status in {"canceled", "incomplete_expired"}),
                    n.subscription_id,
                ),
            )
            _customer_status(conn, int(n.customer_id), stamp)
            _receipt(conn, ticket.event_id, n, stamp, "subscription")

        self._write(finish)

    def has_subscription_access(self, customer_id: int, *, now: datetime) -> bool:
        return self._write(
            lambda conn: (
                conn.execute(
                    """SELECT 1 FROM b2b_subscriptions s
            JOIN b2b_customers c USING(customer_id) JOIN b2b_orders o ON o.order_id=s.order_id
            WHERE s.customer_id=? AND s.stripe_customer_id=c.stripe_customer_id AND o.customer_id=s.customer_id
              AND o.status_platnosci='paid' AND s.status='active' AND s.terminal=0 AND s.paused=0
              AND s.current_period_end>? LIMIT 1""",
                    (customer_id, int(now.timestamp())),
                ).fetchone()
                is not None
            )
        )

    def billing_customer(self, customer_id: int) -> str | None:
        def read(conn):
            row = conn.execute(
                "SELECT stripe_customer_id FROM b2b_customers WHERE customer_id=?",
                (customer_id,),
            ).fetchone()
            return row[0] if row else None

        return self._write(read)
