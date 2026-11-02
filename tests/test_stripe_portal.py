import json
import sqlite3

import pytest
import responses
import stripe
from fastapi.testclient import TestClient

from app.config import WebConfig
from app.identity import AuthenticatedCustomer
from app.main import create_app
from app.portal_service import PortalForbidden, PortalService
from app.stripe_service import CheckoutBusy
from core.checkout_store import CheckoutUnavailable
from core.instance import database_replacement_lock
from tests.test_payment_store import payment_fixture as _payment_fixture  # noqa: F401
from tests.test_stripe_service import settings
from tests.test_subscription_store import (
    subscription_fixture as _subscription_fixture,  # noqa: F401
)

URL = "https://billing.stripe.com/p/session/test_synthetic"


class Sessions:
    def __init__(self, store):
        self.store = store
        self.calls = []
        self.fail = False
        self.mutate = False
        self.value = {
            "id": "bps_synthetic",
            "object": "billing_portal.session",
            "customer": "cus_synthetic",
            "livemode": False,
            "url": URL,
        }

    def create(self, params):
        self.calls.append(params)
        with (
            database_replacement_lock(self.store.path),
            sqlite3.connect(self.store.path, timeout=0.1) as c,
        ):
            c.execute("BEGIN IMMEDIATE")
            if self.mutate:
                c.execute("UPDATE b2b_customers SET stripe_customer_id=NULL")
        if self.fail:
            raise stripe.APIConnectionError("synthetic-private")
        return self.value


@pytest.fixture
def portal(subscription):
    store, _ = subscription
    fake = Sessions(store)
    return PortalService(store, settings(), sessions=fake), fake


def test_owner_gets_only_own_customer_and_server_return_url(portal):
    service, fake = portal
    assert service.start(AuthenticatedCustomer(1)) == URL
    assert fake.calls == [
        {"customer": "cus_synthetic", "return_url": "https://example.test/zamowienie"}
    ]
    with sqlite3.connect(service.store.path) as c:
        c.execute("INSERT INTO b2b_customers(email) VALUES('other@example.test')")
    with pytest.raises(PortalForbidden):
        service.start(AuthenticatedCustomer(2))
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"url": "https://billing.stripe.com.evil.test/x"},
        {"url": "https://user@billing.stripe.com/x"},
        {"url": "http://billing.stripe.com/x"},
        {"url": URL + "\r\nX:1"},
        {"url": "https://billing.stripe.com:444/x"},
        {"livemode": True},
        {"customer": "cus_other"},
        {"id": "invalid"},
        {"object": "checkout.session"},
    ],
)
def test_unsafe_provider_response_never_redirects(portal, changes):
    service, fake = portal
    fake.value.update(changes)
    with pytest.raises(CheckoutUnavailable):
        service.start(AuthenticatedCustomer(1))


def test_network_error_and_mapping_change_fail_closed(portal):
    service, fake = portal
    fake.fail = True
    with pytest.raises(CheckoutUnavailable):
        service.start(AuthenticatedCustomer(1))
    fake.fail = False
    fake.mutate = True
    with pytest.raises(PortalForbidden):
        service.start(AuthenticatedCustomer(1))


def test_portal_rate_limit_bounds_provider_calls(portal):
    service, fake = portal
    for _ in range(30):
        assert service.start(AuthenticatedCustomer(1)) == URL
    with pytest.raises(CheckoutBusy):
        service.start(AuthenticatedCustomer(1))
    assert len(fake.calls) == 30


def test_real_sdk_portal_payload_and_headers_are_pinned(subscription):
    store, _ = subscription
    service = PortalService(store, settings())

    def callback(request):
        from urllib.parse import parse_qs

        assert parse_qs(request.body) == {
            "customer": ["cus_synthetic"],
            "return_url": ["https://example.test/zamowienie"],
        }
        assert request.headers["Stripe-Version"] == "2024-06-20"
        assert request.headers["Authorization"] == "Bearer sk_test_synthetic123"
        with (
            database_replacement_lock(store.path),
            sqlite3.connect(store.path, timeout=0.1) as c,
        ):
            c.execute("BEGIN IMMEDIATE")
        return (
            200,
            {"Content-Type": "application/json"},
            json.dumps(Sessions(store).value),
        )

    with responses.RequestsMock() as http:
        http.add_callback(
            responses.POST,
            "https://api.stripe.com/v1/billing_portal/sessions",
            callback=callback,
        )
        assert service.start(AuthenticatedCustomer(1)) == URL


def test_public_portal_denies_spoofed_ids_headers_and_cookies(portal, monkeypatch):
    service, fake = portal
    config = WebConfig(checkout=settings())

    def forbidden(*args, **kwargs):
        raise AssertionError("unauthenticated request touched DB")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with TestClient(create_app(config, portal_service=service)) as client:
        response = client.post(
            "/panel/portal?customer_id=1",
            json={"customer_id": 1, "stripe_customer_id": "cus_synthetic"},
            headers={
                "Origin": "https://example.test",
                "X-Customer-Id": "1",
                "Authorization": "Bearer fake",
                "Cookie": "customer_id=1;session=forged",
            },
        )
    assert response.status_code == 401 and not fake.calls


def test_trusted_identity_route_rejects_csrf_body_and_unknown_account(portal):
    service, fake = portal
    identity = AuthenticatedCustomer(1)

    def resolver(_request):
        return identity

    with TestClient(
        create_app(
            WebConfig(checkout=settings()),
            portal_service=service,
            identity_resolver=resolver,
        )
    ) as client:
        assert client.post("/panel/portal").status_code == 403
        assert (
            client.post(
                "/panel/portal", headers={"Origin": "https://evil.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/panel/portal",
                json={"customer_id": 2},
                headers={"Origin": "https://example.test"},
            ).status_code
            == 400
        )
        response = client.post(
            "/panel/portal",
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers["location"] == URL
        assert response.headers["cache-control"] == "no-store"
        identity = AuthenticatedCustomer(999)
        assert (
            client.post(
                "/panel/portal", headers={"Origin": "https://example.test"}
            ).status_code
            == 403
        )
    assert len(fake.calls) == 1


def test_default_disabled_does_not_construct_stripe_or_connect_db(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled portal touched dependencies")

    monkeypatch.setattr(stripe.StripeClient, "__init__", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    with TestClient(create_app(WebConfig())) as client:
        assert client.post("/panel/portal").status_code == 503


@pytest.mark.parametrize("value", [True, 0, -1, "1", None])
def test_identity_contract_rejects_non_database_identifiers(value):
    with pytest.raises(ValueError):
        AuthenticatedCustomer(value)
