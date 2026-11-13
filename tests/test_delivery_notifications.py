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


def settings(**changes):
    from core.delivery_notifications import DeliveryNotificationSettings
    from core.email_sender import SMTPSettings

    return DeliveryNotificationSettings(
        **{
            "enabled": True,
            "origin": "https://example.test",
            "smtp": SMTPSettings("smtp.example.test", 587, "service@example.test"),
            **changes,
        }
    )


def test_worker_sends_minimal_text_and_html_without_sql_or_os_lock(queued):
    from core.delivery_notifications import DeliveryNotificationWorker

    sender = Sender(queued.path)
    worker = DeliveryNotificationWorker(
        queued, settings(), sender=sender, now=lambda: NOW
    )
    assert worker.run_pending()["accepted"] == 1
    assert worker.run_pending()["accepted"] == 0
    recipient, subject, body, key, html = sender.calls[0]
    assert recipient == "person@example.test" and "Żółta Tablica" in subject
    assert "Nowe: 2" in body and "Zmiany: 1" in body and "Pełna baza: 1" in body
    assert "https://example.test/panel/paczki/1" in body
    assert 'href="https://example.test/panel/paczki/1"' in html
    assert key.startswith("zt-package-ready-")
    assert all(
        private not in body + html + key
        for private in (
            "PRIVATE-CASE",
            "PRIVATE COMPANY",
            "person@example.test",
            "one.csv",
            "token=",
            "request_token",
        )
    )
    with sqlite3.connect(queued.path) as conn:
        assert (
            conn.execute("SELECT doreczono_utc FROM b2b_deliveries").fetchone()[0]
            is None
        )


@pytest.mark.parametrize(
    "outcome,state",
    [("failed", "rejected"), ("unknown", "unknown"), ("retry", "pending")],
)
def test_worker_provider_outcomes_survive_restart(queued, outcome, state):
    from core.delivery_notifications import DeliveryNotificationWorker

    sender = Sender(queued.path, outcome)
    DeliveryNotificationWorker(
        queued, settings(), sender=sender, now=lambda: NOW
    ).run_pending()
    assert row(queued)["status"] == state
    sender.outcome = "accepted"
    DeliveryNotificationWorker(
        B2BOutbox(queued.path),
        settings(),
        sender=sender,
        now=lambda: NOW + timedelta(minutes=2),
    ).run_pending()
    assert len(sender.calls) == (2 if outcome == "retry" else 1)


def test_smtp_accepted_but_receipt_save_failed_is_quarantined(queued, monkeypatch):
    from core.checkout_store import CheckoutUnavailable
    from core.delivery_notifications import DeliveryNotificationWorker

    sender = Sender(queued.path)
    with monkeypatch.context() as temporary:

        def unavailable(*args, **kwargs):
            raise CheckoutUnavailable

        temporary.setattr(queued, "finish", unavailable)
        DeliveryNotificationWorker(
            queued, settings(), sender=sender, now=lambda: NOW
        ).run_pending()
    assert row(queued)["status"] == "sending"
    DeliveryNotificationWorker(
        queued, settings(), sender=sender, now=lambda: NOW + timedelta(seconds=91)
    ).run_pending()
    assert row(queued)["status"] == "unknown" and len(sender.calls) == 1


def test_disabled_worker_and_cli_never_open_db_or_smtp(tmp_path, capsys):
    from core.delivery_notifications import (
        DeliveryNotificationSettings,
        DeliveryNotificationWorker,
        main,
    )

    path = tmp_path / "missing.sqlite"
    assert (
        sum(
            DeliveryNotificationWorker(B2BOutbox(path), DeliveryNotificationSettings())
            .run_pending()
            .values()
        )
        == 0
    )
    assert main(["--database", str(path)], source={}) == 0
    assert "disabled" in capsys.readouterr().out
    assert not path.exists()


@pytest.mark.parametrize(
    "address", ["person@example.test\r\nBcc:other@example.test", "bad\x00@example.test"]
)
def test_invalid_recipient_never_reaches_sender(outbox, address):
    from core.delivery_notifications import DeliveryNotificationWorker

    with sqlite3.connect(outbox.path) as conn:
        conn.execute("UPDATE b2b_customers SET email=? WHERE customer_id=1", (address,))
    publish(outbox)
    sender = Sender(outbox.path)
    DeliveryNotificationWorker(
        outbox, settings(), sender=sender, now=lambda: NOW
    ).run_pending()
    assert row(outbox)["status"] == "cancelled" and sender.calls == []


@pytest.mark.parametrize(
    "origin",
    [
        "https://example.test?",
        "https://example.test#",
        "https://@example.test",
        "https://example.test/path",
        "https://example.test\\evil",
        "javascript:alert(1)",
        "http://public.example.test",
    ],
)
def test_origin_is_canonical_and_has_no_userinfo_or_fragment(origin):
    with pytest.raises(ValueError):
        settings(origin=origin)


def test_expired_sending_is_reported_as_unknown_in_recovery_run(queued):
    from core.delivery_notifications import DeliveryNotificationWorker

    ticket = queued.claim(now=NOW)
    queued.begin_send(ticket, now=NOW)
    sender = Sender(queued.path)
    result = DeliveryNotificationWorker(
        queued, settings(), sender=sender, now=lambda: NOW + timedelta(seconds=91)
    ).run_pending()
    assert result["unknown"] == 1 and sender.calls == []


def test_two_workers_contend_for_one_durable_claim(queued):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from core.checkout_store import CheckoutUnavailable

    def claim():
        for _ in range(20):
            try:
                return B2BOutbox(queued.path).claim(now=NOW)
            except CheckoutUnavailable:
                time.sleep(0.01)
        pytest.fail("Claim did not recover from bounded contention")

    with ThreadPoolExecutor(max_workers=2) as pool:
        tickets = list(pool.map(lambda _: claim(), range(2)))
    assert sum(ticket is not None for ticket in tickets) == 1


def test_provider_exception_never_exposes_details_or_retries(queued, caplog):
    from core.delivery_notifications import DeliveryNotificationWorker

    class BrokenSender:
        def send_email(self, *args, **kwargs):
            raise RuntimeError("PRIVATE-SMTP-DATA")

    result = DeliveryNotificationWorker(
        queued, settings(), sender=BrokenSender(), now=lambda: NOW
    ).run_pending()
    assert result["unknown"] == 1 and "PRIVATE-SMTP-DATA" not in caplog.text
    assert row(queued)["status"] == "unknown"


def test_worker_reports_retry_exhaustion_as_rejected(queued):
    from core.delivery_notifications import DeliveryNotificationWorker

    sender = Sender(queued.path, "retry")
    for attempt in range(5):
        stamp = NOW + timedelta(hours=attempt)
        result = DeliveryNotificationWorker(
            queued, settings(), sender=sender, now=lambda: stamp
        ).run_pending()
    assert result["rejected"] == 1 and result["retry"] == 0
    assert row(queued)["status"] == "rejected"


def test_publication_and_enqueue_commit_atomically(outbox):
    with sqlite3.connect(outbox.path) as conn:
        conn.execute(
            "CREATE TRIGGER fail_enqueue BEFORE INSERT ON b2b_outbox BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        publish(outbox)
    with sqlite3.connect(outbox.path) as conn:
        assert (
            conn.execute("SELECT count(*) FROM package_publications").fetchone()[0] == 0
        )
        assert conn.execute("SELECT count(*) FROM b2b_outbox").fetchone()[0] == 0


def test_enabled_cli_uses_fenced_worker_and_explicit_test_configuration(
    queued, monkeypatch, capsys
):
    import json

    import core.delivery_notifications as module

    sender = Sender(queued.path)
    monkeypatch.setattr(module, "SupervisedSMTP", lambda settings: sender)
    source = {
        "GUNB_B2B_NOTIFY_MODE": "test",
        "GUNB_B2B_NOTIFY_ORIGIN": "https://example.test",
        "GUNB_B2B_NOTIFY_SMTP_HOST": "smtp.example.test",
        "GUNB_B2B_NOTIFY_SMTP_FROM": "service@example.test",
    }
    assert (
        module.main(["--database", str(queued.path), "--limit", "1"], source=source)
        == 0
    )
    assert json.loads(capsys.readouterr().out)["accepted"] == 1
    assert len(sender.calls) == 1 and row(queued)["status"] == "accepted"


def test_smtp_message_preserves_existing_text_and_adds_optional_html():
    from core.email_sender import SMTPEmailSender

    sender = SMTPEmailSender(settings().smtp)
    text = sender._message("person@example.test", "Subject", "plain", "key")
    assert text.get_content_type() == "text/plain"
    both = sender._message(
        "person@example.test", "Subject", "plain", "key", html_body="<p>html</p>"
    )
    assert both.get_content_type() == "multipart/alternative"
    assert both.get_body(preferencelist=("plain",)).get_content().strip() == "plain"
    assert (
        both.get_body(preferencelist=("html",)).get_content().strip() == "<p>html</p>"
    )
