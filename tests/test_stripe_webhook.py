import hashlib
import hmac
import json
import sqlite3
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import WebConfig
from app.main import create_app
from tests.test_payment_store import NOW
from tests.test_payment_store import payment_fixture as _payment_fixture  # noqa: F401
from tests.test_stripe_service import settings

SECRET = "whsec_synthetic_not_a_real_signing_secret"


def event(paid, **updates):
    result = {
        "id": "evt_synthetic",
        "object": "event",
        "api_version": "2024-06-20",
        "type": "checkout.session.completed",
        "livemode": False,
        "created": int(NOW.timestamp()),
        "data": {
            "object": {
                "id": paid.session_id,
                "object": "checkout.session",
                "livemode": False,
                "status": "complete",
                "payment_status": "paid",
                "currency": "pln",
                "amount_total": 12345,
                "amount_subtotal": 12345,
                "mode": "payment",
                "created": paid.session_created,
                "client_reference_id": paid.order_id,
                "metadata": {
                    "order_id": paid.order_id,
                    "customer_id": paid.customer_id,
                    "checkout_key": paid.key_hash,
                },
                "customer": "cus_synthetic",
                "payment_intent": "pi_synthetic",
                "subscription": None,
            }
        },
    }
    result.update(updates)
    return result


def signed(client, payload, *, secret=SECRET, stamp=None, raw=None):
    body = json.dumps(payload, indent=2).encode() if raw is None else raw
    stamp = int(time.time()) if stamp is None else stamp
    signature = hmac.new(
        secret.encode(), str(stamp).encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/api/webhook/stripe",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Stripe-Signature": f"t={stamp},v1={signature}",
        },
    )


@pytest.fixture
def client(payment, tmp_path):
    store, paid = payment
    config = WebConfig(
        database_path=store.path,
        checkout=settings(),
        webhook_secret=SECRET,
        test_package_dir=tmp_path / "private",
    )
    with TestClient(create_app(config)) as http:
        yield http, store, paid, config


def test_raw_signed_completion_generates_private_package_once(client):
    http, store, paid, config = client
    assert signed(http, event(paid)).status_code == 200
    assert signed(http, event(paid)).status_code == 200
    assert signed(http, event(paid, id="evt_another")).status_code == 200
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT count(*),min(state) FROM paid_package_jobs"
        ).fetchone() == (1, "ready")
        assert c.execute(
            "SELECT count(*),min(doreczono_utc) FROM b2b_deliveries"
        ).fetchone() == (1, None)
        assert c.execute("SELECT count(*) FROM stripe_events").fetchone() == (2,)
        assert c.execute(
            "SELECT stripe_customer_id,status_subskrypcji FROM b2b_customers"
        ).fetchone() == (None, "nieaktywna")
    assert len(list(config.test_package_dir.iterdir())) == 2
    assert http.get("/zamowienie/sukces?paid=true&order_id=1").status_code == 200


@pytest.mark.parametrize(
    "variant", ["forged", "stale", "future", "modified", "missing"]
)
def test_invalid_signature_never_touches_database(client, variant, monkeypatch):
    http, _, paid, _ = client

    def forbidden(*args, **kwargs):
        raise AssertionError("unverified input touched storage")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    if variant == "missing":
        response = http.post("/api/webhook/stripe", json=event(paid))
    elif variant == "modified":
        body = json.dumps(event(paid)).encode()
        stamp = int(time.time())
        digest = hmac.new(
            SECRET.encode(), str(stamp).encode() + b"." + body, hashlib.sha256
        ).hexdigest()
        response = http.post(
            "/api/webhook/stripe",
            content=body + b" ",
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": f"t={stamp},v1={digest}",
            },
        )
    else:
        response = signed(
            http,
            event(paid),
            secret="whsec_wrong" if variant == "forged" else SECRET,
            stamp=int(time.time()) + ({"stale": -301, "future": 301}.get(variant, 0)),
        )
    assert response.status_code == 400
    assert SECRET not in response.text and "traceback" not in response.text.lower()


@pytest.mark.parametrize(
    "change",
    [
        "live_event",
        "live_session",
        "account",
        "api",
        "wrong_amount",
        "wrong_customer",
        "wrong_ref",
        "bad_session",
        "float",
        "missing_metadata",
    ],
)
def test_signed_mismatches_never_mark_paid(client, change):
    http, store, paid, _ = client
    data = event(paid)
    session = data["data"]["object"]
    if change == "live_event":
        data["livemode"] = True
    if change == "live_session":
        session["livemode"] = True
    if change == "account":
        data["account"] = "acct_foreign"
    if change == "api":
        data["api_version"] = "2025-01-01"
    if change == "wrong_amount":
        session["amount_total"] = 1
    if change == "wrong_customer":
        session["metadata"]["customer_id"] = "2"
    if change == "wrong_ref":
        session["client_reference_id"] = "2"
    if change == "bad_session":
        session["id"] = "cs_live_bad"
    if change == "float":
        session["amount_total"] = 12345.0
    if change == "missing_metadata":
        session.pop("metadata")
    assert signed(http, data).status_code in (400, 409)
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone() == (
            "pending",
        )
        assert c.execute("SELECT count(*) FROM paid_package_jobs").fetchone() == (0,)


def test_unpaid_and_unsupported_are_ignored_without_dropping_later_paid_event(client):
    http, store, paid, _ = client
    unpaid = event(paid)
    unpaid["data"]["object"]["payment_status"] = "unpaid"
    assert signed(http, unpaid).status_code == 200
    assert (
        signed(
            http, event(paid, id="evt_unknown", type="payment_intent.succeeded")
        ).status_code
        == 200
    )
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone() == (
            "pending",
        )
    assert signed(http, event(paid, id="evt_paidlater")).status_code == 200
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone() == (
            "paid",
        )


@pytest.mark.parametrize(
    "raw",
    [
        b"null",
        b"[]",
        b"{",
        b'{"id":"evt_a","id":"evt_b"}',
        b'{"amount":NaN}',
        b'"text"',
    ],
)
def test_signed_malformed_payload_is_generic_400(client, raw):
    http, _, _, _ = client
    response = signed(http, None, raw=raw)
    assert response.status_code == 400


def test_limits_storage_failure_and_same_event_recovery(client):
    from core.instance import database_replacement_lock

    http, store, paid, _ = client
    assert (
        http.post(
            "/api/webhook/stripe",
            content=b"x" * (128 * 1024 + 1),
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": "t=1,v1=x",
            },
        ).status_code
        == 413
    )
    assert (
        http.post(
            "/api/webhook/stripe", content=b"x", headers={"Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    with database_replacement_lock(store.path):
        response = signed(http, event(paid))
    assert response.status_code == 503 and response.headers["retry-after"]
    assert signed(http, event(paid)).status_code == 200


def test_disabled_webhook_never_constructs_provider_or_opens_storage(monkeypatch):
    import stripe

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled touched dependencies")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(stripe.StripeClient, "__init__", forbidden)
    with TestClient(create_app(WebConfig())) as http:
        response = http.post("/api/webhook/stripe", json={})
    assert response.status_code == 503


def test_webhook_config_is_test_only_private_and_hides_secret(client):
    _, _, _, config = client
    assert SECRET not in repr(config)
    with pytest.raises(ValueError):
        replace(config, checkout=settings(test_mode=False))
    with pytest.raises(ValueError):
        replace(config, test_package_dir=None)
    with pytest.raises(ValueError):
        replace(
            config,
            test_package_dir=__import__("pathlib").Path("app/static/private").resolve(),
        )
    with pytest.raises(ValueError):
        replace(config, webhook_secret="invalid")


def test_paid_subscription_prepares_first_package_without_activating_account(client):
    http, store, paid, _ = client
    with sqlite3.connect(store.path) as c:
        c.execute(
            "UPDATE checkout_attempts SET kind='abonament',offer_json=?",
            (json.dumps({"amount_grosze": 23456, "interval": "month"}),),
        )
        c.execute("UPDATE b2b_orders SET typ_pakietu='abonament'")
    data = event(paid)
    data["data"]["object"].update(
        mode="subscription",
        amount_total=23456,
        amount_subtotal=23456,
        subscription="sub_synthetic",
        payment_intent=None,
    )
    assert signed(http, data).status_code == 200
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT state FROM paid_package_jobs").fetchone() == ("ready",)
        assert c.execute("SELECT status_subskrypcji FROM b2b_customers").fetchone() == (
            "nieaktywna",
        )


def test_post_ack_failure_is_recovered_without_regenerating_on_duplicate(
    client, monkeypatch
):
    from core.paid_packages import PaidPackagePreparer

    http, store, paid, config = client
    original = PaidPackagePreparer.prepare

    def interrupted(*_args):
        raise RuntimeError("synthetic-private-detail")

    monkeypatch.setattr(PaidPackagePreparer, "prepare", interrupted)
    assert signed(http, event(paid)).status_code == 200
    assert signed(http, event(paid)).status_code == 200
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT state FROM paid_package_jobs").fetchone() == (
            "pending",
        )
    monkeypatch.setattr(PaidPackagePreparer, "prepare", original)
    assert PaidPackagePreparer(store.path, config.test_package_dir).run_pending() == 1


def test_stream_limit_does_not_trust_content_length(client):
    http, _, _, _ = client
    response = http.post(
        "/api/webhook/stripe",
        content=iter([b"x" * 65536, b"y" * 65536, b"z"]),
        headers={
            "Content-Type": "application/json",
            "Content-Length": "1",
            "Stripe-Signature": "t=1,v1=x",
        },
    )
    assert response.status_code == 413
