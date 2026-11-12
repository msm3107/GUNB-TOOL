"""Publication notifications use synthetic SQLite and fake SMTP only."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from core.b2b_outbox import B2BOutbox
from core.email_sender import DeliveryResult
from core.instance import database_replacement_lock
from core.storage import LeadRepository
from tests.test_storage import lead

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc)


@pytest.fixture
def outbox(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with LeadRepository(path) as repo:
        c = repo.connection
        c.execute(
            "INSERT INTO b2b_customers VALUES(1,'person@example.test','PRIVATE COMPANY',NULL,NULL,'nieaktywna')"
        )
        c.execute(
            "INSERT INTO b2b_customers(customer_id,email) VALUES(2,'other@example.test')"
        )
        c.execute("INSERT INTO b2b_orders VALUES(1,1,'{}','jednorazowy','paid')")
        c.execute(
            "INSERT INTO b2b_deliveries VALUES(1,1,'delta',NULL,'one.xlsx','one.csv')"
        )
        for fmt in ("xlsx", "csv"):
            c.execute(
                "INSERT INTO package_artifacts VALUES(1,?,?,100,?)",
                (fmt, "one." + fmt, "a" * 64),
            )
        for i, category in enumerate(("new", "new", "change", "full")):
            repo.upsert(lead("PRIVATE-CASE-" + str(i)))
            c.execute(
                "INSERT INTO b2b_delivery_items VALUES(1,?,?,?)",
                ("PRIVATE-CASE-" + str(i), "r" * 64, category),
            )
    return B2BOutbox(path)


def publish(store):
    with sqlite3.connect(store.path) as conn:
        conn.execute("INSERT INTO package_publications VALUES(1,?)", (NOW.isoformat(),))


@pytest.fixture
def queued(outbox):
    publish(outbox)
    return outbox


def row(store):
    with sqlite3.connect(store.path) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute("SELECT * FROM b2b_outbox").fetchone())


def test_preparation_does_not_enqueue_and_publication_enqueues_once(outbox):
    assert not outbox.enqueue_ready(1)
    with sqlite3.connect(outbox.path) as conn:
        assert conn.execute("SELECT count(*) FROM b2b_outbox").fetchone()[0] == 0
    publish(outbox)
    assert row(outbox)["status"] == "pending"
    assert not outbox.enqueue_ready(1)
    with sqlite3.connect(outbox.path) as conn:
        assert conn.execute("SELECT count(*) FROM b2b_outbox").fetchone()[0] == 1
        assert (
            conn.execute("SELECT doreczono_utc FROM b2b_deliveries").fetchone()[0]
            is None
        )


def test_claim_fencing_and_safe_recovery_before_smtp(queued):
    ticket = queued.claim(now=NOW)
    assert ticket and "person@example.test" not in repr(ticket)
    assert queued.claim(now=NOW) is None
    late = NOW + timedelta(seconds=91)
    queued.recover_stale(now=late)
    fresh = B2BOutbox(queued.path).claim(now=late)
    assert fresh and fresh.lease_token != ticket.lease_token
    assert queued.begin_send(ticket, now=late) is None
    ready = queued.begin_send(fresh, now=late)
    assert (ready.new_count, ready.change_count, ready.full_count) == (2, 1, 1)
    assert queued.finish(fresh, DeliveryResult("accepted"), now=late)
    assert row(queued)["status"] == "accepted"


def test_started_smtp_crash_is_unknown_and_never_auto_retried(queued):
    ticket = queued.claim(now=NOW)
    assert queued.begin_send(ticket, now=NOW)
    queued.recover_stale(now=NOW + timedelta(seconds=91))
    assert row(queued)["status"] == "unknown"
    assert queued.claim(now=NOW + timedelta(days=1)) is None
    assert not queued.finish(
        ticket, DeliveryResult("accepted"), now=NOW + timedelta(seconds=92)
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE b2b_customers SET email='changed@example.test' WHERE customer_id=1",
        "UPDATE b2b_orders SET customer_id=2 WHERE order_id=1",
        "UPDATE b2b_orders SET status_platnosci='failed' WHERE order_id=1",
        "DELETE FROM package_publications",
        "DELETE FROM package_artifacts WHERE format='csv'",
        "UPDATE b2b_deliveries SET plik_csv='changed.csv'",
    ],
)
def test_current_authorization_rechecked_after_claim(queued, mutation):
    ticket = queued.claim(now=NOW)
    with sqlite3.connect(queued.path) as conn:
        conn.execute(mutation)
    assert queued.begin_send(ticket, now=NOW) is None
    assert row(queued)["status"] == "cancelled"


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("accepted", "accepted"),
        ("failed", "rejected"),
        ("unknown", "unknown"),
        ("retry", "pending"),
    ],
)
def test_outcomes_persist_and_retry_has_backoff(queued, outcome, status):
    ticket = queued.claim(now=NOW)
    assert queued.begin_send(ticket, now=NOW)
    assert queued.finish(ticket, DeliveryResult(outcome), now=NOW)
    assert row(queued)["status"] == status
    assert queued.claim(now=NOW) is None
    if outcome == "retry":
        assert queued.claim(now=NOW + timedelta(seconds=60))


def test_safe_retry_is_bounded_to_five_attempts(queued):
    stamp = NOW
    for attempt in range(1, 6):
        ticket = queued.claim(now=stamp)
        assert ticket and queued.begin_send(ticket, now=stamp)
        assert queued.finish(ticket, DeliveryResult("retry"), now=stamp)
        assert row(queued)["attempts"] == attempt
        stamp += timedelta(hours=1)
    assert row(queued)["status"] == "rejected"


class Sender:
    def __init__(self, path, outcome="accepted"):
        self.path, self.outcome, self.calls = path, outcome, []

    def send_email(self, recipient, subject, body, *, idempotency_key, html_body=None):
        with database_replacement_lock(self.path):
            with sqlite3.connect(self.path, timeout=0) as conn:
                conn.execute("BEGIN IMMEDIATE")
        self.calls.append((recipient, subject, body, idempotency_key, html_body))
        return DeliveryResult(self.outcome)
