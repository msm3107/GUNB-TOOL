"""Authenticated native Checkout uses account identity and retries the same attempt."""
import json
import sqlite3
from uuid import UUID

import pytest
import stripe
from fastapi.testclient import TestClient

from app.auth import AuthService
from app.config import WebConfig
from app.main import create_app
from app.stripe_service import CheckoutService
from core.checkout_store import CheckoutStore
from tests.test_auth_billing import login
from tests.test_auth_navigation import Fields
from tests.test_auth_service import NOW, Sender
from tests.test_auth_service import settings as auth_settings
from tests.test_auth_store import (
    auth_store_fixture as _auth_store_fixture,  # noqa: F401
)
from tests.test_stripe_service import URL, Sessions, settings

ORIGIN = "https://example.test"
FILTERS = {"wojewodztwo": "28", "branza": "materialy", "powiaty": "2814", "kategorie": "komercyjna", "promien_km": ""}


@pytest.fixture
def owned_http(auth_store):
    fake = Sessions(auth_store.path)
    checkout = CheckoutService(CheckoutStore(auth_store.path), settings(), sessions=fake, now=lambda: NOW)
    sender = Sender(auth_store.path)
    auth = AuthService(auth_store, auth_settings(), sender=sender, now=lambda: NOW)
    config = WebConfig(database_path=auth_store.path, checkout=settings(), auth=auth_settings())
    with TestClient(create_app(config, checkout_service=checkout, auth_service=auth), base_url=ORIGIN, headers={"Origin": ORIGIN}) as client:
        yield client, checkout, fake, auth, sender


def prepare(client, **changes):
    result = client.post("/zamowienie/podsumowanie", data={**FILTERS, **changes})
    assert result.status_code == 200
    fields = Fields(result.text).values
    assert UUID(fields["request_token"]).version == 4
    return {"kind": "abonament", "request_token": fields["request_token"], "filters": fields["filters"]}


def test_native_timeout_retry_and_double_submit_keep_one_order(owned_http):
    client, service, fake, _, sender = owned_http
    login(client, sender)
    data = prepare(client)
    assert json.loads(data["filters"])["powiaty"] == ["2814"]
    fake.error = stripe.APIConnectionError("SYNTHETIC-SECRET-ERROR")
    failed = client.post("/zamowienie/checkout", data=data)
    assert failed.status_code == 503
    assert "SYNTHETIC-SECRET" not in failed.text
    assert Fields(failed.text).values["request_token"] == data["request_token"]
    assert Fields(failed.text).values["filters"] == data["filters"]
    assert "checkout.stripe.com" in failed.headers["content-security-policy"]
    fake.error = None
    for _ in range(2):
        result = client.post("/zamowienie/checkout", data=data, follow_redirects=False)
        assert result.status_code == 303 and result.headers["location"] == URL
    assert len(fake.calls) == 2
    assert fake.calls[0][1] == fake.calls[1][1]
    with sqlite3.connect(service.store.path) as conn:
        assert conn.execute("SELECT count(*) FROM b2b_orders").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM b2b_deliveries").fetchone()[0] == 0
        marker = json.loads(conn.execute("SELECT offer_json FROM checkout_attempts").fetchone()[0])
        assert marker["authenticated_customer_id"] == 1


def test_expired_web_session_keeps_native_attempt_for_login_in_new_tab(owned_http):
    client, _, fake, _, sender = owned_http
    login(client, sender)
    data = prepare(client)
    client.cookies.clear()
    rejected = client.post("/zamowienie/checkout", data=data)
    assert rejected.status_code == 401
    assert Fields(rejected.text).values["request_token"] == data["request_token"]
    assert "nowa karta" in rejected.text
    assert fake.calls == []
    login(client, sender)
    assert client.post("/zamowienie/checkout", data=data, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("extra", [{"email": "other@example.test"}, {"customer_id": 2}, {"amount": 1}])
def test_authenticated_checkout_rejects_client_identity_and_price(owned_http, extra):
    client, _, fake, _, sender = owned_http
    login(client, sender)
    data = prepare(client)
    data["filters"] = json.loads(data["filters"])
    result = client.post("/zamowienie/checkout", json={**data, **extra})
    assert result.status_code == 422
    assert not fake.calls


def test_native_prepare_requires_login_and_same_origin(owned_http):
    client, _, fake, _, sender = owned_http
    anonymous = client.post("/zamowienie/podsumowanie", data=FILTERS)
    assert anonymous.status_code == 401
    login(client, sender)
    assert client.post("/zamowienie/podsumowanie", data=FILTERS, headers={"Origin": "https://evil.test"}).status_code == 403
    assert not fake.calls


def test_other_account_cannot_resume_owned_attempt(owned_http):
    client, service, fake, _, sender = owned_http
    login(client, sender)
    data = prepare(client)
    fake.error = stripe.APIConnectionError("timeout")
    assert client.post("/zamowienie/checkout", data=data).status_code == 503
    with sqlite3.connect(service.store.path) as conn:
        conn.execute("UPDATE b2b_customers SET email='changed@example.test' WHERE customer_id=1")
        conn.execute("INSERT INTO b2b_customers(email) VALUES('person@example.test')")
    client.cookies.clear()
    login(client, sender)
    fake.error = None
    assert client.post("/zamowienie/checkout", data=data).status_code == 409
    assert len(fake.calls) == 1


def test_configurator_exposes_native_test_action_only_when_enabled(owned_http):
    client, _, _, _, sender = owned_http
    login(client, sender)
    assert "Przejdź do testowego zamówienia" in client.get("/zamowienie").text
    assert client.get("/zamowienie").headers["referrer-policy"] == "same-origin"
    with TestClient(create_app(WebConfig())) as disabled:
        assert "Przejdź do testowego zamówienia" not in disabled.get("/zamowienie").text
        assert disabled.post("/zamowienie/podsumowanie", data=FILTERS).status_code == 503


def test_created_session_but_failed_local_save_retries_same_provider_key(owned_http, monkeypatch):
    from core.checkout_store import CheckoutUnavailable
    client, service, fake, _, sender = owned_http
    login(client, sender)
    data = prepare(client)
    complete = service.store.complete
    monkeypatch.setattr(service.store, "complete", lambda *args: (_ for _ in ()).throw(CheckoutUnavailable()))
    failed = client.post("/zamowienie/checkout", data=data)
    assert failed.status_code == 503
    assert Fields(failed.text).values["request_token"] == data["request_token"]
    monkeypatch.setattr(service.store, "complete", complete)
    assert client.post("/zamowienie/checkout", data=data, follow_redirects=False).status_code == 303
    assert len(fake.calls) == 2 and fake.calls[0][1] == fake.calls[1][1]
    with sqlite3.connect(service.store.path) as conn:
        assert conn.execute("SELECT count(*) FROM b2b_orders").fetchone()[0] == 1


def test_account_changed_between_lookup_and_reserve_never_calls_provider(owned_http, monkeypatch):
    client, service, fake, _, sender = owned_http
    login(client, sender)
    data = prepare(client)
    original = service.store.owner_email
    def changed(customer_id):
        email = original(customer_id)
        with sqlite3.connect(service.store.path) as conn:
            conn.execute("UPDATE b2b_customers SET email='changed@example.test' WHERE customer_id=?", (customer_id,))
        return email
    monkeypatch.setattr(service.store, "owner_email", changed)
    assert client.post("/zamowienie/checkout", data=data).status_code == 409
    assert not fake.calls

