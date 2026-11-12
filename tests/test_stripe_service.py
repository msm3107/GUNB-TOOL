import copy
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import stripe

from app.stripe_service import CheckoutOffer, CheckoutService, CheckoutSettings
from core.checkout_store import CheckoutConflict, CheckoutStore, CheckoutUnavailable
from core.instance import (
    database_replacement_lock,
    database_replacement_lock_path,
    lock_is_free,
)
from core.package_selection import PackageFilters
from core.storage import LeadRepository

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
URL = "https://checkout.stripe.com/c/pay/cs_test_synthetic"


def settings(**updates):
    values = {"test_mode": True, "secret_key": "sk_test_synthetic123", "origin": "https://example.test",
              "package_offer": CheckoutOffer(12345), "subscription_offer": CheckoutOffer(23456, "month")}
    values.update(updates)
    return CheckoutSettings(**values)


class Customers:
    def __init__(self, path):
        self.path = path
        self.calls = []
        self.error = None
        self.after_create = None
        self.check_locks = True

    def create(self, params, options):
        if self.check_locks:
            with database_replacement_lock(self.path), sqlite3.connect(self.path, timeout=0) as conn:
                conn.execute("BEGIN IMMEDIATE")
        self.calls.append((copy.deepcopy(params), dict(options)))
        if self.after_create:
            self.after_create()
        if self.error:
            raise self.error
        return {"id": "cus_synthetic", "object": "customer", "livemode": False, "metadata": params["metadata"]}


class Sessions:
    def __init__(self, path):
        self.path = path
        self.calls = []
        self.error = None
        self.response = {"id": "cs_test_synthetic", "livemode": False, "url": URL}

    def create(self, params, options):
        assert lock_is_free(database_replacement_lock_path(self.path))
        # A real unrelated writer can commit during the provider call.
        with sqlite3.connect(self.path, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")
            assert conn.execute("SELECT count(*) FROM b2b_orders").fetchone()[0] == 1
        self.calls.append((copy.deepcopy(params), dict(options)))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with LeadRepository(path):
        pass
    fake = Sessions(path)
    instance = CheckoutService(CheckoutStore(path), settings(), sessions=fake, now=lambda: NOW)
    return instance, fake


def start(service, **changes):
    values = {"email": "person@example.test", "kind": "jednorazowy", "filters": PackageFilters("28", "materialy"), "request_token": str(uuid4())}
    values.update(changes)
    return service.start(**values)


@pytest.mark.parametrize("kind,mode,amount,recurring", (("jednorazowy", "payment", 12345, None), ("abonament", "subscription", 23456, {"interval": "month"})))
def test_exact_modes_integer_pln_amounts_metadata_and_no_network_transaction(service, kind, mode, amount, recurring):
    instance, fake = service
    assert start(instance, kind=kind) == URL
    params, options = fake.calls[0]
    assert params["mode"] == mode and params["customer_email"] == "person@example.test"
    assert params["metadata"]["order_id"] == params["client_reference_id"] == "1"
    assert params["metadata"]["customer_id"] == "1" and "customer" not in params
    price = params["line_items"][0]["price_data"]
    assert price["unit_amount"] == amount and type(price["unit_amount"]) is int
    assert price["currency"] == "pln" and price.get("recurring") == recurring
    assert params["line_items"][0]["quantity"] == 1
    assert params["success_url"] == "https://example.test/zamowienie/sukces"
    assert params["expires_at"] == int(NOW.timestamp()) + 3600
    assert options["idempotency_key"].startswith("zt-checkout-")
    assert "person" not in options["idempotency_key"]


def test_ambiguous_error_and_changed_configuration_retry_same_order_and_parameters(service):
    instance, fake = service
    token = str(uuid4())
    fake.error = stripe.APIConnectionError("synthetic secret must not leak")
    with pytest.raises(CheckoutUnavailable):
        start(instance, request_token=token)
    fake.error = None
    changed = CheckoutService(instance.store, settings(package_offer=CheckoutOffer(99999), origin="https://changed.example.test"),
                              sessions=fake, now=lambda: NOW + timedelta(minutes=1))
    assert start(changed, request_token=token) == URL
    assert fake.calls[0] == fake.calls[1]
    assert start(changed, request_token=token) == URL and len(fake.calls) == 2
    with pytest.raises(CheckoutConflict):
        start(changed, request_token=token, email="other@example.test")


@pytest.mark.parametrize("value", (True, 1.1, 0, -1, "100"))
def test_amount_must_be_positive_integer_grosze(value):
    with pytest.raises(ValueError):
        CheckoutOffer(value)


def test_live_keys_and_unsafe_origin_rejected_and_secret_not_repr():
    assert "sk_test_synthetic123" not in repr(settings())
    for changes in ({"secret_key": "sk_live_synthetic123"}, {"origin": "https://example.test/evil"},
                    {"origin": "http://remote.example.test"}, {"subscription_offer": CheckoutOffer(123)}):
        with pytest.raises(ValueError):
            settings(**changes)


@pytest.mark.parametrize("response", (
    {"id": "cs_live_demo", "livemode": True, "url": URL},
    {"id": "cs_test_demo", "livemode": False, "url": "https://checkout.stripe.com.evil.test/x"},
    {"id": "cs_test_demo", "livemode": False, "url": "https://user@checkout.stripe.com/x"},
    {"id": "cs_test_demo", "livemode": False, "url": "http://checkout.stripe.com/x"},
    {"id": "cs_test_demo", "livemode": False, "url": URL + "\r\nx: y"},
))
def test_invalid_provider_response_never_redirects(service, response):
    instance, fake = service
    fake.response = response
    with pytest.raises(CheckoutUnavailable):
        start(instance)


def test_expired_uncertain_attempt_never_creates_new_provider_session(service):
    instance, fake = service
    token = str(uuid4())
    fake.error = stripe.APIConnectionError("timeout")
    with pytest.raises(CheckoutUnavailable):
        start(instance, request_token=token)
    fake.error = None
    late = CheckoutService(instance.store, settings(), sessions=fake, now=lambda: NOW + timedelta(minutes=30))
    with pytest.raises(CheckoutConflict):
        start(late, request_token=token)
    assert len(fake.calls) == 1


def test_key_rotation_cannot_recreate_ambiguous_session_in_another_account(service):
    instance, fake = service
    token = str(uuid4())
    fake.error = stripe.APIConnectionError("timeout")
    with pytest.raises(CheckoutUnavailable):
        start(instance, request_token=token)
    fake.error = None
    rotated = CheckoutService(instance.store, settings(secret_key="sk_test_otheraccount123"), sessions=fake, now=lambda: NOW)
    with pytest.raises(CheckoutConflict):
        start(rotated, request_token=token)
    assert len(fake.calls) == 1


def test_disabled_service_does_not_open_database_or_call_provider(service):
    instance, fake = service
    off = CheckoutService(CheckoutStore(None), CheckoutSettings(), sessions=fake)
    with pytest.raises(CheckoutUnavailable):
        start(off)
    assert fake.calls == []
    with database_replacement_lock(instance.store.path), pytest.raises(CheckoutUnavailable):
        start(instance)
    assert fake.calls == []


def test_real_sdk_serializes_request_through_fake_http_only(service):
    from urllib.parse import parse_qs

    import responses

    instance, _ = service
    production_adapter = CheckoutService(instance.store, settings(), now=lambda: NOW)

    def check_http(request):
        import json

        assert lock_is_free(database_replacement_lock_path(instance.store.path))
        assert request.headers["Authorization"] == "Bearer sk_test_synthetic123"
        assert request.headers["Stripe-Version"] == "2024-06-20"
        assert request.headers["Idempotency-Key"].startswith("zt-checkout-")
        values = parse_qs(request.body)
        assert values["line_items[0][price_data][unit_amount]"] == ["12345"]
        assert values["line_items[0][price_data][currency]"] == ["pln"]
        assert values["metadata[order_id]"] == ["1"]
        return 200, {"Content-Type": "application/json", "Request-Id": "req_synthetic"}, json.dumps(
            {"object": "checkout.session", "id": "cs_test_synthetic", "livemode": False, "url": URL})

    with responses.RequestsMock() as mocked_http:
        mocked_http.add_callback(responses.POST, "https://api.stripe.com/v1/checkout/sessions", callback=check_http)
        assert start(production_adapter) == URL


def test_db_failure_after_provider_success_retries_identical_session(service, monkeypatch):
    instance, fake = service
    token = str(uuid4())
    with monkeypatch.context() as patch:
        def unavailable(*_):
            raise CheckoutUnavailable

        patch.setattr(instance.store, "complete", unavailable)
        with pytest.raises(CheckoutUnavailable):
            start(instance, request_token=token)
    assert start(instance, request_token=token) == URL
    assert fake.calls[0] == fake.calls[1]


def test_concurrent_retry_uses_one_order_and_same_provider_key(service):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    instance, fake = service
    barrier, entered = threading.Barrier(2), threading.Event()
    calls = []

    class Overlapping:
        def create(self, params, options):
            calls.append((params, options))
            entered.set()
            barrier.wait(timeout=5)
            return fake.response

    overlap = CheckoutService(instance.store, settings(), sessions=Overlapping(), now=lambda: NOW)
    token = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(start, overlap, request_token=token)
        assert entered.wait(timeout=5)
        second = workers.submit(start, overlap, request_token=token)
        results = []
        for future in (first, second):
            try:
                results.append(future.result(timeout=5))
            except CheckoutUnavailable:
                # The existing replacement guard is fail-fast/exclusive. A
                # concurrent final write may get503 and retry the same token.
                results.append(None)
        assert URL in results
    assert start(overlap, request_token=token) == URL
    assert calls[0] == calls[1]
    with sqlite3.connect(instance.store.path) as conn:
        assert conn.execute("SELECT count(*) FROM b2b_orders").fetchone()[0] == 1


def test_attempt_rate_limit_bounds_provider_and_database_work(service):
    from app.stripe_service import CheckoutBusy

    instance, fake = service
    token = str(uuid4())
    for _ in range(30):
        assert start(instance, request_token=token) == URL
    with pytest.raises(CheckoutBusy):
        start(instance)
    assert len(fake.calls) == 1
