import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core.checkout_store import CheckoutConflict, CheckoutStore, CheckoutUnavailable
from core.instance import database_replacement_lock
from core.storage import LeadRepository

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
KEY = hashlib.sha256(b"synthetic-request").hexdigest()


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with LeadRepository(path):
        pass
    return CheckoutStore(path)


def reserve(store, **changes):
    values = {"key_hash": KEY, "fingerprint": "f" * 64, "email": "person@example.test", "filters_json": '{"wojewodztwo":"28"}',
              "kind": "jednorazowy", "offer_json": '{"amount_grosze":12345,"interval":null}', "origin": "https://example.test", "now": NOW}
    values.update(changes)
    return store.reserve(**values)


def test_reserve_is_durable_idempotent_and_does_not_change_existing_customer(store):
    with sqlite3.connect(store.path) as c:
        c.execute("INSERT INTO b2b_customers(email,stripe_customer_id,status_subskrypcji) VALUES ('person@example.test','cus_existing','active')")
    first = reserve(store)
    again = reserve(store, offer_json='{"amount_grosze":99999}', origin="https://changed.example.test")
    assert first == again
    assert first.email == "person@example.test" and first.session_id is None
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*),min(status_platnosci) FROM b2b_orders").fetchone() == (1, "pending")
        assert c.execute("SELECT stripe_customer_id,status_subskrypcji FROM b2b_customers").fetchone() == ("cus_existing", "active")
    with pytest.raises(CheckoutConflict):
        reserve(store, fingerprint="a" * 64)


def test_cached_session_and_expiry_never_mark_paid(store):
    attempt = reserve(store)
    store.complete(KEY, "cs_test_demo", "https://checkout.stripe.com/c/pay/cs_test_demo")
    cached = reserve(store)
    assert cached.order_id == attempt.order_id and cached.session_id == "cs_test_demo"
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone()[0] == "pending"
    with pytest.raises(CheckoutConflict):
        store.complete(KEY, "cs_test_different", "https://checkout.stripe.com/other")
    with pytest.raises(CheckoutConflict):
        reserve(store, now=NOW + timedelta(hours=1))


def test_missing_database_and_real_replacement_guard_fail_closed(store, tmp_path):
    with database_replacement_lock(store.path), pytest.raises(CheckoutUnavailable):
        reserve(store)
    with pytest.raises(CheckoutUnavailable):
        reserve(CheckoutStore(tmp_path / "missing" / "no.sqlite"))
    assert not (tmp_path / "missing").exists()


def test_v18_migration_preserves_orders_and_has_backup(tmp_path):
    from core import storage
    from tests.test_storage import legacy_database

    path = tmp_path / "old.sqlite"
    c = legacy_database(path, 18)
    c.execute("INSERT INTO b2b_customers(customer_id,email) VALUES(7,'kept@example.test')")
    c.execute("INSERT INTO b2b_orders(order_id,customer_id,typ_pakietu,status_platnosci) VALUES(8,7,'jednorazowy','paid')")
    c.commit()
    c.close()
    with LeadRepository(path) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        assert tuple(repo.connection.execute("SELECT order_id,status_platnosci FROM b2b_orders").fetchone()) == (8, "paid")
        assert repo.connection.execute("SELECT count(*) FROM checkout_attempts").fetchone()[0] == 0
    backup, = (tmp_path / "backups").glob("old-przed-*.sqlite")
    with sqlite3.connect(backup) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 18
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone()[0] == "paid"
        assert c.execute("SELECT name FROM sqlite_master WHERE name='checkout_attempts'").fetchone() is None


def test_v19_failure_rolls_back_table_and_version(tmp_path, monkeypatch):
    from core import storage
    from tests.test_storage import legacy_database

    path = tmp_path / "failed.sqlite"
    legacy_database(path, 18).close()
    steps = storage._MIGRATIONS
    monkeypatch.setattr(storage, "_MIGRATIONS", steps[:18] + (steps[18] + "SELECT missing_checkout_function();",))
    with pytest.raises(sqlite3.OperationalError, match="missing_checkout_function"):
        LeadRepository(path)
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 18
        assert c.execute("SELECT name FROM sqlite_master WHERE name='checkout_attempts'").fetchone() is None


def test_store_never_migrates_old_schema(store):
    with sqlite3.connect(store.path) as conn:
        conn.execute("PRAGMA user_version=18")
    with pytest.raises(CheckoutUnavailable):
        reserve(store)
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 18
        assert conn.execute("SELECT count(*) FROM b2b_orders").fetchone()[0] == 0
