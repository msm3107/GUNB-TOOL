"""Pending Checkout attempts; no HTTP, provider calls, migrations or fulfillment."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .instance import AlreadyRunning, database_replacement_lock
from .storage import SCHEMA_VERSION


class CheckoutUnavailable(RuntimeError):
    pass


class CheckoutConflict(ValueError):
    pass


class CheckoutUnauthorized(CheckoutConflict):
    pass


def is_owned_checkout(offer_json: str, customer_id: int) -> bool:
    try:
        marker = json.loads(offer_json).get("authenticated_customer_id")
        return type(marker) is int and marker == customer_id
    except (ValueError, TypeError, AttributeError, RecursionError):
        return False


@dataclass(frozen=True)
class CheckoutAttempt:
    key_hash: str
    fingerprint: str
    order_id: int
    customer_id: int
    email: str
    filters_json: str
    kind: str
    offer_json: str
    origin: str
    created_at: int
    expires_at: int
    session_id: str | None
    url: str | None


class CheckoutStore:
    def __init__(self, path: Path | None):
        self.path = path

    @staticmethod
    def _check_session(conn, owner_id, session_hash, stamp):
        if session_hash is not None and conn.execute(
            """SELECT 1 FROM auth_sessions s JOIN b2b_customers c USING(customer_id)
            WHERE session_hash=? AND s.customer_id=? AND expires_at>?
            AND s.verified_email=c.email COLLATE NOCASE""", (session_hash, owner_id, stamp)
        ).fetchone() is None:
            raise CheckoutUnauthorized

    def read_owned_offer(self, customer_id, key_hash, *, session_hash, now):
        def read(conn):
            self._check_session(conn, customer_id, session_hash, int(now.timestamp()))
            row = conn.execute("""SELECT a.customer_id,o.customer_id AS owner,a.kind,a.offer_json,a.email,c.email AS current_email
                FROM checkout_attempts a JOIN b2b_orders o USING(order_id)
                JOIN b2b_customers c ON c.customer_id=o.customer_id WHERE a.key_hash=?""", (key_hash,)).fetchone()
            if row is None:
                return None
            if (row["customer_id"] != customer_id or row["owner"] != customer_id
                    or row["email"].lower() != row["current_email"].lower()
                    or not is_owned_checkout(row["offer_json"], customer_id)):
                raise CheckoutConflict
            return row["kind"], row["offer_json"]
        return self._write(read)

    def _write(self, action):
        if self.path is None:
            raise CheckoutUnavailable
        try:
            path = self.path.resolve(strict=True)
            with database_replacement_lock(path):
                conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=0.1, isolation_level=None)
                try:
                    conn.row_factory = sqlite3.Row
                    conn.execute("PRAGMA foreign_keys=ON")
                    if (conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                            or conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal"):
                        raise CheckoutUnavailable
                    conn.execute("BEGIN IMMEDIATE")
                    result = action(conn)
                    conn.execute("COMMIT")
                    return result
                finally:
                    conn.close()  # rolls back an unfinished transaction, including conflicts
        except (OSError, sqlite3.Error, AlreadyRunning):
            raise CheckoutUnavailable from None

    def owner_identity(self, customer_id: int) -> tuple[str, str | None]:
        def read(conn):
            row = conn.execute("SELECT email,stripe_customer_id FROM b2b_customers WHERE customer_id=?", (customer_id,)).fetchone()
            if row is None:
                raise CheckoutConflict
            return row[0], row[1]
        return self._write(read)

    def reserve(self, *, key_hash: str, fingerprint: str, email: str, filters_json: str,
                kind: str, offer_json: str, origin: str, now: datetime, owner_id: int | None = None,
                session_hash: str | None = None, expected_offer_version: str | None = None, current_offer_version: str | None = None) -> CheckoutAttempt:
        if not re.fullmatch("[0-9a-f]{64}", key_hash) or not re.fullmatch("[0-9a-f]{64}", fingerprint):
            raise ValueError("Invalid checkout digest")
        stamp = int(now.timestamp())

        def reserve(conn):
            self._check_session(conn, owner_id, session_hash, stamp)
            row = conn.execute("SELECT * FROM checkout_attempts WHERE key_hash=?", (key_hash,)).fetchone()
            if row is None and expected_offer_version is not None and expected_offer_version != current_offer_version:
                raise CheckoutConflict
            if owner_id is not None:
                owner = conn.execute("SELECT email,stripe_customer_id FROM b2b_customers WHERE customer_id=?", (owner_id,)).fetchone()
                if owner is None or owner[0].lower() != email:
                    raise CheckoutConflict
                frozen = json.loads(row["offer_json"] if row else offer_json)
                if not isinstance(frozen, dict) or frozen.get("authenticated_stripe_customer_id") != owner[1]:
                    raise CheckoutConflict
            if row:
                pending = conn.execute("SELECT status_platnosci FROM b2b_orders WHERE order_id=?", (row["order_id"],)).fetchone()[0]
                if (row["fingerprint"] != fingerprint or stamp >= row["expires_at"] or pending != "pending"
                        or (owner_id is not None and row["customer_id"] != owner_id)):
                    raise CheckoutConflict
                return CheckoutAttempt(**dict(row))
            if owner_id is None:
                conn.execute("INSERT INTO b2b_customers(email) VALUES(?) ON CONFLICT(email) DO NOTHING", (email,))
                customer_id = conn.execute("SELECT customer_id FROM b2b_customers WHERE email=?", (email,)).fetchone()[0]
            else:
                customer_id = owner_id
            order_id = conn.execute("INSERT INTO b2b_orders(customer_id,filtry_json,typ_pakietu) VALUES(?,?,?)",
                                    (customer_id, filters_json, kind)).lastrowid
            conn.execute("""INSERT INTO checkout_attempts
                (key_hash,fingerprint,order_id,customer_id,email,filters_json,kind,offer_json,origin,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (key_hash, fingerprint, order_id, customer_id, email, filters_json, kind, offer_json, origin, stamp, stamp + 3600))
            return CheckoutAttempt(**dict(conn.execute("SELECT * FROM checkout_attempts WHERE key_hash=?", (key_hash,)).fetchone()))

        return self._write(reserve)

    def complete(self, key_hash: str, session_id: str, url: str) -> None:
        def complete(conn):
            row = conn.execute("SELECT session_id,url FROM checkout_attempts WHERE key_hash=?", (key_hash,)).fetchone()
            if row is None or (row["session_id"] is not None and (row["session_id"], row["url"]) != (session_id, url)):
                raise CheckoutConflict
            conn.execute("UPDATE checkout_attempts SET session_id=?,url=? WHERE key_hash=?", (session_id, url, key_hash))

        self._write(complete)
