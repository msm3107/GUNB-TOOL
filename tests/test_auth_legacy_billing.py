import copy
import hashlib
import json
import sqlite3
from datetime import timedelta

import pytest
import responses
import stripe
from fastapi.testclient import TestClient

from app.auth import AuthService
from app.config import WebConfig
from app.identity import AuthenticatedCustomer
from app.main import create_app
from app.subscription_service import SubscriptionService
from core.auth_store import AuthStore
from core.instance import database_replacement_lock
from core.payment_store import PaidCheckout, PaymentStore
from core.storage import SCHEMA_VERSION, LeadRepository
from core.subscription_store import SubscriptionNotice, SubscriptionStore
from tests.test_auth_billing import UUID, login
from tests.test_auth_service import Sender
from tests.test_auth_service import settings as auth_settings
from tests.test_payment_store import NOW
from tests.test_storage import legacy_database
from tests.test_stripe_service import settings
from tests.test_subscription_service import Subscriptions

CURRENT = NOW + timedelta(days=30)
KEY = hashlib.sha256(UUID.encode()).hexdigest()
SESSION = "cs_test_legacy"


def checkout_snapshot():
    return {
        "id": SESSION,
        "object": "checkout.session",
        "livemode": False,
        "mode": "subscription",
        "status": "complete",
        "payment_status": "paid",
        "currency": "pln",
        "amount_total": 23456,
        "created": int(NOW.timestamp()),
        "expires_at": int(NOW.timestamp()) + 3600,
        "customer_email": "person@example.test",
        "customer": "cus_synthetic",
        "subscription": "sub_synthetic",
        "client_reference_id": "1",
        "metadata": {"order_id": "1", "customer_id": "1", "checkout_key": KEY},
    }


class CheckoutSessions:
    def __init__(self, path):
        self.path = path
        self.value = checkout_snapshot()
        self.calls = 0
        self.failure = False
        self.after_read = None

    def retrieve(self, session_id):
        assert session_id == SESSION  # only the local paid receipt selects a Session
        with (
            database_replacement_lock(self.path),
            sqlite3.connect(self.path, timeout=0.1) as c,
        ):
            c.execute("BEGIN IMMEDIATE")
        self.calls += 1
        if self.failure:
            raise stripe.APIConnectionError("synthetic-private")
        if self.after_read:
            self.after_read()
        return copy.deepcopy(self.value)


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "v20.sqlite"
    c = legacy_database(path, 20)
    filters = '{"wojewodztwo":"28","branza":"materialy"}'
    c.execute(
        "INSERT INTO b2b_customers(customer_id,email) VALUES(1,'person@example.test')"
    )
    c.execute("INSERT INTO b2b_orders VALUES(1,1,?,'abonament','paid')", (filters,))
    c.execute(
        """INSERT INTO checkout_attempts(key_hash,fingerprint,order_id,customer_id,email,filters_json,
        kind,offer_json,origin,created_at,expires_at,session_id,url) VALUES(?,?,1,1,?,?,
        'abonament',?,'https://example.test',?,?,?,?)""",
        (
            KEY,
            "f" * 64,
            "person@example.test",
            filters,
            '{"amount_grosze":23456,"interval":"month"}',
            int(NOW.timestamp()),
            int(NOW.timestamp()) + 3600,
            SESSION,
            "https://checkout.stripe.com/test",
        ),
    )
    c.execute(
        "INSERT INTO b2b_deliveries(delivery_id,order_id,typ_dostawy,plik_xlsx,plik_csv) VALUES(7,1,'delta','kept.xlsx','kept.csv')"
    )
    c.execute(
        "INSERT INTO paid_package_jobs(order_id,checkout_key,session_id,state,created_at,delivery_id) VALUES(1,?,?,'ready',?,7)",
        (KEY, SESSION, int(NOW.timestamp())),
    )
    c.execute(
        "INSERT INTO stripe_events VALUES('evt_legacy','checkout.session.completed',?,'paid',1)",
        (int(NOW.timestamp()),),
    )
    c.commit()
    c.close()
    with LeadRepository(path) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM checkout_billing_links"
            ).fetchone()[0]
            == 0
        )
    (backup,) = (tmp_path / "backups").glob("*.sqlite")
    with sqlite3.connect(backup) as c:
        assert c.execute("PRAGMA user_version").fetchone() == (20,)
    return path


def unchanged_business(path):
    with sqlite3.connect(path) as c:
        assert c.execute(
            "SELECT count(*),min(status_platnosci) FROM b2b_orders"
        ).fetchone() == (1, "paid")
        assert c.execute(
            "SELECT count(*),min(state),min(delivery_id) FROM paid_package_jobs"
        ).fetchone() == (1, "ready", 7)
        assert c.execute(
            "SELECT delivery_id,doreczono_utc,plik_xlsx,plik_csv FROM b2b_deliveries"
        ).fetchall() == [(7, None, "kept.xlsx", "kept.csv")]
        assert c.execute(
            "SELECT count(*) FROM stripe_events WHERE outcome='paid'"
        ).fetchone() == (1,)


@pytest.fixture
def legacy_http(legacy):
    store = SubscriptionStore(legacy)
    notice = SubscriptionNotice("sub_synthetic", "cus_synthetic", KEY, "1", "1")
    subs = Subscriptions(store, notice)
    subs.value["current_period_end"] = int(CURRENT.timestamp()) + 3600
    sessions = CheckoutSessions(legacy)
    service = SubscriptionService(
        store,
        settings(),
        subscriptions=subs,
        checkout_sessions=sessions,
        now=lambda: CURRENT,
    )
    sender = Sender(legacy)
    auth = AuthService(
        AuthStore(legacy), auth_settings(), sender=sender, now=lambda: CURRENT
    )
    config = WebConfig(database_path=legacy, auth=auth_settings(), checkout=settings())
    with TestClient(
        create_app(config, auth_service=auth, subscription_service=service),
        base_url="https://example.test",
    ) as client:
        login(client, sender)
        yield client, store, subs, sessions


def claim(client):
    return client.post(
        "/auth/billing-link",
        json={"request_token": UUID},
        headers={"Origin": "https://example.test"},
    )


def test_v20_migration_login_recovers_old_checkout_and_retry_without_duplicate_work(
    legacy_http,
):
    client, store, subs, sessions = legacy_http
    assert claim(client).status_code == 200
    assert sessions.calls == subs.calls == 1
    assert store.has_subscription_access(1, now=CURRENT)
    assert claim(client).status_code == 200
    assert (
        sessions.calls == 1 and subs.calls == 2
    )  # persisted correlation bypasses recovery on retry
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT * FROM checkout_billing_links").fetchall() == [
            (1, "cus_synthetic", "sub_synthetic")
        ]
        assert c.execute("SELECT stripe_customer_id FROM b2b_customers").fetchone() == (
            "cus_synthetic",
        )
    receipt = PaidCheckout(
        KEY,
        SESSION,
        "1",
        "1",
        "subscription",
        "pln",
        23456,
        int(NOW.timestamp()),
        "cus_synthetic",
        "sub_synthetic",
    )
    assert (
        PaymentStore(store.path).record_event(
            event_id="evt_late_duplicate",
            event_type="checkout.session.completed",
            completion=receipt,
            now=CURRENT,
        )
        is None
    )
    unchanged_business(store.path)


def test_legacy_session_id_survives_uncertain_checkout_response_save(legacy_http):
    client, store, _, sessions = legacy_http
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE checkout_attempts SET session_id=NULL,url=NULL")
    assert claim(client).status_code == 200 and sessions.calls == 1
    unchanged_business(store.path)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "cs_test_other"},
        {"object": "subscription"},
        {"livemode": True},
        {"mode": "payment"},
        {"status": "open"},
        {"payment_status": "unpaid"},
        {"currency": "eur"},
        {"amount_total": 1},
        {"amount_total": 23456.0},
        {"customer_email": "other@example.test"},
        {"client_reference_id": "2"},
        {"metadata": {}},
        {"customer": "cus_invalid/../../"},
        {"subscription": None},
        {"created": int(NOW.timestamp()) - 301},
        {"expires_at": int(NOW.timestamp()) + 7200},
    ],
)
def test_legacy_recovery_rejects_unverified_provider_snapshot(legacy_http, changes):
    client, store, subs, sessions = legacy_http
    sessions.value.update(changes)
    assert claim(client).status_code == 503
    assert subs.calls == 0
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM checkout_billing_links").fetchone() == (
            0,
        )
        assert c.execute("SELECT stripe_customer_id FROM b2b_customers").fetchone() == (
            None,
        )
    unchanged_business(store.path)


def test_checkout_network_failure_and_subscription_retry_preserve_legacy_payment(
    legacy_http,
):
    client, store, subs, sessions = legacy_http
    sessions.failure = True
    assert claim(client).status_code == 503
    assert not store.has_subscription_access(1, now=CURRENT)
    sessions.failure = False
    subs.failure = True
    assert claim(client).status_code == 503
    assert not store.has_subscription_access(1, now=CURRENT)
    subs.failure = False
    assert claim(client).status_code == 200
    assert sessions.calls == 2
    unchanged_business(store.path)


def test_changed_local_proof_between_network_and_commit_is_rejected(legacy_http):
    client, store, subs, sessions = legacy_http

    def change():
        with sqlite3.connect(store.path) as c:
            c.execute("UPDATE b2b_orders SET filtry_json='{}'")

    sessions.after_read = change
    assert claim(client).status_code == 409 and subs.calls == 0
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM checkout_billing_links").fetchone() == (
            0,
        )
    unchanged_business(store.path)


def test_legacy_missing_link_does_not_authorize_wrong_customer_or_session(legacy_http):
    client, store, subs, sessions = legacy_http
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE checkout_attempts SET session_id='cs_test_other'")
    assert claim(client).status_code == 409
    assert sessions.calls == subs.calls == 0


def test_failed_legacy_link_insert_rolls_back_then_same_proof_retries(legacy_http):
    client, store, _, _ = legacy_http
    with sqlite3.connect(store.path) as c:
        c.execute(
            "CREATE TRIGGER fail_restore BEFORE INSERT ON checkout_billing_links BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    assert claim(client).status_code == 503
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT stripe_customer_id FROM b2b_customers").fetchone() == (
            None,
        )
        c.execute("DROP TRIGGER fail_restore")
    assert claim(client).status_code == 200
    unchanged_business(store.path)


def test_real_sdk_reads_only_existing_checkout_outside_database_locks(legacy_http):
    _, store, subs, _ = legacy_http
    auth = AuthService(
        AuthStore(store.path),
        auth_settings(),
        sender=Sender(store.path),
        now=lambda: CURRENT,
    )
    service = SubscriptionService(
        store, settings(), subscriptions=subs, now=lambda: CURRENT
    )

    def callback(request):
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
            json.dumps(checkout_snapshot()),
        )

    with responses.RequestsMock() as http:
        http.add_callback(
            responses.GET,
            "https://api.stripe.com/v1/checkout/sessions/" + SESSION,
            callback=callback,
        )
        auth.bind_billing(AuthenticatedCustomer(1), UUID, service)
    assert store.has_subscription_access(1, now=CURRENT)
    unchanged_business(store.path)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE b2b_customers SET stripe_customer_id='cus_other'",
        "UPDATE b2b_customers SET email='other@example.test'",
        "UPDATE checkout_attempts SET offer_json='{}'",
    ],
)
def test_recovery_does_not_commit_after_owner_or_snapshot_changes(
    legacy_http, statement
):
    client, store, subs, sessions = legacy_http

    def change():
        with sqlite3.connect(store.path) as c:
            c.execute(statement)

    sessions.after_read = change
    assert claim(client).status_code == 409 and subs.calls == 0
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM checkout_billing_links").fetchone() == (
            0,
        )
    unchanged_business(store.path)


def test_wrong_customer_proof_never_reaches_provider(legacy_http):
    client, store, subs, sessions = legacy_http
    with sqlite3.connect(store.path) as c:
        c.execute(
            "INSERT INTO b2b_customers(customer_id,email) VALUES(2,'other@example.test')"
        )
        c.execute("UPDATE b2b_orders SET customer_id=2")
    assert claim(client).status_code == 409 and sessions.calls == subs.calls == 0
    unchanged_business(store.path)


def test_concurrent_recovered_link_finalization_is_idempotent(legacy_http):
    from concurrent.futures import ThreadPoolExecutor

    from core.auth_store import BillingLinkMissing
    from core.checkout_store import CheckoutUnavailable

    _, store, subs, sessions = legacy_http
    auth_store = AuthStore(store.path)
    with pytest.raises(BillingLinkMissing) as missing:
        auth_store.claim_billing(1, KEY)
    restored = SubscriptionService(
        store, settings(), subscriptions=subs, checkout_sessions=sessions
    ).recover_checkout(missing.value.attempt)

    def finish(_):
        try:
            return AuthStore(store.path).claim_billing(1, KEY, recovered=restored)
        except CheckoutUnavailable:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(finish, range(2)))
    assert any(item is not None for item in results)
    assert auth_store.claim_billing(1, KEY, recovered=restored) == next(
        item for item in results if item is not None
    )
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM checkout_billing_links").fetchone() == (
            1,
        )
    unchanged_business(store.path)
