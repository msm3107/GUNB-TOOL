"""New authenticated orders reconcile by owned order ID; legacy proof stays separate."""
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthService
from app.config import WebConfig
from app.main import create_app
from app.stripe_service import CheckoutService
from app.subscription_service import SubscriptionService
from core.checkout_store import CheckoutStore, CheckoutUnavailable
from core.package_selection import PackageFilters
from core.payment_store import PaidCheckout, PaymentStore
from core.subscription_store import SubscriptionNotice, SubscriptionStore
from tests.test_auth_billing import UUID, login
from tests.test_auth_service import NOW, Sender
from tests.test_auth_service import settings as auth_settings
from tests.test_auth_store import (
    auth_store_fixture as _auth_store_fixture,  # noqa: F401
)
from tests.test_stripe_service import Sessions, settings
from tests.test_subscription_service import Subscriptions

ORIGIN = "https://example.test"


@pytest.fixture
def owned_billing(auth_store):
    checkout = CheckoutService(CheckoutStore(auth_store.path), settings(), sessions=Sessions(auth_store.path), now=lambda: NOW)
    checkout.start_owned(customer_id=1, kind="abonament", filters=PackageFilters("28", "materialy"), request_token=UUID)
    key = hashlib.sha256(UUID.encode()).hexdigest()
    PaymentStore(auth_store.path).record_event(event_id="evt_paid", event_type="checkout.session.completed", completion=PaidCheckout(key, "cs_test_synthetic", "1", "1", "subscription", "pln", 23456, int(NOW.timestamp()), "cus_synthetic", "sub_synthetic"), now=NOW)
    notice = SubscriptionNotice("sub_synthetic", "cus_synthetic", key, "1", "1")
    store = SubscriptionStore(auth_store.path)
    api = Subscriptions(store, notice)
    subscriptions = SubscriptionService(store, settings(), subscriptions=api, now=lambda: NOW)
    sender = Sender(auth_store.path)
    auth = AuthService(auth_store, auth_settings(), sender=sender, now=lambda: NOW)
    config = WebConfig(database_path=auth_store.path, auth=auth_settings(), checkout=settings())
    with TestClient(create_app(config, auth_service=auth, subscription_service=subscriptions), base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        login(client, sender)
        yield client, auth, store, api


def test_owned_paid_order_reconciles_without_checkout_uuid_and_retries(owned_billing):
    client, _, store, api = owned_billing
    assert '/panel/rozliczenia/1' in client.get("/panel").text
    for _ in range(2):
        result = client.post("/panel/rozliczenia/1", follow_redirects=False)
        assert result.status_code == 303 and result.headers["location"] == "/panel?notice=billing"
    assert api.calls == 2
    assert store.has_subscription_access(1, now=NOW)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM b2b_orders").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM paid_package_jobs").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM b2b_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM stripe_events WHERE event_id=\'evt_paid\'").fetchone()[0] == 1


def test_legacy_same_email_order_needs_original_proof(owned_billing):
    client, _, store, api = owned_billing
    with sqlite3.connect(store.path) as conn:
        offer = json.loads(conn.execute("SELECT offer_json FROM checkout_attempts").fetchone()[0])
        offer.pop("authenticated_customer_id")
        conn.execute("UPDATE checkout_attempts SET offer_json=?", (json.dumps(offer),))
    assert '/panel/rozliczenia/1' not in client.get("/panel").text
    assert client.post("/panel/rozliczenia/1").status_code == 403
    assert api.calls == 0
    assert client.post("/auth/billing-link", json={"request_token": UUID}).status_code == 200
    assert store.has_subscription_access(1, now=NOW)


@pytest.mark.parametrize("mutation", ["UPDATE b2b_orders SET status_platnosci=\'failed\'", "INSERT INTO b2b_customers(customer_id,email) VALUES(2,\'other@example.test\'); UPDATE b2b_orders SET customer_id=2"])
def test_foreign_or_unpaid_order_never_reads_provider(owned_billing, mutation):
    client, _, store, api = owned_billing
    with sqlite3.connect(store.path) as conn:
        conn.executescript(mutation)
    assert client.post("/panel/rozliczenia/1").status_code == 403
    assert api.calls == 0
    assert not store.has_subscription_access(1, now=NOW)


def test_payment_revoked_during_provider_read_never_grants_access(owned_billing, monkeypatch):
    client, _, store, api = owned_billing
    retrieve = api.retrieve
    def revoked(*args, **kwargs):
        result = retrieve(*args, **kwargs)
        with sqlite3.connect(store.path) as conn:
            conn.execute("UPDATE b2b_orders SET status_platnosci=\'failed\'")
        return result
    monkeypatch.setattr(api, "retrieve", revoked)
    assert client.post("/panel/rozliczenia/1").status_code == 403
    assert not store.has_subscription_access(1, now=NOW)


def test_failed_reconcile_and_snapshot_keep_order_retry_without_uuid(owned_billing, monkeypatch):
    from core.customer_dashboard import CustomerDashboardStore
    client, _, store, api = owned_billing
    api.failure = True
    with monkeypatch.context() as temporary:
        temporary.setattr(CustomerDashboardStore, "snapshot", lambda *a, **k: (_ for _ in ()).throw(CheckoutUnavailable()))
        failed = client.post("/panel/rozliczenia/1")
    assert failed.status_code == 503
    assert '/panel/rozliczenia/1' in failed.text
    assert "synthetic-private" not in failed.text
    api.failure = False
    assert client.post("/panel/rozliczenia/1", follow_redirects=False).status_code == 303
    assert store.has_subscription_access(1, now=NOW)


def test_owned_reconcile_requires_same_origin_and_empty_body(owned_billing):
    client, _, _, api = owned_billing
    assert client.post("/panel/rozliczenia/1", headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/panel/rozliczenia/1", data={"customer_id": "1"}).status_code == 400
    assert api.calls == 0


def test_existing_customer_checkout_recovery_uses_trusted_id_not_missing_email(owned_billing):
    from app.identity import AuthenticatedCustomer
    from tests.test_auth_legacy_billing import checkout_snapshot
    _, auth, store, api = owned_billing
    with sqlite3.connect(store.path) as conn:
        offer = json.loads(conn.execute("SELECT offer_json FROM checkout_attempts").fetchone()[0])
        offer["authenticated_stripe_customer_id"] = "cus_synthetic"
        conn.execute("UPDATE checkout_attempts SET offer_json=?", (json.dumps(offer),))
        conn.execute("UPDATE b2b_customers SET stripe_customer_id='cus_synthetic' WHERE customer_id=1")
        conn.execute("DELETE FROM checkout_billing_links")
    value = checkout_snapshot()
    value.update(id="cs_test_synthetic", customer_email=None)
    class Sessions:
        def retrieve(self, session_id):
            assert session_id == "cs_test_synthetic"
            return value
    service = SubscriptionService(store, settings(), subscriptions=api, checkout_sessions=Sessions(), now=lambda: NOW)
    auth.bind_order(AuthenticatedCustomer(1), 1, service)
    assert store.has_subscription_access(1, now=NOW)

