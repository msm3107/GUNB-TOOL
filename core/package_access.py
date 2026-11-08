"""Artifact identities and download authorizations; all file IO stays outside SQL."""

from __future__ import annotations

from .checkout_store import CheckoutStore
from .package_files import MAX_ARTIFACT_SIZE, Artifact, valid_name


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
    def record_artifacts(self, delivery_id: int, artifacts: tuple[Artifact, ...]):
        validate_artifacts(artifacts)
        self._write(lambda conn: record_artifacts(conn, delivery_id, artifacts))
