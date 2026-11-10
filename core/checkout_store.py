"""Pending Checkout attempts; no HTTP, provider calls, migrations or fulfillment."""

from __future__ import annotations

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

    def owner_email(self, customer_id: int) -> str:
        def read(conn):
            row = conn.execute("SELECT email FROM b2b_customers WHERE customer_id=?", (customer_id,)).fetchone()
            if row is None:
                raise CheckoutConflict
            return row[0]
        return self._write(read)

    def reserve(self, *, key_hash: str, fingerprint: str, email: str, filters_json: str,
                kind: str, offer_json: str, origin: str, now: datetime, owner_id: int | None = None) -> CheckoutAttempt:
        if not re.fullmatch("[0-9a-f]{64}", key_hash) or not re.fullmatch("[0-9a-f]{64}", fingerprint):
            raise ValueError("Invalid checkout digest")
        stamp = int(now.timestamp())

        def reserve(conn):
            if owner_id is not None:
                owner = conn.execute("SELECT email FROM b2b_customers WHERE customer_id=?", (owner_id,)).fetchone()
                if owner is None or owner[0].lower() != email:
                    raise CheckoutConflict
            row = conn.execute("SELECT * FROM checkout_attempts WHERE key_hash=?", (key_hash,)).fetchone()
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
