"""Hashed, bounded authentication state. No HTTP, signing key, SMTP or Stripe."""

from __future__ import annotations

from datetime import datetime

from .checkout_store import CheckoutConflict, CheckoutStore
from .subscription_store import SubscriptionNotice


class AuthRejected(ValueError):
    pass


def _cleanup(conn, stamp: int) -> None:
    for table, field, cutoff in (
        ("auth_requests", "created_at", stamp - 7200),
        ("auth_magic_links", "expires_at", stamp - 86400),
        ("auth_sessions", "expires_at", stamp - 86400),
    ):
        # Identifiers are fixed above; each operation bounds its cleanup work.
        conn.execute(
            f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} WHERE {field}<? LIMIT 100)",
            (cutoff,),
        )


class AuthStore(CheckoutStore):
    def issue_link(
        self,
        *,
        email: str,
        token_hash: str,
        browser_hash: str,
        address_hash: str,
        ip_hash: str,
        now: datetime,
    ) -> bool:
        stamp = int(now.timestamp())

        def issue(conn):
            _cleanup(conn, stamp)
            cutoff = stamp - 3600
            if (
                conn.execute(
                    "SELECT count(*) FROM auth_requests WHERE address_hash=? AND created_at>?",
                    (address_hash, cutoff),
                ).fetchone()[0]
                >= 3
                or conn.execute(
                    "SELECT count(*) FROM auth_requests WHERE ip_hash=? AND created_at>?",
                    (ip_hash, cutoff),
                ).fetchone()[0]
                >= 10
                or conn.execute(
                    "SELECT count(*) FROM auth_requests WHERE created_at>?", (cutoff,)
                ).fetchone()[0]
                >= 100
            ):
                return False
            conn.execute(
                "INSERT INTO auth_requests(address_hash,ip_hash,created_at) VALUES(?,?,?)",
                (address_hash, ip_hash, stamp),
            )
            row = conn.execute(
                "SELECT customer_id,email FROM b2b_customers WHERE email=?", (email,)
            ).fetchone()
            if row is None:
                return False
            conn.execute(
                """INSERT INTO auth_magic_links(token_hash,customer_id,verified_email,browser_hash,created_at,expires_at)
                VALUES(?,?,?,?,?,?)""",
                (
                    token_hash,
                    row["customer_id"],
                    row["email"],
                    browser_hash,
                    stamp,
                    stamp + 900,
                ),
            )
            return True

        return self._write(issue)

    def mark_delivery(self, token_hash: str, outcome: str) -> None:
        self._write(
            lambda conn: conn.execute(
                "UPDATE auth_magic_links SET delivery_status=? WHERE token_hash=?",
                (outcome, token_hash),
            )
        )

    def consume_link(
        self,
        *,
        token_hash: str,
        browser_hash: str,
        session_hash: str,
        now: datetime,
        old_session_hash: str | None = None,
    ) -> int:
        stamp = int(now.timestamp())

        def consume(conn):
            row = conn.execute(
                """SELECT l.customer_id,l.verified_email FROM auth_magic_links l
                JOIN b2b_customers c USING(customer_id) WHERE token_hash=? AND browser_hash=?
                AND used_at IS NULL AND expires_at>? AND l.verified_email=c.email COLLATE NOCASE""",
                (token_hash, browser_hash, stamp),
            ).fetchone()
            if row is None:
                raise AuthRejected
            conn.execute(
                "UPDATE auth_magic_links SET used_at=? WHERE token_hash=?",
                (stamp, token_hash),
            )
            conn.execute(
                "INSERT INTO auth_sessions VALUES(?,?,?,?,?)",
                (
                    session_hash,
                    row["customer_id"],
                    row["verified_email"],
                    stamp,
                    stamp + 28800,
                ),
            )
            if old_session_hash:
                conn.execute(
                    "DELETE FROM auth_sessions WHERE session_hash=?",
                    (old_session_hash,),
                )
            return row["customer_id"]

        return self._write(consume)

    def session_customer(self, session_hash: str, *, now: datetime) -> int | None:
        def read(conn):
            row = conn.execute(
                """SELECT s.customer_id FROM auth_sessions s JOIN b2b_customers c USING(customer_id)
                WHERE session_hash=? AND expires_at>? AND s.verified_email=c.email COLLATE NOCASE""",
                (session_hash, int(now.timestamp())),
            ).fetchone()
            return row[0] if row else None

        return self._write(read)

    def revoke_session(self, session_hash: str) -> None:
        self._write(
            lambda conn: conn.execute(
                "DELETE FROM auth_sessions WHERE session_hash=?", (session_hash,)
            )
        )

    def claim_billing(self, customer_id: int, key_hash: str) -> SubscriptionNotice:
        def claim(conn):
            row = conn.execute(
                """SELECT a.key_hash,a.order_id,a.customer_id,l.stripe_customer_id,l.subscription_id,
                    c.stripe_customer_id AS trusted
                FROM checkout_attempts a JOIN b2b_orders o USING(order_id)
                JOIN checkout_billing_links l USING(order_id) JOIN b2b_customers c ON c.customer_id=o.customer_id
                WHERE a.key_hash=? AND o.customer_id=? AND a.customer_id=o.customer_id
                  AND a.email=c.email COLLATE NOCASE AND o.status_platnosci='paid' AND a.kind='abonament' """,
                (key_hash, customer_id),
            ).fetchone()
            if row is None or row["trusted"] not in (None, row["stripe_customer_id"]):
                raise CheckoutConflict
            if row["trusted"] is None:
                conn.execute(
                    "UPDATE b2b_customers SET stripe_customer_id=?,status_subskrypcji='nieaktywna' WHERE customer_id=?",
                    (row["stripe_customer_id"], customer_id),
                )
                conn.execute(
                    """UPDATE b2b_subscriptions SET status='incomplete',refresh_token=NULL,refresh_until=NULL
                    WHERE customer_id=? AND stripe_customer_id=? AND terminal=0""",
                    (customer_id, row["stripe_customer_id"]),
                )
            return SubscriptionNotice(
                row["subscription_id"],
                row["stripe_customer_id"],
                row["key_hash"],
                str(row["order_id"]),
                str(customer_id),
            )

        return self._write(claim)
