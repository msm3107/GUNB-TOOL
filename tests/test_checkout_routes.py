import sqlite3
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.config import WebConfig
from app.main import create_app
from tests.test_stripe_service import URL, settings


def payload(**updates):
    values = {"email": "person@example.test", "kind": "jednorazowy", "request_token": str(uuid4()),
              "filters": {"wojewodztwo": "28", "branza": "materialy"}}
    values.update(updates)
    return values


def test_default_disabled_never_opens_database_or_network(monkeypatch):
    import stripe

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled Checkout cannot touch DB or provider")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(stripe.StripeClient, "__init__", forbidden)
    with TestClient(create_app(WebConfig())) as client:
        response = client.post("/zamowienie/checkout", json=payload())
        assert response.status_code == 503 and "location" not in response.headers
        assert response.headers["cache-control"] == "no-store"


@pytest.fixture
def http(service):
    instance, fake = service
    config = WebConfig(database_path=instance.store.path, checkout=settings())
    with TestClient(create_app(config, checkout_service=instance), base_url="https://example.test",
                    headers={"Origin": "https://example.test"}) as client:
        yield client, instance, fake


@pytest.fixture
def service(tmp_path):
    from app.stripe_service import CheckoutService
    from core.checkout_store import CheckoutStore
    from core.storage import LeadRepository
    from tests.test_stripe_service import NOW, Sessions

    path = tmp_path / "synthetic.sqlite"
    with LeadRepository(path):
        pass
    fake = Sessions(path)
    return CheckoutService(CheckoutStore(path), settings(), sessions=fake, now=lambda: NOW), fake


def test_test_checkout_redirect_and_success_never_confirms_or_fulfills(http):
    client, instance, fake = http
    response = client.post("/zamowienie/checkout", json=payload(), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == URL
    assert response.headers["cache-control"] == "no-store"
    returned = client.get('/zamowienie/sukces?paid=true&session_id=cs_live_forged&order_id=1')
    assert returned.status_code == 200 and "nie potwierdza płatności" in returned.text
    assert "cs_live_forged" not in returned.text and "person@example.test" not in returned.text
    assert 'class="brand"' in returned.text or "Żółta Tablica" in returned.text
    with sqlite3.connect(instance.store.path) as c:
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone()[0] == "pending"
        assert c.execute("SELECT count(*) FROM b2b_deliveries").fetchone()[0] == 0
    assert len(fake.calls) == 1


@pytest.mark.parametrize("changes", ({"amount": 1}, {"customer_id": 1}, {"email": "bad\n@example.test"},
    {"kind": "free"}, {"request_token": "predictable"}, {"filters": {"wojewodztwo": "28", "branza": "materialy", "lat": 53}}))
def test_untrusted_price_identity_and_bad_inputs_reject_before_network(http, changes):
    client, instance, fake = http
    assert client.post("/zamowienie/checkout", json=payload(**changes)).status_code == 422
    assert fake.calls == []
    with sqlite3.connect(instance.store.path) as c:
        assert c.execute("SELECT count(*) FROM b2b_orders").fetchone()[0] == 0


def test_body_origin_and_format_are_bounded_before_writes(http):
    client, _, fake = http
    assert client.post("/zamowienie/checkout", json=payload(), headers={"Origin": "https://evil.example.test"}).status_code == 403
    assert client.post("/zamowienie/checkout", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert client.post("/zamowienie/checkout", content=b"x" * 8193, headers={"Content-Type": "application/json"}).status_code == 413
    assert fake.calls == []


def test_provider_failure_is_generic_and_token_retry_works(http):
    import stripe

    client, _, fake = http
    data = payload()
    fake.error = stripe.APIConnectionError("SECRET-SYNTHETIC-FAILURE")
    response = client.post("/zamowienie/checkout", json=data)
    assert response.status_code == 503 and "SECRET-SYNTHETIC" not in response.text
    assert "content-security-policy" in response.headers
    fake.error = None
    assert client.post("/zamowienie/checkout", json=data, follow_redirects=False).status_code == 303


def test_env_test_config_explicit_and_never_live():
    config = WebConfig.from_env({"GUNB_WEB_CHECKOUT_MODE": "test", "GUNB_WEB_STRIPE_SECRET_KEY": "sk_test_synthetic123",
        "GUNB_WEB_PUBLIC_ORIGIN": "http://127.0.0.1:8000", "GUNB_WEB_STRIPE_TEST_PACKAGE_GROSZE": "12345",
        "GUNB_WEB_STRIPE_TEST_SUBSCRIPTION_GROSZE": "23456", "GUNB_WEB_STRIPE_TEST_INTERVAL": "month"})
    assert config.checkout.test_mode and config.checkout.package_offer.amount_grosze == 12345
    assert "sk_test_synthetic123" not in repr(config)
    with pytest.raises(ValueError):
        WebConfig.from_env({"GUNB_WEB_CHECKOUT_MODE": "live"})
