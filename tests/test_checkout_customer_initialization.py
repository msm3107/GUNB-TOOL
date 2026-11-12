"""First account customer is coordinated across forms, retries and workers."""

import copy
import hashlib
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import stripe
from fastapi.testclient import TestClient

from app.auth import AuthService
from app.config import WebConfig
from app.main import create_app
from app.stripe_service import CheckoutService
from app.subscription_service import SubscriptionService
from core.checkout_store import CheckoutStore, CheckoutUnavailable
from core.instance import database_replacement_lock
from core.package_selection import PackageFilters
from core.payment_store import PaidCheckout, PaymentStore
from core.subscription_store import SubscriptionNotice, SubscriptionStore
from tests.conftest import Clock
from tests.test_auth_billing import login
from tests.test_auth_service import NOW, Sender
from tests.test_auth_service import settings as auth_settings
from tests.test_auth_store import (
    auth_store_fixture as _auth_store_fixture,  # noqa: F401
)
from tests.test_owned_checkout import prepare
from tests.test_stripe_service import Customers, settings
from tests.test_subscription_service import snapshot


class Sessions:
    def __init__(self, path):
        self.path = path
        self.calls = []
        self.values = {}
        self.fail_first = False
        self.check_locks = True

    def create(self, params, options):
        if self.check_locks:
            with (
                database_replacement_lock(self.path),
                sqlite3.connect(self.path, timeout=0) as conn,
            ):
                conn.execute("BEGIN IMMEDIATE")
        self.calls.append((copy.deepcopy(params), dict(options)))
        order = params["metadata"]["order_id"]
        self.values[order] = params
        if self.fail_first:
            self.fail_first = False
            raise stripe.APIConnectionError("synthetic ambiguous response")
        return {
            "id": "cs_test_order" + order,
            "livemode": False,
            "url": "https://checkout.stripe.com/test-order" + order,
        }

    def retrieve(self, session_id):
        order = session_id.removeprefix("cs_test_order")
        params = self.values[order]
        return {
            "id": session_id,
            "object": "checkout.session",
            "livemode": False,
            "mode": "subscription",
            "status": "complete",
            "payment_status": "paid",
            "currency": "pln",
            "amount_total": params["line_items"][0]["price_data"]["unit_amount"],
            "created": int(NOW.timestamp()),
            "expires_at": params["expires_at"],
            "customer_email": params.get("customer_email"),
            "customer": params.get("customer", "cus_order" + order),
            "subscription": "sub_order" + order,
            "client_reference_id": order,
            "metadata": params["metadata"],
        }


class Subscriptions:
    def __init__(self, sessions):
        self.sessions = sessions

    def retrieve(self, subscription_id):
        order = subscription_id.removeprefix("sub_order")
        params = self.sessions.values[order]
        metadata = params["metadata"]
        notice = SubscriptionNotice(
            subscription_id,
            params.get("customer", "cus_order" + order),
            metadata["checkout_key"],
            order,
            metadata["customer_id"],
        )
        return snapshot(notice)


@pytest.fixture
def first_customer(auth_store):
    customers, sessions = Customers(auth_store.path), Sessions(auth_store.path)
    clock = Clock(NOW)
    checkout = CheckoutService(
        CheckoutStore(auth_store.path),
        settings(),
        sessions=sessions,
        customers=customers,
        now=clock.now_utc,
    )
    checkout.clock = clock
    sender = Sender(auth_store.path)
    auth = AuthService(auth_store, auth_settings(), sender=sender, now=clock.now_utc)
    subs = SubscriptionService(
        SubscriptionStore(auth_store.path),
        settings(),
        subscriptions=Subscriptions(sessions),
        checkout_sessions=sessions,
        now=clock.now_utc,
    )
    config = WebConfig(
        database_path=auth_store.path, checkout=settings(), auth=auth_settings()
    )
    with TestClient(
        create_app(
            config,
            checkout_service=checkout,
            auth_service=auth,
            subscription_service=subs,
        ),
        base_url="https://example.test",
        headers={"Origin": "https://example.test"},
    ) as client:
        login(client, sender)
        yield client, checkout, customers, sessions


def pay_and_reconcile(client, checkout, sessions):
    for order, params in sessions.values.items():
        completion = PaidCheckout(
            params["metadata"]["checkout_key"],
            "cs_test_order" + order,
            order,
            "1",
            "subscription",
            "pln",
            23456,
            int(NOW.timestamp()),
            params.get("customer", "cus_order" + order),
            "sub_order" + order,
        )
        PaymentStore(checkout.store.path).record_event(
            event_id="evt_paid" + order,
            event_type="checkout.session.completed",
            completion=completion,
            now=NOW,
        )
    for order in sessions.values:
        for _ in range(2):
            assert (
                client.post(
                    "/panel/rozliczenia/" + order, follow_redirects=False
                ).status_code
                == 303
            )
    with sqlite3.connect(checkout.store.path) as conn:
        assert conn.execute(
            "SELECT count(*),count(DISTINCT stripe_customer_id) FROM checkout_billing_links"
        ).fetchone() == (2, 1)
        assert conn.execute("SELECT count(*) FROM paid_package_jobs").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT count(*) FROM stripe_events WHERE outcome='paid'"
            ).fetchone()[0]
            == 2
        )
        assert conn.execute("SELECT count(*) FROM b2b_orders").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM b2b_deliveries").fetchone()[0] == 0


def test_two_forms_share_first_customer_and_original_attempt_retries(first_customer):
    client, checkout, customers, sessions = first_customer
    first, second = prepare(client), prepare(client)
    sessions.fail_first = True
    assert client.post("/zamowienie/checkout", data=first).status_code == 503
    assert (
        client.post(
            "/zamowienie/checkout", data=second, follow_redirects=False
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/zamowienie/checkout", data=first, follow_redirects=False
        ).status_code
        == 303
    )
    assert sessions.calls[0] == sessions.calls[-1]
    pay_and_reconcile(client, checkout, sessions)
    assert customers.calls
    assert all(
        params["customer"] == "cus_synthetic" and "customer_email" not in params
        for params, _ in sessions.calls
    )


def test_parallel_workers_reserve_same_initial_customer(first_customer, monkeypatch):
    client, checkout, customers, sessions = first_customer
    barrier = threading.Barrier(2)
    reserved = set()
    lock = threading.Lock()
    reserve = CheckoutStore.reserve

    def simultaneous(self, **kwargs):
        attempt = reserve(self, **kwargs)
        with lock:
            first_reservation = attempt.key_hash not in reserved
            reserved.add(attempt.key_hash)
        if first_reservation:
            barrier.wait(timeout=5)
        return attempt

    monkeypatch.setattr(CheckoutStore, "reserve", simultaneous)
    another = CheckoutService(
        CheckoutStore(checkout.store.path),
        settings(),
        sessions=sessions,
        customers=customers,
        now=lambda: NOW,
    )
    customers.check_locks = sessions.check_locks = False
    session_hash = hashlib.sha256(
        client.cookies.get("__Host-zt_session").encode()
    ).hexdigest()

    def start(service):
        token = str(uuid4())
        for _ in range(10):
            try:
                return service.start_owned(
                    customer_id=1,
                    kind="abonament",
                    filters=PackageFilters("28", "materialy"),
                    request_token=token,
                    session_hash=session_hash,
                )
            except CheckoutUnavailable:
                time.sleep(0.01)
        pytest.fail("Concurrent reservation did not become retryable")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(start, service) for service in (checkout, another)]
        assert all(
            future.result(timeout=10).startswith("https://checkout.stripe.com/")
            for future in futures
        )
    pay_and_reconcile(client, checkout, sessions)
    assert len(customers.calls) >= 2 and all(
        call == customers.calls[0] for call in customers.calls
    )


def test_unknown_customer_creation_keeps_shared_key_and_frozen_offer(first_customer):
    from dataclasses import replace

    from app.stripe_service import CheckoutOffer

    client, checkout, customers, sessions = first_customer
    first, second = prepare(client), prepare(client)
    customers.error = stripe.APIConnectionError("synthetic unknown outcome")
    assert client.post("/zamowienie/checkout", data=first).status_code == 503
    assert client.post("/zamowienie/checkout", data=second).status_code == 503
    assert sessions.calls == []
    customers.error = None
    checkout.settings = replace(
        checkout.settings, subscription_offer=CheckoutOffer(99999, "year")
    )
    for data in (first, second):
        assert (
            client.post(
                "/zamowienie/checkout", data=data, follow_redirects=False
            ).status_code
            == 303
        )
    assert all(call == customers.calls[0] for call in customers.calls)
    assert all(
        params["line_items"][0]["price_data"]["unit_amount"] == 23456
        for params, _ in sessions.calls
    )
    pay_and_reconcile(client, checkout, sessions)


@pytest.mark.parametrize("change", ["email", "revoke", "expire", "mapping"])
def test_customer_creation_rechecks_identity_before_binding_or_checkout(
    first_customer, change
):
    client, checkout, customers, sessions = first_customer
    data = prepare(client)

    def invalidate():
        with sqlite3.connect(checkout.store.path) as conn:
            if change == "email":
                conn.execute("UPDATE b2b_customers SET email='changed@example.test'")
            elif change == "revoke":
                conn.execute("DELETE FROM auth_sessions")
            elif change == "expire":
                conn.execute(
                    "UPDATE auth_sessions SET expires_at=?", (int(NOW.timestamp()),)
                )
            else:
                conn.execute("UPDATE b2b_customers SET stripe_customer_id='cus_other'")

    customers.after_create = invalidate
    result = client.post("/zamowienie/checkout", data=data, follow_redirects=False)
    assert result.status_code == (409 if change == "mapping" else 401)
    assert sessions.calls == []
    with sqlite3.connect(checkout.store.path) as conn:
        assert conn.execute("SELECT stripe_customer_id FROM b2b_customers").fetchone()[
            0
        ] == ("cus_other" if change == "mapping" else None)


def test_customer_created_but_binding_save_failed_retries_same_key(
    first_customer, monkeypatch
):
    client, checkout, customers, sessions = first_customer
    data = prepare(client)
    bind = checkout.store.bind_checkout_customer
    with monkeypatch.context() as temporary:

        def unavailable(*args, **kwargs):
            raise CheckoutUnavailable

        temporary.setattr(checkout.store, "bind_checkout_customer", unavailable)
        assert client.post("/zamowienie/checkout", data=data).status_code == 503
    assert sessions.calls == []
    assert (
        client.post(
            "/zamowienie/checkout", data=data, follow_redirects=False
        ).status_code
        == 303
    )
    assert len(customers.calls) == 2 and customers.calls[0] == customers.calls[1]
    assert checkout.store.bind_checkout_customer == bind


@pytest.mark.parametrize(
    "change",
    [
        {"id": "cus_invalid!"},
        {"object": "subscription"},
        {"livemode": True},
        {"metadata": {"customer_id": "2"}},
    ],
)
def test_unverified_customer_response_never_binds_or_opens_checkout(
    first_customer, monkeypatch, change
):
    client, checkout, customers, sessions = first_customer
    data = prepare(client)
    create = customers.create
    monkeypatch.setattr(customers, "create", lambda *args: {**create(*args), **change})
    assert client.post("/zamowienie/checkout", data=data).status_code == 503
    assert sessions.calls == []
    with sqlite3.connect(checkout.store.path) as conn:
        assert (
            conn.execute("SELECT stripe_customer_id FROM b2b_customers").fetchone()[0]
            is None
        )


@pytest.mark.parametrize("delay_hours", [0, 2])
@pytest.mark.parametrize("missing_link", [False, True])
def test_legacy_reservation_appearing_during_customer_io_keeps_original_owned_retry(
    first_customer, missing_link, delay_hours
):
    client, checkout, customers, sessions = first_customer
    legacy_token = str(uuid4())

    def legacy_reserved():
        checkout.start(
            email="person@example.test",
            kind="abonament",
            filters=PackageFilters("28", "materialy"),
            request_token=legacy_token,
        )

    customers.after_create = legacy_reserved
    data = prepare(client)
    assert (
        client.post(
            "/zamowienie/checkout", data=data, follow_redirects=False
        ).status_code
        == 409
    )
    assert len(customers.calls) == len(sessions.calls) == 1
    with sqlite3.connect(checkout.store.path) as conn:
        assert (
            conn.execute("SELECT stripe_customer_id FROM b2b_customers").fetchone()[0]
            is None
        )
    customers.after_create = None
    checkout.clock.advance(hours=delay_hours)
    params = sessions.values["2"]
    payment = PaidCheckout(
        params["metadata"]["checkout_key"],
        "cs_test_order2",
        "2",
        "1",
        "subscription",
        "pln",
        23456,
        int(NOW.timestamp()),
        "cus_order2",
        "sub_order2",
    )
    PaymentStore(checkout.store.path).record_event(
        event_id="evt_paid2",
        event_type="checkout.session.completed",
        completion=payment,
        now=checkout.clock.now_utc(),
    )
    if missing_link:
        with sqlite3.connect(checkout.store.path) as conn:
            conn.execute("DELETE FROM checkout_billing_links")
    assert (
        client.post(
            "/auth/billing-link", json={"request_token": legacy_token}
        ).status_code
        == 200
    )
    # A two-hour delay expires the reserved new attempt as well. A valid retry
    # stays within its existing lifetime, while legacy recovery remains usable.
    if delay_hours:
        assert (
            client.post(
                "/zamowienie/checkout", data=data, follow_redirects=False
            ).status_code
            == 409
        )
    else:
        assert (
            client.post(
                "/zamowienie/checkout", data=data, follow_redirects=False
            ).status_code
            == 303
        )
        assert (
            sessions.calls[-1][0]["customer"] == "cus_order2"
            and len(customers.calls) == 1
        )


@pytest.mark.parametrize("delay_hours", [0, 2])
@pytest.mark.parametrize("missing_link", [False, True])
def test_pending_legacy_payment_after_new_submit_preserves_both_retries(
    first_customer, missing_link, delay_hours
):
    client, checkout, customers, sessions = first_customer
    legacy_token = str(uuid4())
    checkout.start(
        email="person@example.test",
        kind="abonament",
        filters=PackageFilters("28", "materialy"),
        request_token=legacy_token,
    )
    key = sessions.values["1"]["metadata"]["checkout_key"]
    payment = PaidCheckout(
        key,
        "cs_test_order1",
        "1",
        "1",
        "subscription",
        "pln",
        23456,
        int(NOW.timestamp()),
        "cus_order1",
        "sub_order1",
    )

    def legacy_paid():
        PaymentStore(checkout.store.path).record_event(
            event_id="evt_paid1",
            event_type="checkout.session.completed",
            completion=payment,
            now=checkout.clock.now_utc(),
        )
        if missing_link:
            with sqlite3.connect(checkout.store.path) as conn:
                conn.execute("DELETE FROM checkout_billing_links")

    checkout.clock.advance(hours=delay_hours)
    data = prepare(client)
    assert (
        client.post(
            "/zamowienie/checkout", data=data, follow_redirects=False
        ).status_code
        == 409
    )
    assert customers.calls == [] and len(sessions.calls) == 1
    legacy_paid()
    with sqlite3.connect(checkout.store.path) as conn:
        assert (
            conn.execute("SELECT stripe_customer_id FROM b2b_customers").fetchone()[0]
            is None
        )
    assert (
        client.post(
            "/auth/billing-link", json={"request_token": legacy_token}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/zamowienie/checkout", data=data, follow_redirects=False
        ).status_code
        == 303
    )
    assert sessions.calls[-1][0]["customer"] == "cus_order1" and customers.calls == []


@pytest.mark.parametrize("missing_link", [False, True])
def test_paid_legacy_recovery_precedes_new_customer_and_preserves_native_retry(
    first_customer, missing_link
):
    from tests.test_auth_navigation import Fields

    client, checkout, customers, sessions = first_customer
    legacy_token = str(uuid4())
    checkout.start(
        email="person@example.test",
        kind="abonament",
        filters=PackageFilters("28", "materialy"),
        request_token=legacy_token,
    )
    key = sessions.values["1"]["metadata"]["checkout_key"]
    payment = PaidCheckout(
        key,
        "cs_test_order1",
        "1",
        "1",
        "subscription",
        "pln",
        23456,
        int(NOW.timestamp()),
        "cus_order1",
        "sub_order1",
    )
    PaymentStore(checkout.store.path).record_event(
        event_id="evt_paid1",
        event_type="checkout.session.completed",
        completion=payment,
        now=NOW,
    )
    if missing_link:
        with sqlite3.connect(checkout.store.path) as conn:
            conn.execute("DELETE FROM checkout_billing_links")
    data = prepare(client)
    blocked = client.post("/zamowienie/checkout", data=data, follow_redirects=False)
    assert blocked.status_code == 409
    assert "Potwierdź rozliczenia dotychczasowego abonamentu" in blocked.text
    assert 'href="/panel" target="_blank"' in blocked.text
    fields = Fields(blocked.text).values
    assert (
        fields["request_token"] == data["request_token"]
        and fields["filters"] == data["filters"]
    )
    assert customers.calls == [] and len(sessions.calls) == 1
    assert (
        client.post(
            "/auth/billing-link", json={"request_token": legacy_token}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/zamowienie/checkout", data=data, follow_redirects=False
        ).status_code
        == 303
    )
    assert sessions.calls[-1][0]["customer"] == "cus_order1" and customers.calls == []
    params = sessions.values["2"]
    completion = PaidCheckout(
        params["metadata"]["checkout_key"],
        "cs_test_order2",
        "2",
        "1",
        "subscription",
        "pln",
        23456,
        int(NOW.timestamp()),
        "cus_order1",
        "sub_order2",
    )
    PaymentStore(checkout.store.path).record_event(
        event_id="evt_paid2",
        event_type="checkout.session.completed",
        completion=completion,
        now=NOW,
    )
    assert (
        client.post("/panel/rozliczenia/2", follow_redirects=False).status_code == 303
    )
    assert (
        client.post(
            "/auth/billing-link", json={"request_token": legacy_token}
        ).status_code
        == 200
    )
    with sqlite3.connect(checkout.store.path) as conn:
        assert conn.execute(
            "SELECT count(*),count(DISTINCT stripe_customer_id) FROM b2b_subscriptions"
        ).fetchone() == (2, 1)
        assert conn.execute("SELECT count(*) FROM paid_package_jobs").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT count(*) FROM stripe_events WHERE outcome='paid'"
            ).fetchone()[0]
            == 2
        )
