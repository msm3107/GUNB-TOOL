import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from core.checkout_store import CheckoutConflict, CheckoutStore, CheckoutUnavailable
from core.payment_store import PaidCheckout, PaymentStore
from core.storage import LeadRepository

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
KEY = "a" * 64


@pytest.fixture(name="payment")
def payment_fixture(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with LeadRepository(path):
        pass
    attempt = CheckoutStore(path).reserve(
        key_hash=KEY,
        fingerprint="b" * 64,
        email="synthetic@example.test",
        filters_json='{"wojewodztwo":"28","branza":"materialy"}',
        kind="jednorazowy",
        offer_json=json.dumps({"amount_grosze": 12345, "interval": None}),
        origin="https://example.test",
        now=NOW,
    )
    paid = PaidCheckout(
        KEY,
        "cs_test_synthetic",
        str(attempt.order_id),
        str(attempt.customer_id),
        "payment",
        "pln",
        12345,
        int(NOW.timestamp()),
    )
    return PaymentStore(path), paid


def record(store, paid, event="evt_synthetic"):
    return store.record_event(
        event_id=event,
        event_type="checkout.session.completed",
        completion=paid,
        now=NOW,
    )


def test_paid_event_and_job_are_durable_and_deduplicated_by_event_and_session(payment):
    store, paid = payment
    assert record(store, paid) == 1
    assert record(PaymentStore(store.path), paid) is None
    assert record(store, paid, "evt_distinct") is None
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone() == (
            "paid",
        )
        assert c.execute("SELECT count(*) FROM stripe_events").fetchone() == (2,)
        assert c.execute(
            "SELECT order_id,state,session_id FROM paid_package_jobs"
        ).fetchone() == (1, "pending", "cs_test_synthetic")
        assert c.execute(
            "SELECT stripe_customer_id,status_subskrypcji FROM b2b_customers"
        ).fetchone() == (None, "nieaktywna")
        assert c.execute("SELECT count(*) FROM b2b_deliveries").fetchone() == (0,)


@pytest.mark.parametrize(
    "changes",
    [
        {"key_hash": "c" * 64},
        {"order_id": "2"},
        {"customer_id": "2"},
        {"amount_total": 12344},
        {"amount_total": 12345.0},
        {"currency": "eur"},
        {"mode": "subscription"},
        {"session_created": int(NOW.timestamp()) - 301},
    ],
)
def test_mismatched_payment_never_updates_or_acks(payment, changes):
    store, paid = payment
    with pytest.raises(CheckoutConflict):
        record(store, replace(paid, **changes))
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone() == (
            "pending",
        )
        assert c.execute("SELECT count(*) FROM stripe_events").fetchone() == (0,)
        assert c.execute("SELECT count(*) FROM paid_package_jobs").fetchone() == (0,)


def test_session_correlation_and_order_immutability(payment):
    store, paid = payment
    CheckoutStore(store.path).complete(
        KEY, "cs_test_other", "https://checkout.stripe.com/other"
    )
    with pytest.raises(CheckoutConflict):
        record(store, paid)
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE b2b_orders SET filtry_json='{}'")
    with pytest.raises(CheckoutConflict):
        record(store, replace(paid, session_id="cs_test_other"))


def test_webhook_can_precede_checkout_response_save_without_creating_second_order(
    payment,
):
    store, paid = payment
    record(store, paid)
    CheckoutStore(store.path).complete(
        KEY, paid.session_id, "https://checkout.stripe.com/test"
    )
    with pytest.raises(CheckoutConflict):
        record(store, replace(paid, session_id="cs_test_other"), "evt_other")
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM b2b_orders").fetchone() == (1,)


def test_failed_enqueue_rolls_back_payment_and_event(payment):
    store, paid = payment
    with sqlite3.connect(store.path) as c:
        c.execute(
            "CREATE TRIGGER fail_job BEFORE INSERT ON paid_package_jobs BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(CheckoutUnavailable):
        record(store, paid)
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone() == (
            "pending",
        )
        assert c.execute("SELECT count(*) FROM stripe_events").fetchone() == (0,)
        c.execute("DROP TRIGGER fail_job")
    assert record(store, paid) == 1


def test_ignored_event_is_durable_without_payment(payment):
    store, _ = payment
    assert (
        store.record_event(
            event_id="evt_ignored",
            event_type="payment_intent.succeeded",
            completion=None,
            now=NOW,
        )
        is None
    )
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT outcome FROM stripe_events").fetchone() == ("ignored",)
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone() == (
            "pending",
        )


def test_v20_migration_backup_and_rollback(tmp_path, monkeypatch):
    from core import storage
    from tests.test_storage import legacy_database

    path = tmp_path / "legacy.sqlite"
    c = legacy_database(path, 19)
    c.execute("INSERT INTO b2b_customers(email) VALUES('kept@example.test')")
    c.execute(
        "INSERT INTO b2b_orders(customer_id,typ_pakietu,status_platnosci) VALUES(1,'jednorazowy','paid')"
    )
    c.commit()
    c.close()
    steps = storage._MIGRATIONS
    monkeypatch.setattr(
        storage,
        "_MIGRATIONS",
        steps[:19] + (steps[19] + "SELECT missing_payment_function();",),
    )
    with pytest.raises(sqlite3.OperationalError):
        LeadRepository(path)
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone() == (19,)
        assert (
            c.execute(
                "SELECT name FROM sqlite_master WHERE name='stripe_events'"
            ).fetchone()
            is None
        )
    monkeypatch.setattr(storage, "_MIGRATIONS", steps)
    with LeadRepository(path) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        assert (
            repo.connection.execute(
                "SELECT status_platnosci FROM b2b_orders"
            ).fetchone()[0]
            == "paid"
        )
        assert not repo.connection.execute("PRAGMA foreign_key_check").fetchall()
    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert backups
    for backup in backups:
        with sqlite3.connect(backup) as c:
            assert c.execute("PRAGMA user_version").fetchone() == (19,)


def test_concurrent_receipts_across_store_instances_enqueue_once(payment):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store, paid = payment
    barrier = Barrier(2)

    def submit(event):
        barrier.wait(timeout=5)
        try:
            return record(PaymentStore(store.path), paid, event)
        except CheckoutUnavailable:
            return "retry"

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(submit, ["evt_parallelA", "evt_parallelB"]))
    assert 1 in results
    record(store, paid, "evt_parallelA")
    record(store, paid, "evt_parallelB")
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM paid_package_jobs").fetchone() == (1,)
        assert c.execute("SELECT count(*) FROM stripe_events").fetchone() == (2,)


def test_payment_store_does_not_migrate_or_replace_missing_database(payment, tmp_path):
    store, paid = payment
    with sqlite3.connect(store.path) as c:
        c.execute("PRAGMA user_version=19")
    with pytest.raises(CheckoutUnavailable):
        record(store, paid)
    missing = tmp_path / "absent" / "db.sqlite"
    with pytest.raises(CheckoutUnavailable):
        record(PaymentStore(missing), paid)
    assert not missing.parent.exists()


def test_session_timestamp_allows_bounded_server_clock_skew(payment):
    store, paid = payment
    assert record(store, replace(paid, session_created=paid.session_created - 60)) == 1
