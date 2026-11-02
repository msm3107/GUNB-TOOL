import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from core.checkout_store import CheckoutConflict, CheckoutUnavailable
from core.payment_store import PaidCheckout, PaymentStore
from core.storage import LeadRepository
from core.subscription_store import (
    SubscriptionNotice,
    SubscriptionState,
    SubscriptionStore,
)
from tests.test_payment_store import NOW
from tests.test_payment_store import payment_fixture as _payment_fixture  # noqa: F401


@pytest.fixture(name="subscription")
def subscription_fixture(payment):
    payment_store, paid = payment
    with sqlite3.connect(payment_store.path) as c:
        c.execute("UPDATE b2b_customers SET stripe_customer_id='cus_synthetic'")
        c.execute("UPDATE b2b_orders SET typ_pakietu='abonament'")
        c.execute(
            "UPDATE checkout_attempts SET kind='abonament',offer_json=?",
            (json.dumps({"amount_grosze": 12345, "interval": "month"}),),
        )
    paid = replace(
        paid,
        mode="subscription",
        stripe_customer_id="cus_synthetic",
        subscription_id="sub_synthetic",
    )
    payment_store.record_event(
        event_id="evt_checkout",
        event_type="checkout.session.completed",
        completion=paid,
        now=NOW,
    )
    notice = SubscriptionNotice(
        "sub_synthetic", "cus_synthetic", paid.key_hash, paid.order_id, paid.customer_id
    )
    return SubscriptionStore(payment_store.path), notice


def claim(store, notice, event="evt_update", now=NOW):
    return store.claim_event(notice, event_id=event, now=now)


def active(**changes):
    values = {
        "status": "active",
        "period_end": int(NOW.timestamp()) + 3600,
        "cancel_at_period_end": False,
        "paused": False,
    }
    values.update(changes)
    return SubscriptionState(**values)


def test_verified_state_updates_access_and_event_atomically(subscription):
    store, notice = subscription
    ticket = claim(store, notice)
    assert ticket is not None
    store.finish_refresh(ticket, active(), now=NOW)
    assert store.has_subscription_access(1, now=NOW)
    assert not store.has_subscription_access(2, now=NOW)
    assert not store.has_subscription_access(1, now=NOW + timedelta(hours=1))
    assert claim(store, notice) is None
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_subskrypcji FROM b2b_customers").fetchone() == (
            "active",
        )
        assert c.execute(
            "SELECT outcome FROM stripe_events WHERE event_id='evt_update'"
        ).fetchone() == ("subscription",)


@pytest.mark.parametrize(
    "state",
    [
        active(status="past_due"),
        active(status="unpaid"),
        active(status="canceled"),
        active(status="paused"),
        active(status="trialing"),
        active(paused=True),
    ],
)
def test_non_active_states_revoke_access(subscription, state):
    store, notice = subscription
    store.finish_refresh(claim(store, notice), active(), now=NOW)
    store.finish_refresh(claim(store, notice, "evt_revoke"), state, now=NOW)
    assert not store.has_subscription_access(1, now=NOW)


def test_scheduled_cancellation_keeps_access_only_until_period_end(subscription):
    store, notice = subscription
    store.finish_refresh(
        claim(store, notice), active(cancel_at_period_end=True), now=NOW
    )
    assert store.has_subscription_access(1, now=NOW)
    assert not store.has_subscription_access(1, now=NOW + timedelta(hours=1))


def test_deleted_event_fences_inflight_refresh_and_cannot_resurrect(subscription):
    store, notice = subscription
    ticket = claim(store, notice)
    assert claim(store, replace(notice, deleted=True), "evt_delete") is None
    store.finish_refresh(ticket, active(), now=NOW)
    assert claim(store, notice, "evt_old") is None
    assert not store.has_subscription_access(1, now=NOW)
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT status,terminal,refresh_token FROM b2b_subscriptions"
        ).fetchone() == ("canceled", 1, None)


def test_refresh_is_serialized_and_expired_worker_is_fenced(subscription):
    store, notice = subscription
    old = claim(store, notice)
    with pytest.raises(CheckoutUnavailable):
        claim(store, notice, "evt_second")
    later = NOW + timedelta(seconds=31)
    current = claim(SubscriptionStore(store.path), notice, "evt_second", later)
    with pytest.raises(CheckoutUnavailable):
        store.finish_refresh(old, active(), now=later)
    store.finish_refresh(current, active(status="past_due"), now=later)
    assert not store.has_subscription_access(1, now=later)


def test_unknown_stripe_customer_never_binds_by_guest_email(subscription):
    store, notice = subscription
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE b2b_customers SET stripe_customer_id=NULL")
    assert claim(store, notice) is None
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT stripe_customer_id,status_subskrypcji FROM b2b_customers"
        ).fetchone() == (None, "nieaktywna")
        assert c.execute("SELECT count(*) FROM b2b_subscriptions").fetchone() == (0,)


@pytest.mark.parametrize(
    "changes",
    [
        {"order_id": "2"},
        {"customer_id": "2"},
        {"key_hash": "c" * 64},
        {"subscription_id": "sub_other"},
    ],
)
def test_subscription_requires_exact_paid_checkout_correlation(subscription, changes):
    store, notice = subscription
    with pytest.raises(CheckoutConflict):
        claim(store, replace(notice, **changes))


def test_missing_paid_receipt_retries_and_mapping_change_revokes_access(subscription):
    store, notice = subscription
    with sqlite3.connect(store.path) as c:
        c.execute("DELETE FROM checkout_billing_links")
    with pytest.raises(CheckoutUnavailable):
        claim(store, notice)
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT count(*) FROM stripe_events WHERE event_id='evt_update'"
        ).fetchone() == (0,)


def test_failed_state_commit_keeps_event_retryable(subscription):
    store, notice = subscription
    ticket = claim(store, notice)
    with sqlite3.connect(store.path) as c:
        c.execute(
            "CREATE TRIGGER fail_state BEFORE UPDATE OF status ON b2b_subscriptions BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(CheckoutUnavailable):
        store.finish_refresh(ticket, active(), now=NOW)
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT count(*) FROM stripe_events WHERE event_id='evt_update'"
        ).fetchone() == (0,)
        assert c.execute("SELECT status_subskrypcji FROM b2b_customers").fetchone() == (
            "nieaktywna",
        )


def test_v21_migration_preserves_ledger_and_rolls_back(tmp_path, monkeypatch):
    from core import storage
    from tests.test_storage import legacy_database

    path = tmp_path / "old.sqlite"
    c = legacy_database(path, 20)
    c.execute(
        "INSERT INTO stripe_events VALUES('evt_kept','checkout.session.completed',1,'ignored',NULL)"
    )
    c.execute(
        "INSERT INTO b2b_customers(customer_id,email) VALUES(7,'kept@example.test')"
    )
    c.execute(
        "INSERT INTO b2b_orders(order_id,customer_id,typ_pakietu,status_platnosci) VALUES(8,7,'abonament','paid')"
    )
    c.execute(
        "INSERT INTO stripe_events VALUES('evt_paid','checkout.session.completed',2,'paid',8)"
    )
    c.commit()
    c.close()
    steps = storage._MIGRATIONS
    monkeypatch.setattr(
        storage,
        "_MIGRATIONS",
        steps[:-1] + (steps[-1] + "SELECT missing_subscription_function();",),
    )
    with pytest.raises(sqlite3.OperationalError):
        LeadRepository(path)
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone() == (20,)
        assert c.execute("SELECT event_id FROM stripe_events").fetchone() == (
            "evt_kept",
        )
        assert (
            c.execute(
                "SELECT name FROM sqlite_master WHERE name='b2b_subscriptions'"
            ).fetchone()
            is None
        )
    monkeypatch.setattr(storage, "_MIGRATIONS", steps)
    with LeadRepository(path) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == 21
        assert (
            repo.connection.execute("SELECT event_id FROM stripe_events").fetchone()[0]
            == "evt_kept"
        )
        assert not repo.connection.execute("PRAGMA foreign_key_check").fetchall()
        assert tuple(
            repo.connection.execute(
                "SELECT outcome,order_id FROM stripe_events WHERE event_id='evt_paid'"
            ).fetchone()
        ) == ("paid", 8)
        assert (
            repo.connection.execute(
                "SELECT status_platnosci FROM b2b_orders WHERE order_id=8"
            ).fetchone()[0]
            == "paid"
        )
    backups = list((tmp_path / "backups").glob("*.sqlite"))
    assert backups
    for backup in backups:
        with sqlite3.connect(backup) as c:
            assert c.execute("PRAGMA user_version").fetchone() == (20,)
            assert c.execute(
                "SELECT outcome,order_id FROM stripe_events WHERE event_id='evt_paid'"
            ).fetchone() == ("paid", 8)


def test_deleted_clears_paused_projection(subscription):
    store, notice = subscription
    store.finish_refresh(claim(store, notice), active(paused=True), now=NOW)
    claim(store, replace(notice, deleted=True), "evt_deletedpaused")
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_subskrypcji FROM b2b_customers").fetchone() == (
            "canceled",
        )


def test_one_canceled_subscription_does_not_revoke_another_active_subscription(
    subscription,
):
    from core.checkout_store import CheckoutStore

    store, notice = subscription
    store.finish_refresh(claim(store, notice), active(), now=NOW)
    attempt = CheckoutStore(store.path).reserve(
        key_hash="c" * 64,
        fingerprint="d" * 64,
        email="synthetic@example.test",
        filters_json='{"wojewodztwo":"28","branza":"materialy"}',
        kind="abonament",
        offer_json='{"amount_grosze":12345,"interval":"month"}',
        origin="https://example.test",
        now=NOW,
    )
    paid = PaidCheckout(
        attempt.key_hash,
        "cs_test_second",
        str(attempt.order_id),
        str(attempt.customer_id),
        "subscription",
        "pln",
        12345,
        int(NOW.timestamp()),
        "cus_synthetic",
        "sub_second",
    )
    PaymentStore(store.path).record_event(
        event_id="evt_checkoutsecond",
        event_type="checkout.session.completed",
        completion=paid,
        now=NOW,
    )
    second = SubscriptionNotice(
        "sub_second",
        "cus_synthetic",
        attempt.key_hash,
        str(attempt.order_id),
        str(attempt.customer_id),
    )
    store.finish_refresh(claim(store, second, "evt_second"), active(), now=NOW)
    claim(store, replace(notice, deleted=True), "evt_deletefirst")
    assert store.has_subscription_access(1, now=NOW)
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_subskrypcji FROM b2b_customers").fetchone() == (
            "active",
        )
        c.execute("UPDATE b2b_customers SET stripe_customer_id=NULL")
    assert not store.has_subscription_access(1, now=NOW)
