"""Worker z kontrolowanym nadawcą; brak prawdziwego SMTP, Meta i wysyłki."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import sqlite3
import threading

import pytest

from gunb_tool.bot_store import BotStore, UserFilters
from gunb_tool.notification_models import DeliveryResult, parse_time
from gunb_tool.notification_store import NotificationStore
from gunb_tool.notification_worker import NotificationWorker
from gunb_tool.storage import LeadRepository
from tests.bot_helpers import MIETEK, lead
from tests.test_notification_store import enqueue, notification_store, ready, report


class FakeSender:
    def __init__(self, *results, callback=None):
        self.results = list(results)
        self.calls = []
        self.callback = callback

    def send(self, endpoint, part, *, idempotency_key):
        self.calls.append((endpoint, part, idempotency_key))
        if self.callback:
            self.callback()
        result = self.results.pop(0) if self.results else DeliveryResult("accepted", "provider-message-1")
        if isinstance(result, BaseException):
            raise result
        return result


def state(store, ident):
    return store.repo.connection.execute("SELECT * FROM notification_outbox WHERE id = ?", (ident,)).fetchone()


@pytest.mark.parametrize("outcome", ["accepted", "delivered", "read"])
def test_provider_result_and_shown_revisions_are_recorded_atomically(notification_store, outcome):
    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    sender = FakeSender(DeliveryResult(outcome, "receipt-1"))
    assert NotificationWorker(store, {"email": sender}).run_once() == 1
    row = state(store, ident)
    assert row["state"] == outcome and row["provider_message_id"] == "receipt-1"
    history = store.repo.connection.execute("SELECT * FROM notification_deliveries").fetchall()
    assert len(history) == 1
    assert (history[0]["endpoint_id"], history[0]["id_sprawy"], history[0]["outbox_id"], history[0]["outcome"]) == (
        endpoint.id, "A/1", ident, outcome,
    )
    assert store.repo.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
    assert NotificationWorker(store, {"email": sender}).run_once() == 0


def test_retry_keeps_snapshot_and_stable_provider_key(notification_store, clock):
    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    sender = FakeSender(DeliveryResult("retry"), DeliveryResult("accepted", "receipt-2"))
    worker = NotificationWorker(store, {"email": sender})
    assert worker.run_once() == 1 and state(store, ident)["state"] == "retry"
    seconds = (parse_time(state(store, ident)["next_attempt_at"]) - store.repo.now()).total_seconds()
    assert 60 <= seconds <= 75
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 0
    clock.advance(seconds=59)
    assert worker.run_once() == 0
    clock.advance(seconds=16)
    store.repo.upsert(lead("A/1", nazwa_zamierzenia="Zmieniona nazwa bez zmiany statusu"))
    assert worker.run_once() == 1
    assert sender.calls[0][1:] == sender.calls[1][1:]
    assert "Zmieniona nazwa" not in sender.calls[1][1].body
    assert state(store, ident)["attempts"] == 2 and state(store, ident)["state"] == "accepted"


def test_retry_after_is_respected(notification_store, clock):
    store = notification_store
    ident, = enqueue(store, ready(store))
    worker = NotificationWorker(store, {"email": FakeSender(DeliveryResult("retry", retry_after_seconds=200))})
    worker.run_once()
    assert parse_time(state(store, ident)["next_attempt_at"]) == store.repo.now() + timedelta(seconds=200)
    clock.advance(seconds=199)
    assert worker.run_once() == 0
    clock.advance(seconds=1)
    assert worker.run_once() == 1 and state(store, ident)["state"] == "accepted"


def test_exhausted_attempts_disable_only_endpoint_and_do_not_mark_delivery(notification_store, clock):
    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    worker = NotificationWorker(store, {"email": FakeSender(DeliveryResult("retry"), DeliveryResult("retry"))}, max_attempts=2)
    worker.run_once()
    clock.advance(seconds=80)
    worker.run_once()
    assert state(store, ident)["state"] == "failed" and state(store, ident)["attempts"] == 2
    assert not store.get_endpoint(MIETEK, endpoint.id).enabled
    assert BotStore(store.repo).get_user(MIETEK).status == "aktywny"
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 0


@pytest.mark.parametrize("failure", [DeliveryResult("unknown"), DeliveryResult("failed"),
                                     TimeoutError("PRIVATE_TOKEN_IN_PROVIDER_ERROR"), "bad-provider-result"])
def test_uncertain_or_permanent_failure_isolates_endpoint_and_does_not_leak_error(notification_store, caplog, failure):
    store = notification_store
    email = ready(store)
    whatsapp = ready(store, "whatsapp", "+48123456789")
    store.repo.upsert(lead("B/2"))
    first, = enqueue(store, email, "first", "A/1")
    second, = enqueue(store, email, "second", "B/2")
    other, = enqueue(store, whatsapp, "other", "A/1")
    bad, good = FakeSender(failure), FakeSender()
    assert NotificationWorker(store, {"email": bad, "whatsapp": good}).run_once() == 2
    assert state(store, first)["state"] == ("failed" if failure == DeliveryResult("failed") else "unknown")
    assert state(store, second)["state"] == "cancelled" and state(store, other)["state"] == "accepted"
    assert len(bad.calls) == len(good.calls) == 1
    assert not store.get_endpoint(MIETEK, email.id).enabled and store.get_endpoint(MIETEK, whatsapp.id).enabled
    assert "PRIVATE_TOKEN" not in caplog.text and "bad-provider-result" not in caplog.text
    assert BotStore(store.repo).get_user(MIETEK).is_active


@pytest.mark.parametrize("change", ["address", "consent", "pause", "blocked", "access", "filter", "hidden", "revision"])
def test_preflight_revalidates_after_claim_and_cancels_stale_report(notification_store, clock, change):
    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    claim = store.claim(("email",))
    bot = BotStore(store.repo)
    if change == "address":
        store.change_address(MIETEK, endpoint.id, "nowy@example.test")
    elif change == "consent":
        store.revoke_consent(MIETEK, endpoint.id)
    elif change == "pause":
        bot.set_paused(MIETEK, True)
    elif change == "blocked":
        store.repo.connection.execute("UPDATE bot_users SET status = 'zablokowany' WHERE chat_id = ?", (MIETEK,))
    elif change == "access":
        bot.set_access(MIETEK, store.repo.now().isoformat())
    elif change == "filter":
        bot.set_filters(MIETEK, UserFilters(kategorie=("komercyjna",)))
    elif change == "hidden":
        bot.set_lead_state(MIETEK, "A/1", "ukryty")
    else:
        clock.advance(seconds=1)
        store.repo.upsert(lead("A/1", status="odmowa"))
    assert store.prepare_send(claim) is None
    assert state(store, ident)["state"] == "cancelled"


def test_expired_queue_is_not_dispatched(notification_store, clock):
    store = notification_store
    ident, = enqueue(store, ready(store))
    clock.advance(hours=1, seconds=1)
    sender = FakeSender()
    assert NotificationWorker(store, {"email": sender}).run_once() == 1
    assert not sender.calls and state(store, ident)["state"] == "expired"


def test_crash_lease_becomes_unknown_and_late_same_claim_ack_is_allowed(notification_store, clock):
    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    claim = store.claim(("email",))
    clock.advance(seconds=121)
    assert store.claim(("email",)) is None
    assert state(store, ident)["state"] == "unknown" and not store.get_endpoint(MIETEK, endpoint.id).enabled
    assert store.complete(claim, DeliveryResult("accepted", "late-receipt"))
    assert state(store, ident)["state"] == "accepted"
    assert not store.get_endpoint(MIETEK, endpoint.id).enabled


def test_foreign_or_already_completed_claim_cannot_overwrite_result(notification_store):
    store = notification_store
    ident, = enqueue(store, ready(store))
    claim = store.claim(("email",))
    forged = replace(claim, claim_owner="foreign-owner")
    assert not store.complete(forged, DeliveryResult("accepted", "foreign"))
    assert store.prepare_send(forged) is None
    assert state(store, ident)["state"] == "sending"
    assert store.complete(claim, DeliveryResult("accepted", "valid"))
    assert not store.complete(claim, DeliveryResult("failed"))
    assert state(store, ident)["provider_message_id"] == "valid"


def test_only_one_claim_per_endpoint_but_other_channels_continue(notification_store):
    store = notification_store
    email = ready(store)
    whatsapp = ready(store, "whatsapp", "+48123456789")
    store.repo.upsert(lead("B/2"))
    first, = enqueue(store, email, "first", "A/1")
    second, = enqueue(store, email, "second", "B/2")
    other, = enqueue(store, whatsapp, "other", "A/1")
    a = store.claim(("email",))
    assert a.id == first and store.claim(("email",)) is None
    assert store.claim(("email", "whatsapp")).id == other
    assert store.complete(a, DeliveryResult("accepted"))
    assert store.claim(("email",)).id == second


def test_two_connections_cannot_claim_same_task(notification_store, tmp_path, clock):
    enqueue(notification_store, ready(notification_store))
    barrier = threading.Barrier(2)

    def claim(_):
        with LeadRepository(tmp_path / "notifications.sqlite", now=clock.now_utc) as repo:
            store = NotificationStore(repo)
            barrier.wait(timeout=5)
            return store.claim(("email",))

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, (1, 2)))
    assert sum(item is not None for item in claims) == 1
    assert notification_store.repo.connection.execute("SELECT attempts FROM notification_outbox").fetchone()[0] == 1


def test_worker_connection_restart_retains_retry_snapshot(notification_store, tmp_path, clock):
    store = notification_store
    ident, = enqueue(store, ready(store))
    first = FakeSender(DeliveryResult("retry"))
    with LeadRepository(tmp_path / "notifications.sqlite", now=clock.now_utc) as repo:
        NotificationWorker(NotificationStore(repo), {"email": first}).run_once()
    clock.advance(seconds=80)
    store.repo.upsert(lead("A/1", nazwa_zamierzenia="Nowy opis"))
    second = FakeSender()
    with LeadRepository(tmp_path / "notifications.sqlite", now=clock.now_utc) as repo:
        assert NotificationWorker(NotificationStore(repo), {"email": second}).run_once() == 1
    assert first.calls[0][1:] == second.calls[0][1:]
    assert state(store, ident)["state"] == "accepted"


def test_dispatch_holds_no_write_transaction(notification_store, tmp_path, clock):
    store = notification_store
    enqueue(store, ready(store))

    def concurrent_write():
        assert not store.repo.connection.in_transaction
        with LeadRepository(tmp_path / "notifications.sqlite", now=clock.now_utc) as repo:
            BotStore(repo).set_company(MIETEK, "Fikcyjna firma")

    assert NotificationWorker(store, {"email": FakeSender(callback=concurrent_write)}).run_once() == 1
    assert BotStore(store.repo).get_user(MIETEK).firma == "Fikcyjna firma"
    assert store.repo.connection.execute("SELECT state FROM notification_outbox").fetchone()[0] == "accepted"


def test_worker_refuses_external_transaction(notification_store):
    store = notification_store
    ident, = enqueue(store, ready(store))
    sender = FakeSender()
    with store.repo.transaction():
        with pytest.raises(RuntimeError):
            NotificationWorker(store, {"email": sender}).run_once()
    assert not sender.calls and state(store, ident)["state"] == "queued"


def test_corrupt_payload_is_quarantined_without_dispatch(notification_store):
    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    store.repo.connection.execute("UPDATE notification_outbox SET payload = ? WHERE id = ?", ('{"version":99}', ident))
    sender = FakeSender()
    assert NotificationWorker(store, {"email": sender}).run_once() == 1
    assert not sender.calls and state(store, ident)["state"] == "failed"
    assert not store.get_endpoint(MIETEK, endpoint.id).enabled


def test_address_changed_after_dispatch_does_not_retarget_ack_or_retry(notification_store):
    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    sender = FakeSender(callback=lambda: store.change_address(MIETEK, endpoint.id, "nowy@example.test"))
    worker = NotificationWorker(store, {"email": sender})
    assert worker.run_once() == 1
    assert sender.calls[0][0].address == "odbiorca@example.test" and state(store, ident)["state"] == "accepted"
    assert not store.get_endpoint(MIETEK, endpoint.id).enabled and worker.run_once() == 0


def test_endpoint_deleted_during_dispatch_is_not_recreated_by_ack(notification_store):
    store = notification_store
    endpoint = ready(store)
    enqueue(store, endpoint)
    sender = FakeSender(callback=lambda: store.delete_endpoint(MIETEK, endpoint.id))
    assert NotificationWorker(store, {"email": sender}).run_once() == 1
    assert all(store.repo.connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0
               for name in ("notification_endpoints", "notification_outbox", "notification_deliveries"))


def test_loop_is_bounded_and_unregistered_channel_is_untouched(notification_store):
    store = notification_store
    endpoint = ready(store)
    store.repo.upsert(lead("B/2"))
    a, = enqueue(store, endpoint, "first", "A/1")
    b, = enqueue(store, endpoint, "second", "B/2")
    sender = FakeSender()
    assert NotificationWorker(store, {}).run_once() == 0
    assert NotificationWorker(store, {"whatsapp": sender}).run_once() == 0
    worker = NotificationWorker(store, {"email": sender})
    assert worker.run_once(limit=1) == 1 and len(sender.calls) == 1
    assert state(store, a)["state"] == "accepted" and state(store, b)["state"] == "queued"
    for bad in (0, 101, True):
        with pytest.raises(ValueError):
            worker.run_once(limit=bad)


def test_multipart_report_records_every_ref_only_in_accepted_parts(notification_store):
    store = notification_store
    endpoint = ready(store)
    for key in ("B/2", "C/3"):
        store.repo.upsert(lead(key))
    parts = report(store, endpoint, "A/1", "B/2", "C/3", part_size=2)
    ids = store.enqueue(MIETEK, endpoint.id, "multipart", parts,
                        expires_at=store.repo.now() + timedelta(hours=1))
    sender = FakeSender(DeliveryResult("accepted"), DeliveryResult("failed"))
    assert NotificationWorker(store, {"email": sender}).run_once() == 2
    history = store.repo.connection.execute("SELECT id_sprawy, outbox_id FROM notification_deliveries").fetchall()
    assert {(row["id_sprawy"], row["outbox_id"]) for row in history} == {("A/1", ids[0]), ("B/2", ids[0])}
    assert [state(store, ident)["state"] for ident in ids] == ["accepted", "failed"]


def test_receipt_database_failure_rolls_back_all_refs_and_outbox(notification_store):
    store = notification_store
    endpoint = ready(store)
    store.repo.upsert(lead("B/2"))
    ident, = enqueue(store, endpoint, "atomic-receipt", "A/1", "B/2")
    claim = store.claim(("email",))
    assert store.prepare_send(claim) is not None
    store.repo.connection.execute(
        "CREATE TEMP TRIGGER reject_second_receipt BEFORE INSERT ON notification_deliveries"
        " WHEN NEW.id_sprawy = 'B/2' BEGIN SELECT RAISE(ABORT, 'simulated receipt failure'); END",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.complete(claim, DeliveryResult("accepted", "receipt"))
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 0
    assert state(store, ident)["state"] == "sending" and state(store, ident)["provider_message_id"] is None
    store.repo.connection.execute("DROP TRIGGER reject_second_receipt")
    assert store.complete(claim, DeliveryResult("accepted", "receipt"))
    assert state(store, ident)["state"] == "accepted"
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 2


def test_external_transaction_cannot_commit_partial_receipt(notification_store):
    store = notification_store
    endpoint = ready(store)
    store.repo.upsert(lead("B/2"))
    ident, = enqueue(store, endpoint, "atomic-receipt", "A/1", "B/2")
    claim = store.claim(("email",))
    store.repo.connection.execute(
        "CREATE TEMP TRIGGER reject_second_receipt BEFORE INSERT ON notification_deliveries"
        " WHEN NEW.id_sprawy = 'B/2' BEGIN SELECT RAISE(ABORT, 'simulated receipt failure'); END",
    )
    failure = None
    with store.repo.transaction():
        try:
            store.complete(claim, DeliveryResult("accepted", "receipt"))
        except (sqlite3.IntegrityError, RuntimeError) as exc:
            failure = exc
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 0
    assert isinstance(failure, RuntimeError)
    assert state(store, ident)["state"] == "sending" and state(store, ident)["provider_message_id"] is None
    store.repo.connection.execute("DROP TRIGGER reject_second_receipt")
    assert store.complete(claim, DeliveryResult("accepted", "receipt"))
    assert state(store, ident)["state"] == "accepted"
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 2


@pytest.mark.parametrize("operation", ["claim", "prepare"])
def test_claim_and_preflight_refuse_external_transaction(notification_store, operation):
    store = notification_store
    ident, = enqueue(store, ready(store))
    claim = store.claim(("email",)) if operation == "prepare" else None
    before = dict(state(store, ident))
    with store.repo.transaction():
        with pytest.raises(RuntimeError, match="transakcj"):
            store.prepare_send(claim) if claim else store.claim(("email",))
    assert dict(state(store, ident)) == before
    result = store.prepare_send(claim) if claim else store.claim(("email",))
    assert result is not None


def test_process_exit_during_provider_call_is_never_automatically_resent(notification_store, clock):
    class SimulatedProcessExit(BaseException):
        pass

    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    first = FakeSender(SimulatedProcessExit())
    with pytest.raises(SimulatedProcessExit):
        NotificationWorker(store, {"email": first}).run_once()
    assert len(first.calls) == 1 and state(store, ident)["state"] == "sending"
    clock.advance(seconds=121)
    restarted = FakeSender()
    assert NotificationWorker(store, {"email": restarted}).run_once() == 0
    assert not restarted.calls and state(store, ident)["state"] == "unknown"
    assert not store.get_endpoint(MIETEK, endpoint.id).enabled
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 0
