"""Prepare private test-paid exports. No network, HTTP, Stripe SDK or delivery ACK."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .checkout_store import CheckoutUnavailable
from .delivery_memory import DeliveryClassifier
from .export_package import write_csv, write_xlsx
from .instance import AlreadyRunning, database_replacement_lock
from .package_selection import PackageFilters, PackageTooLarge, select_package
from .payment_store import PaymentStore
from .storage import LeadRepository

log = logging.getLogger(__name__)


class PaidPackagePreparer:
    def __init__(self, database_path: Path, output_dir: Path, *, now=None):
        self.store = PaymentStore(database_path)
        self.output_dir = output_dir
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._slots = threading.BoundedSemaphore(2)

    def run_pending(self, limit: int = 10) -> int:
        return sum(
            self.prepare(order)
            for order in self.store.pending_packages(now=self._now(), limit=limit)
        )

    def prepare(self, order_id: int) -> bool:
        if not self._slots.acquire(blocking=False):
            return False  # durable pending job is available to the recovery runner
        job = None
        paths: list[Path] = []
        try:
            job = self.store.claim_package(order_id, now=self._now())
            if job is None:
                return False
            selected = json.loads(job.filters_json)
            for key in ("powiaty", "kategorie"):
                if key in selected:
                    selected[key] = tuple(selected[key])
            filters = PackageFilters(**selected)
            as_of = self._now().astimezone(ZoneInfo("Europe/Warsaw")).date()
            path = self.store.path.resolve(strict=True)
            with (
                database_replacement_lock(path),
                LeadRepository(path, existing_only=True) as repo,
            ):
                conn = repo.connection
                deadline = time.monotonic() + 2
                conn.set_progress_handler(
                    lambda: int(time.monotonic() >= deadline), 1000
                )
                conn.execute("PRAGMA query_only=ON")
                conn.execute("BEGIN")
                items = DeliveryClassifier(repo).classify(
                    job.order_id, select_package(conn, filters, today=as_of)
                )
            # The bounded snapshot is detached: no connection or OS guard during export.
            self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            output = self.output_dir.resolve(strict=True)
            output.chmod(0o700)
            for suffix, writer in (("xlsx", write_xlsx), ("csv", write_csv)):
                destination = output / f"{job.token}.{suffix}"
                # Exclusive creation prevents following pre-existing file symlinks.
                with destination.open("xb") as stream:
                    paths.append(destination)
                    destination.chmod(0o600)
                    writer(items, stream, trade_key=filters.branza, as_of=as_of)
                    stream.flush()
                    os.fsync(stream.fileno())
            if self.store.finish_package(
                job, items, paths[0].name, paths[1].name, now=self._now()
            ):
                paths.clear()  # paths become visible through the DB only after both exports exist
                return True
            return False
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            sqlite3.Error,
            AlreadyRunning,
            CheckoutUnavailable,
        ) as exc:
            # No source data, payload or exception details in operational logs.
            log.warning("Test package preparation failed; order_id=%s", order_id)
            if job is not None:
                try:
                    self.store.fail_package(
                        job,
                        "too_large"
                        if isinstance(exc, PackageTooLarge)
                        else "prepare_failed",
                    )
                except CheckoutUnavailable:
                    pass  # expired lease remains recoverable after storage is healthy
            return False
        finally:
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.warning(
                        "Private test export cleanup failed; order_id=%s", order_id
                    )
            self._slots.release()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Recover private test-paid package preparation; no delivery or payment calls"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    try:
        preparer = PaidPackagePreparer(args.database, args.output)
        ready = preparer.run_pending(args.limit)
        pending = preparer.store.has_unfinished_packages()
    except (CheckoutUnavailable, ValueError):
        print("Preparation unavailable; verify existing WAL schema and limit")
        return 1
    print(f"Prepared test packages: {ready}")
    if pending:
        print(
            "Pending work remains; inspect paid_package_jobs.error_code, wait for active lease expiry and retry"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
