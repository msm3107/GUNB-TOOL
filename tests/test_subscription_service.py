import json
import sqlite3
from dataclasses import replace

import pytest
import responses
import stripe
from fastapi.testclient import TestClient

from app.config import WebConfig
from app.main import create_app
from app.subscription_service import SubscriptionService
from core.checkout_store import CheckoutUnavailable
from core.instance import database_replacement_lock
from tests.test_payment_store import NOW
from tests.test_payment_store import payment_fixture as _payment_fixture  # noqa: F401
from tests.test_stripe_service import settings
from tests.test_stripe_webhook import SECRET, signed
from tests.test_subscription_store import (
    subscription_fixture as _subscription_fixture,  # noqa: F401
)


def snapshot(notice, **changes):
    value = {
        "id": notice.subscription_id,
        "object": "subscription",
        "livemode": False,
        "customer": notice.stripe_customer_id,
        "status": "active",
        "current_period_end": int(NOW.timestamp()) + 3600,
        "cancel_at_period_end": False,
        "pause_collection": None,
        "metadata": {
            "order_id": notice.order_id,
            "customer_id": notice.customer_id,
            "checkout_key": notice.key_hash,
        },
    }
    value.update(changes)
    return value


class Subscriptions:
    def __init__(self, store, notice):
        self.store, self.notice = store, notice
        self.value = snapshot(notice)
        self.calls = 0
        self.failure = False

    def retrieve(self, subscription_id):
        assert subscription_id == self.notice.subscription_id
        with (
            database_replacement_lock(self.store.path),
            sqlite3.connect(self.store.path, timeout=0.1) as c,
        ):
            c.execute("BEGIN IMMEDIATE")
        self.calls += 1
        if self.failure:
            raise stripe.APIConnectionError("synthetic-private")
        return self.value


def test_current_provider_state_wins_over_delayed_snapshot_and_deleted_needs_no_api(
    subscription,
):
    store, notice = subscription
    fake = Subscriptions(store, notice)
    service = SubscriptionService(
        store, settings(), subscriptions=fake, now=lambda: NOW
    )
    service.apply(notice, event_id="evt_current")
    assert store.has_subscription_access(1, now=NOW)
    fake.value = snapshot(notice, status="past_due")
    service.apply(notice, event_id="evt_delayedactive")
    assert not store.has_subscription_access(1, now=NOW)
    service.apply(replace(notice, deleted=True), event_id="evt_deleted")
    service.apply(notice, event_id="evt_oldactive")
    assert fake.calls == 2
    assert not store.has_subscription_access(1, now=NOW)


def test_provider_failure_releases_lease_without_ack_then_retry_succeeds(subscription):
    store, notice = subscription
    fake = Subscriptions(store, notice)
    fake.failure = True
    service = SubscriptionService(
        store, settings(), subscriptions=fake, now=lambda: NOW
    )
    with pytest.raises(CheckoutUnavailable):
        service.apply(notice, event_id="evt_retry")
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT refresh_token FROM b2b_subscriptions").fetchone() == (
            None,
        )
        assert c.execute(
            "SELECT count(*) FROM stripe_events WHERE event_id='evt_retry'"
        ).fetchone() == (0,)
    fake.failure = False
    service.apply(notice, event_id="evt_retry")
    assert store.has_subscription_access(1, now=NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"livemode": True},
        {"id": "sub_other"},
        {"customer": "cus_other"},
        {"metadata": {}},
        {"current_period_end": True},
        {"cancel_at_period_end": 1},
        {"pause_collection": "invalid"},
        {"status": "unexpected"},
    ],
)
def test_bad_provider_snapshot_never_grants_access(subscription, changes):
    store, notice = subscription
    fake = Subscriptions(store, notice)
    fake.value = snapshot(notice, **changes)
    service = SubscriptionService(
        store, settings(), subscriptions=fake, now=lambda: NOW
    )
    with pytest.raises(CheckoutUnavailable):
        service.apply(notice, event_id="evt_invalid")
    assert not store.has_subscription_access(1, now=NOW)


def test_real_sdk_retrieval_has_pinned_version_and_never_holds_database_guard(
    subscription,
):
    store, notice = subscription
    service = SubscriptionService(store, settings(), now=lambda: NOW)

    def callback(request):
        assert request.headers["Stripe-Version"] == "2024-06-20"
        assert request.headers["Authorization"] == "Bearer sk_test_synthetic123"
        with (
            database_replacement_lock(store.path),
            sqlite3.connect(store.path, timeout=0.1) as c,
        ):
            c.execute("BEGIN IMMEDIATE")
        return (200, {"Content-Type": "application/json"}, json.dumps(snapshot(notice)))

    with responses.RequestsMock() as http:
        http.add_callback(
            responses.GET,
            "https://api.stripe.com/v1/subscriptions/sub_synthetic",
            callback=callback,
        )
        service.apply(notice, event_id="evt_sdk")
    assert store.has_subscription_access(1, now=NOW)


def test_signed_subscription_webhook_updates_and_cancels_immediately(
    subscription, tmp_path
):
    store, notice = subscription
    fake = Subscriptions(store, notice)
    service = SubscriptionService(
        store, settings(), subscriptions=fake, now=lambda: NOW
    )
    config = WebConfig(
        database_path=store.path,
        checkout=settings(),
        webhook_secret=SECRET,
        test_package_dir=tmp_path / "private",
    )

    def event(event_id, deleted=False):
        return {
            "id": event_id,
            "object": "event",
            "api_version": "2024-06-20",
            "livemode": False,
            "type": "customer.subscription.deleted"
            if deleted
            else "customer.subscription.updated",
            "data": {
                "object": snapshot(notice, status="canceled" if deleted else "active")
            },
        }

    with TestClient(create_app(config, subscription_service=service)) as client:
        assert signed(client, event("evt_signed")).status_code == 200
        assert store.has_subscription_access(1, now=NOW)
        assert signed(client, event("evt_signed")).status_code == 200
        assert fake.calls == 1
        assert signed(client, event("evt_cancel", True)).status_code == 200
        assert not store.has_subscription_access(1, now=NOW)
        assert signed(client, event("evt_late")).status_code == 200
        assert fake.calls == 1
