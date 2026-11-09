import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from core.auth_store import AuthRejected, AuthStore
from core.checkout_store import CheckoutConflict, CheckoutUnavailable
from core.storage import LeadRepository
from tests.test_payment_store import NOW
from tests.test_payment_store import payment_fixture as _payment_fixture  # noqa: F401
from tests.test_subscription_store import (
    subscription_fixture as _subscription_fixture,  # noqa: F401
)

LINK = "a" * 64
BROWSER = "b" * 64
SESSION = "c" * 64


@pytest.fixture(name="auth_store")
def auth_store_fixture(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with LeadRepository(path) as repo:
        repo.connection.execute(
            "INSERT INTO b2b_customers(email) VALUES('person@example.test')"
        )
    return AuthStore(path)


def issue(store, **updates):
    values = {
        "email": "person@example.test",
        "token_hash": LINK,
        "browser_hash": BROWSER,
        "address_hash": "d" * 64,
        "ip_hash": "e" * 64,
        "now": NOW,
    }
    values.update(updates)
    return store.issue_link(**values)


def consume(store, **updates):
    values = {
        "token_hash": LINK,
        "browser_hash": BROWSER,
        "session_hash": SESSION,
        "now": NOW,
    }
    values.update(updates)
    return store.consume_link(**values)


def test_link_single_use_and_session_expiry_revocation(auth_store):
    assert issue(auth_store) is True
    assert consume(auth_store) == 1
    assert auth_store.session_customer(SESSION, now=NOW) == 1
    with pytest.raises(AuthRejected):
        consume(auth_store, session_hash="f" * 64)
    assert auth_store.session_customer(SESSION, now=NOW + timedelta(hours=8)) is None
    auth_store.revoke_session(SESSION)
    assert auth_store.session_customer(SESSION, now=NOW) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"browser_hash": "f" * 64},
        {"now": NOW + timedelta(minutes=15)},
        {"token_hash": "f" * 64},
    ],
)
def test_invalid_link_never_consumes_or_creates_session(auth_store, changes):
    issue(auth_store)
    with pytest.raises(AuthRejected):
        consume(auth_store, **changes)
    assert auth_store.session_customer(SESSION, now=NOW) is None


def test_email_mutation_invalidates_link_and_session(auth_store):
    issue(auth_store)
    consume(auth_store)
    issue(auth_store, token_hash="f" * 64, address_hash="f" * 64)
    with sqlite3.connect(auth_store.path) as c:
        c.execute("UPDATE b2b_customers SET email='changed@example.test'")
    assert auth_store.session_customer(SESSION, now=NOW) is None
    with pytest.raises(AuthRejected):
        consume(auth_store, token_hash="f" * 64, session_hash="0" * 64)


def test_unknown_and_rate_limited_requests_do_not_create_accounts(auth_store):
    assert issue(auth_store, email="unknown@example.test") is False
    assert issue(auth_store, token_hash="1" * 64) is True
    assert issue(auth_store, token_hash="2" * 64) is True
    assert issue(auth_store, token_hash="3" * 64) is False
    with sqlite3.connect(auth_store.path) as c:
        assert c.execute("SELECT count(*) FROM b2b_customers").fetchone() == (1,)
        assert c.execute("SELECT count(*) FROM auth_requests").fetchone() == (3,)


def test_concurrent_consumption_has_one_winner_and_rolls_back_failure(auth_store):
    issue(auth_store)

    def attempt(i):
        try:
            return consume(AuthStore(auth_store.path), session_hash=str(i) * 64)
        except (AuthRejected, CheckoutUnavailable):
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, [1, 2]))
    assert results.count(1) == 1
    with sqlite3.connect(auth_store.path) as c:
        assert c.execute("SELECT count(*) FROM auth_sessions").fetchone() == (1,)


def test_failed_session_insert_does_not_burn_link(auth_store):
    issue(auth_store)
    with sqlite3.connect(auth_store.path) as c:
        c.execute(
            "CREATE TRIGGER fail_auth BEFORE INSERT ON auth_sessions BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(CheckoutUnavailable):
        consume(auth_store)
    with sqlite3.connect(auth_store.path) as c:
        c.execute("DROP TRIGGER fail_auth")
    assert consume(auth_store) == 1


def test_ip_global_rate_limits_and_bounded_cleanup(auth_store):
    for i in range(11):
        issue(auth_store, email=f"unknown{i}@example.test", address_hash=f"{i:064x}")
    with sqlite3.connect(auth_store.path) as c:
        assert c.execute("SELECT count(*) FROM auth_requests").fetchone() == (10,)
    for i in range(200):
        issue(
            auth_store,
            email=f"unknown{i}@example.test",
            address_hash=f"{i + 20:064x}",
            ip_hash=f"{i + 20:064x}",
        )
    with sqlite3.connect(auth_store.path) as c:
        assert c.execute("SELECT count(*) FROM auth_requests").fetchone() == (100,)
    assert issue(auth_store, now=NOW + timedelta(hours=3))
    with sqlite3.connect(auth_store.path) as c:
        assert c.execute("SELECT count(*) FROM auth_requests").fetchone() == (1,)


def test_replacement_session_is_rotated_atomically(auth_store):
    issue(auth_store)
    consume(auth_store)
    issue(auth_store, token_hash="f" * 64)
    consume(
        auth_store, token_hash="f" * 64, session_hash="0" * 64, old_session_hash=SESSION
    )
    assert auth_store.session_customer(SESSION, now=NOW) is None
    assert auth_store.session_customer("0" * 64, now=NOW) == 1


def test_billing_claim_requires_customer_capability_paid_correlation(subscription):
    store, notice = subscription
    auth = AuthStore(store.path)
    with sqlite3.connect(store.path) as c:
        c.execute("UPDATE b2b_customers SET stripe_customer_id=NULL")
        c.execute(
            "INSERT INTO b2b_customers(customer_id,email) VALUES(2,'other@example.test')"
        )
    with pytest.raises(CheckoutConflict):
        auth.claim_billing(2, notice.key_hash)
    with pytest.raises(CheckoutConflict):
        auth.claim_billing(1, "f" * 64)
    result = auth.claim_billing(1, notice.key_hash)
    assert result == notice
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT stripe_customer_id FROM b2b_customers WHERE customer_id=1"
        ).fetchone() == ("cus_synthetic",)
        c.execute(
            "UPDATE b2b_customers SET stripe_customer_id='cus_other' WHERE customer_id=1"
        )
    with pytest.raises(CheckoutConflict):
        auth.claim_billing(1, notice.key_hash)


def test_v22_migration_preserves_customers_and_rolls_back(tmp_path, monkeypatch):
    from core import storage
    from tests.test_storage import legacy_database

    path = tmp_path / "old.sqlite"
    c = legacy_database(path, 21)
    c.execute(
        "INSERT INTO b2b_customers VALUES(7,'kept@example.test',NULL,NULL,'cus_kept','active')"
    )
    c.commit()
    c.close()
    steps = storage._MIGRATIONS[:22]  # exercise v21->v22 failure, not a later step
    monkeypatch.setattr(
        storage,
        "_MIGRATIONS",
        steps[:-1] + (steps[-1] + "SELECT missing_auth_function();",),
    )
    with pytest.raises(sqlite3.OperationalError):
        LeadRepository(path)
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone() == (21,)
        assert (
            c.execute(
                "SELECT name FROM sqlite_master WHERE name='auth_sessions'"
            ).fetchone()
            is None
        )
    monkeypatch.setattr(storage, "_MIGRATIONS", steps)
    with LeadRepository(path) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == 22
        assert tuple(
            repo.connection.execute(
                "SELECT email,stripe_customer_id FROM b2b_customers"
            ).fetchone()
        ) == ("kept@example.test", "cus_kept")
        assert not repo.connection.execute("PRAGMA foreign_key_check").fetchall()
    assert list((tmp_path / "backups").glob("*.sqlite"))
