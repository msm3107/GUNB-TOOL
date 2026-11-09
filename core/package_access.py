"""Artifact identities and download authorizations; all file IO stays outside SQL."""

from __future__ import annotations

import argparse
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .checkout_store import CheckoutStore, CheckoutUnavailable
from .instance import AlreadyRunning, database_replacement_lock
from .package_files import (
    MAX_ARTIFACT_SIZE,
    Artifact,
    ArtifactUnavailable,
    inspect_artifact,
    valid_name,
)
from .storage import SCHEMA_VERSION


class PackageNotFound(LookupError):
    pass


class DownloadBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadGrant:
    delivery_id: int
    artifact: Artifact
    expires_at: int


_OWNED = """FROM package_artifacts a JOIN b2b_deliveries d USING(delivery_id)
    JOIN b2b_orders o USING(order_id)
    WHERE o.customer_id=? AND o.status_platnosci='paid'
      AND (d.doreczono_utc IS NOT NULL OR EXISTS (
          SELECT 1 FROM package_publications p WHERE p.delivery_id=d.delivery_id))
      AND a.relative_name=CASE a.format WHEN 'xlsx' THEN d.plik_xlsx ELSE d.plik_csv END
"""


def validate_artifacts(artifacts: tuple[Artifact, ...]):
    if len(artifacts) != 2 or {a.format for a in artifacts} != {"xlsx", "csv"}:
        raise ValueError("Both artifact formats required")
    for a in artifacts:
        if (
            not valid_name(a.relative_name, a.format)
            or not 0 < a.size <= MAX_ARTIFACT_SIZE
            or len(a.sha256) != 64
            or any(c not in "0123456789abcdef" for c in a.sha256)
        ):
            raise ValueError("Invalid artifact metadata")


def record_artifacts(conn, delivery_id: int, artifacts: tuple[Artifact, ...]):
    row = conn.execute(
        "SELECT plik_xlsx,plik_csv FROM b2b_deliveries WHERE delivery_id=?",
        (delivery_id,),
    ).fetchone()
    if row is None or any(
        row["plik_" + a.format] != a.relative_name for a in artifacts
    ):
        raise ValueError("Artifact identity differs from delivery")
    for a in artifacts:
        old = conn.execute(
            "SELECT relative_name,size,sha256 FROM package_artifacts WHERE delivery_id=? AND format=?",
            (delivery_id, a.format),
        ).fetchone()
        if old is not None and tuple(old) != (a.relative_name, a.size, a.sha256):
            raise ValueError("Immutable artifact already exists")
        conn.execute(
            "INSERT OR IGNORE INTO package_artifacts(delivery_id,format,relative_name,size,sha256) VALUES(?,?,?,?,?)",
            (delivery_id, a.format, a.relative_name, a.size, a.sha256),
        )


class PackageAccessStore(CheckoutStore):
    def artifact_for(
        self, customer_id: int, delivery_id: int, fmt: str
    ) -> DownloadGrant:
        def find(conn):
            row = conn.execute(
                "SELECT a.* " + _OWNED + " AND a.delivery_id=? AND a.format=?",
                (customer_id, delivery_id, fmt),
            ).fetchone()
            if row is None:
                raise PackageNotFound
            return DownloadGrant(
                delivery_id,
                Artifact(fmt, row["relative_name"], row["size"], row["sha256"]),
                0,
            )

        return self._read(find)

    def record_artifacts(self, delivery_id: int, artifacts: tuple[Artifact, ...]):
        validate_artifacts(artifacts)
        self._write(lambda conn: record_artifacts(conn, delivery_id, artifacts))

    def _read(self, action):
        if self.path is None:
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
                    deadline = time.monotonic() + 0.5
                    conn.set_progress_handler(
                        lambda: int(time.monotonic() > deadline), 1000
                    )
                    conn.execute("PRAGMA query_only=ON")
                    conn.execute("BEGIN")
                    if (
                        conn.execute("PRAGMA user_version").fetchone()[0]
                        != SCHEMA_VERSION
                        or conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
                    ):
                        raise CheckoutUnavailable
                    return action(conn)
                finally:
                    conn.close()
        except (OSError, sqlite3.Error, AlreadyRunning):
            raise CheckoutUnavailable from None

    def backfill(self, delivery_id: int, root: Path) -> int:
        row = self._read(
            lambda c: c.execute(
                "SELECT plik_xlsx,plik_csv FROM b2b_deliveries WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone()
        )
        if row is None:
            raise PackageNotFound
        artifacts = tuple(
            inspect_artifact(root, row["plik_" + fmt], fmt) for fmt in ("xlsx", "csv")
        )
        self.record_artifacts(delivery_id, artifacts)
        return len(artifacts)

    def issue(
        self, customer_id: int, delivery_id: int, fmt: str, token_hash: str, *, now
    ) -> DownloadGrant:
        if (
            fmt not in {"xlsx", "csv"}
            or re.fullmatch("[0-9a-f]{64}", token_hash) is None
        ):
            raise ValueError("Invalid download identity")
        stamp = int(now.timestamp())

        def issue(conn):
            row = conn.execute(
                "SELECT a.* " + _OWNED + " AND a.delivery_id=? AND a.format=?",
                (customer_id, delivery_id, fmt),
            ).fetchone()
            if row is None:
                raise PackageNotFound
            if (
                conn.execute(
                    "SELECT count(*) FROM download_grants WHERE customer_id=? AND created_at>?",
                    (customer_id, stamp - 60),
                ).fetchone()[0]
                >= 10
            ):
                raise DownloadBusy
            conn.execute(
                "INSERT INTO download_grants(token_hash,customer_id,delivery_id,format,relative_name,size,sha256,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    token_hash,
                    customer_id,
                    delivery_id,
                    fmt,
                    row["relative_name"],
                    row["size"],
                    row["sha256"],
                    stamp,
                    stamp + 900,
                ),
            )
            return DownloadGrant(
                delivery_id,
                Artifact(fmt, row["relative_name"], row["size"], row["sha256"]),
                stamp + 900,
            )

        return self._write(issue)

    def resolve(self, customer_id: int, token_hash: str) -> DownloadGrant:
        def resolve(conn):
            row = conn.execute(
                """SELECT a.*,g.expires_at FROM download_grants g
                JOIN package_artifacts a USING(delivery_id,format)
                JOIN b2b_deliveries d USING(delivery_id) JOIN b2b_orders o USING(order_id)
                WHERE g.token_hash=? AND g.customer_id=? AND o.customer_id=g.customer_id
                  AND o.status_platnosci='paid'
                  AND (d.doreczono_utc IS NOT NULL OR EXISTS (SELECT 1 FROM package_publications p WHERE p.delivery_id=d.delivery_id))
                  AND a.sha256=g.sha256 AND a.size=g.size AND a.relative_name=g.relative_name
                  AND a.relative_name=CASE a.format WHEN 'xlsx' THEN d.plik_xlsx ELSE d.plik_csv END""",
                (token_hash, customer_id),
            ).fetchone()
            if row is None:
                raise PackageNotFound
            return DownloadGrant(
                row["delivery_id"],
                Artifact(
                    row["format"], row["relative_name"], row["size"], row["sha256"]
                ),
                row["expires_at"],
            )

        return self._read(resolve)

    def audit(
        self,
        customer_id,
        token_hash: str,
        method: str,
        outcome: str,
        ip_hash: str,
        agent_hash: str,
        *,
        now,
    ):
        self._write(
            lambda c: c.execute(
                "INSERT INTO download_audit(token_hash,customer_id,attempted_at,method,outcome,ip_hash,agent_hash) VALUES(?,?,?,?,?,?,?)",
                (
                    token_hash,
                    customer_id,
                    int(now.timestamp()),
                    method,
                    outcome,
                    ip_hash,
                    agent_hash,
                ),
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill immutable manifests for one existing delivery; never publish or acknowledge"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--delivery-id", type=int, required=True)
    args = parser.parse_args()
    try:
        if args.delivery_id < 1:
            raise ValueError
        count = PackageAccessStore(args.database).backfill(args.delivery_id, args.root)
    except (CheckoutUnavailable, ArtifactUnavailable, PackageNotFound, ValueError):
        print(
            "Manifest unavailable; verify migrated database, private files and delivery identity"
        )
        return 1
    print(f"Manifest recorded: {count} formats; publication unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
