import hashlib
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.auth import AuthService
from app.config import WebConfig
from app.main import create_app
from app.portal_service import PortalService
from app.stripe_service import CheckoutService
from app.subscription_service import SubscriptionService
from core.checkout_store import CheckoutStore
from core.package_selection import PackageFilters
from core.payment_store import PaidCheckout, PaymentStore
from core.subscription_store import SubscriptionNotice, SubscriptionStore
from tests.test_auth_service import Sender
from tests.test_auth_service import settings as auth_settings
from tests.test_auth_store import (
    auth_store_fixture as _auth_store_fixture,  # noqa: F401
)
from tests.test_payment_store import NOW
from tests.test_stripe_portal import Sessions as PortalSessions
from tests.test_stripe_service import settings
from tests.test_subscription_service import Subscriptions

UUID = "12345678-1234-4234-8234-123456789abc"


@pytest.fixture
def billing_http(auth_store):
    class CheckoutSessions:
        def create(self, params, options):
            return {
                "id": "cs_test_synthetic",
                "url": "https://checkout.stripe.com/test",
                "livemode": False,
            }

    checkout = CheckoutService(
        CheckoutStore(auth_store.path),
        settings(),
        sessions=CheckoutSessions(),
        now=lambda: NOW,
    )
    checkout.start(
        email="person@example.test",
        kind="abonament",
        filters=PackageFilters("28", "materialy"),
        request_token=UUID,
    )
    key = hashlib.sha256(UUID.encode()).hexdigest()
    paid = PaidCheckout(
        key,
        "cs_test_synthetic",
        "1",
        "1",
        "subscription",
        "pln",
        23456,
        int(NOW.timestamp()),
        "cus_synthetic",
        "sub_synthetic",
    )
    PaymentStore(auth_store.path).record_event(
        event_id="evt_paid",
        event_type="checkout.session.completed",
        completion=paid,
        now=NOW,
    )
    notice = SubscriptionNotice("sub_synthetic", "cus_synthetic", key, "1", "1")
    subs_store = SubscriptionStore(auth_store.path)
    api = Subscriptions(subs_store, notice)
    subscriptions = SubscriptionService(
        subs_store, settings(), subscriptions=api, now=lambda: NOW
    )
    sender = Sender(auth_store.path)
    auth = AuthService(auth_store, auth_settings(), sender=sender, now=lambda: NOW)
    portal_api = PortalSessions(subs_store)
    config = WebConfig(
        database_path=auth_store.path, auth=auth_settings(), checkout=settings()
    )
    with TestClient(
        create_app(
            config,
            auth_service=auth,
            subscription_service=subscriptions,
            portal_service=PortalService(subs_store, settings(), sessions=portal_api),
        ),
        base_url="https://example.test",
    ) as client:
        yield client, auth, sender, subs_store, api, portal_api


def login(client, sender):
    client.get("/logowanie")
    client.post(
        "/auth/magic-link",
        json={"email": "person@example.test"},
        headers={"Origin": "https://example.test"},
    )
    raw = sender.messages[-1][2].split("/auth/verify#")[1].split()[0]
    assert (
        client.post(
            "/auth/verify",
            data={"token": raw},
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        ).status_code
        == 303
    )


def test_real_session_and_order_capability_link_then_reconcile_and_open_own_portal(
    billing_http,
):
    client, _, sender, store, api, portal = billing_http
    assert (
        client.post(
            "/auth/billing-link",
            json={"request_token": UUID},
            headers={"Origin": "https://example.test"},
        ).status_code
        == 401
    )
    login(client, sender)
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT stripe_customer_id FROM b2b_customers").fetchone() == (
            None,
        )
    assert (
        client.post(
            "/panel/portal", headers={"Origin": "https://example.test"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/auth/billing-link",
            json={"request_token": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
            headers={"Origin": "https://example.test"},
        ).status_code
        == 409
    )
    response = client.post(
        "/auth/billing-link",
        json={"request_token": UUID},
        headers={"Origin": "https://example.test"},
    )
    assert response.status_code == 200 and api.calls == 1
    assert store.has_subscription_access(1, now=NOW)
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT event_type FROM stripe_events WHERE event_id LIKE 'local_reconcile_%'"
        ).fetchone() == ("local.subscription.reconciled",)
    response = client.post(
        "/panel/portal",
        headers={"Origin": "https://example.test"},
        follow_redirects=False,
    )
    assert response.status_code == 303 and len(portal.calls) == 1
    assert (
        client.post(
            "/auth/logout",
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/panel/portal", headers={"Origin": "https://example.test"}
        ).status_code
        == 401
    )


def test_reconcile_failure_does_not_grant_fresh_access_and_same_proof_can_retry(
    billing_http,
):
    client, _, sender, store, api, _ = billing_http
    login(client, sender)
    api.failure = True
    assert (
        client.post(
            "/auth/billing-link",
            json={"request_token": UUID},
            headers={"Origin": "https://example.test"},
        ).status_code
        == 503
    )
    assert not store.has_subscription_access(1, now=NOW)
    api.failure = False
    assert (
        client.post(
            "/auth/billing-link",
            json={"request_token": UUID},
            headers={"Origin": "https://example.test"},
        ).status_code
        == 200
    )
    assert store.has_subscription_access(1, now=NOW)
