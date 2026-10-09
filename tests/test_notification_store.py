"""Rzeczywisty SQLite; tylko fikcyjne adresy, bez adapterów sieciowych."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import threading

import pytest

from gunb_tool.bot_store import BotStore, UserFilters
from gunb_tool.notification_models import ReportPart
from gunb_tool.notification_reports import build_report
from gunb_tool.notification_store import NotificationError, NotificationStore
from gunb_tool.scoring import HOT
from gunb_tool.storage import LeadRepository
from tests.bot_helpers import MIETEK, OBCY, lead


@pytest.fixture
def notification_store(tmp_path, clock):
    with LeadRepository(tmp_path / "notifications.sqlite", now=clock.now_utc) as repo:
        bot = BotStore(repo)
        bot.register(MIETEK, "Odbiorca", None, status="aktywny", backlog_days=7)
        bot.set_access(MIETEK, (repo.now() + timedelta(days=2)).isoformat())
        repo.upsert(lead("A/1"))
        yield NotificationStore(repo)


def ready(store, channel="email", address="odbiorca@example.test"):
    endpoint = store.add_endpoint(MIETEK, channel, address)
    endpoint = store.record_consent(MIETEK, endpoint.id, expected_version=endpoint.version, source="test_form")
    endpoint = store.record_verification(MIETEK, endpoint.id, expected_version=endpoint.version)
    return store.set_enabled(MIETEK, endpoint.id, True, expected_version=endpoint.version)


def report(store, endpoint, *ids, part_size=20):
    return build_report(endpoint, [store.repo.get(key) for key in (ids or ("A/1",))],
                        now=store.repo.now(), part_size=part_size)


def enqueue(store, endpoint, key="report:one", *ids):
    return store.enqueue(MIETEK, endpoint.id, key, report(store, endpoint, *ids),
                         expires_at=store.repo.now() + timedelta(hours=1))


def history(store, endpoint, case="A/1"):
    inv = store.repo.get(case)
    store.repo.connection.execute(
        "INSERT INTO notification_deliveries"
        " (endpoint_id, id_sprawy, revision, kind, outcome, processed_at) VALUES (?, ?, ?, 'report', 'accepted', ?)",
        (endpoint.id, case, inv.status_zmieniony, store.repo.now().isoformat()),
    )


def test_external_transaction_cannot_commit_partial_enqueue(notification_store):
    store = notification_store
    endpoint = ready(store)
    store.repo.upsert(lead("B/2"))
    parts = report(store, endpoint, "A/1", "B/2", part_size=1)
    broken = (parts[0], replace(parts[1], body="\n" * 32767 + "x"))
    failure = None
    with store.repo.transaction():
        BotStore(store.repo).set_company(MIETEK, "Zewnętrzna operacja")
        try:
            store.enqueue(MIETEK, endpoint.id, "atomic-batch", broken,
                          expires_at=store.repo.now() + timedelta(hours=1))
        except (ValueError, RuntimeError) as exc:
            failure = exc
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
    assert isinstance(failure, RuntimeError)
    assert BotStore(store.repo).get_user(MIETEK).firma == "Zewnętrzna operacja"
    ids = store.enqueue(MIETEK, endpoint.id, "atomic-batch", parts,
                        expires_at=store.repo.now() + timedelta(hours=1))
    assert len(ids) == 2
    assert store.enqueue(MIETEK, endpoint.id, "atomic-batch", parts,
                         expires_at=store.repo.now() + timedelta(hours=1)) == ids


@pytest.mark.parametrize("operation", ["add", "verify", "consent", "enable", "address", "revoke", "delete"])
def test_endpoint_mutations_refuse_external_transaction_before_changes(notification_store, operation):
    store = notification_store
    endpoint = store.add_endpoint(MIETEK, "email", "odbiorca@example.test")
    actions = {
        "add": lambda: store.add_endpoint(MIETEK, "whatsapp", "+48123456789"),
        "verify": lambda: store.record_verification(MIETEK, endpoint.id, expected_version=endpoint.version),
        "consent": lambda: store.record_consent(MIETEK, endpoint.id, expected_version=endpoint.version, source="form"),
        "enable": lambda: store.set_enabled(MIETEK, endpoint.id, False, expected_version=endpoint.version),
        "address": lambda: store.change_address(MIETEK, endpoint.id, "nowy@example.test"),
        "revoke": lambda: store.revoke_consent(MIETEK, endpoint.id),
        "delete": lambda: store.delete_endpoint(MIETEK, endpoint.id),
    }
    with store.repo.transaction():
        with pytest.raises(RuntimeError, match="transakcj"):
            actions[operation]()
    assert store.get_endpoint(MIETEK, endpoint.id) == endpoint
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_endpoints").fetchone()[0] == 1


def test_report_round_trip_keeps_facts_and_revisions(notification_store):
    store = notification_store
    endpoint = ready(store)
    part, = report(store, endpoint)
    restored = ReportPart.from_json(part.to_json())
    assert restored == part
    assert restored.chat_id == MIETEK and restored.endpoint_version == endpoint.version
    assert [(ref.id_sprawy, ref.revision) for ref in restored.leads] == [
        ("A/1", store.repo.get("A/1").status_zmieniony),
    ]
    assert "inwestycje do sprawdzenia" in restored.body
    assert "Budowa budynku mieszkalnego jednorodzinnego" in restored.body
    assert "pozwolenie na budowę" in restored.body
    assert "2026-09-28" in restored.body
    assert "https://www.google.com/maps?q=" in restored.body
    assert "etapu robót" in restored.body
    assert "odbiorca@example.test" not in restored.body


def test_report_sanitizes_untrusted_fields_and_map_urls(notification_store):
    store = notification_store
    store.repo.upsert(lead("A/1", nazwa_zamierzenia="Dom\r\n\x00HALA\u202e" + "a" * 2000,
                           google_maps_url="https://evil.example.test/maps?q=123"))
    part, = report(store, ready(store))
    assert "evil.example" not in part.body and "\x00" not in part.body and "\u202e" not in part.body
    assert "Dom HALA" in part.body and "a" * 500 not in part.body
    assert "\r" not in part.title and "\n" not in part.title


def test_report_parts_are_bounded_and_do_not_omit_references(notification_store):
    store = notification_store
    store.repo.upsert(lead("B/2"))
    parts = report(store, ready(store), "A/1", "B/2", part_size=1)
    assert len(parts) == 2 and [part.leads[0].id_sprawy for part in parts] == ["A/1", "B/2"]
    assert build_report(ready(store, address="empty@example.test"), [], now=store.repo.now()) == ()
    with pytest.raises(ValueError):
        build_report(ready(store, address="long@example.test"), [store.repo.get("A/1")] * 21, now=store.repo.now())


def test_unicode_report_splits_to_keep_all_leads_within_byte_limits(notification_store):
    store = notification_store
    ids = [f"unicode-{number}" for number in range(20)]
    for ident in ids:
        store.repo.upsert(lead(ident, nazwa_zamierzenia="🏗" * 200, miejscowosc="🏠" * 200, status="nieznany" * 40))
    parts = report(store, ready(store), *ids)
    assert len(parts) > 1
    assert [ref.id_sprawy for part in parts for ref in part.leads] == ids
    assert all(len(part.body.encode("utf-8")) <= 32768 for part in parts)


@pytest.mark.parametrize("change", [{"version": 99}, {"chat_id": True}, {"leads": []},
                                    {"title": "Raport\nBcc: obcy"}, {"body": "a" * 65537}])
def test_corrupt_or_unbounded_snapshot_is_rejected(notification_store, change):
    part, = report(notification_store, ready(notification_store))
    raw = json.loads(part.to_json())
    raw.update(change)
    with pytest.raises(ValueError):
        ReportPart.from_json(json.dumps(raw))


@pytest.mark.parametrize("channel,address,wanted", [
    ("email", " Nazwa+tag@EXAMPLE.TEST ", "Nazwa+tag@example.test"),
    ("whatsapp", " +48123456789 ", "+48123456789"),
])
def test_address_is_normalized_but_new_endpoint_is_disabled(notification_store, channel, address, wanted):
    endpoint = notification_store.add_endpoint(MIETEK, channel, address)
    assert endpoint.address == wanted and not endpoint.enabled
    assert endpoint.verified_at is None and endpoint.consent_at is None and endpoint.version == 1


@pytest.mark.parametrize("channel,address", [
    ("email", "a@example.test\r\nBcc: x@example.test"), ("email", "dwa adresy@example.test"),
    ("email", "a@@example.test"), ("email", "a@-example.test"), ("email", "łukasz@example.test"),
    ("email", "a" * 65 + "@example.test"), ("whatsapp", "48123456789"),
    ("whatsapp", "+012345678"), ("whatsapp", "+48123 456789"), ("whatsapp", "+1234567890123456"),
])
def test_unsafe_address_is_rejected(notification_store, channel, address):
    with pytest.raises(ValueError):
        notification_store.add_endpoint(MIETEK, channel, address)
    assert notification_store.repo.connection.execute("SELECT COUNT(*) FROM notification_endpoints").fetchone()[0] == 0


def test_activation_requires_verification_and_current_consent(notification_store):
    store = notification_store
    endpoint = store.add_endpoint(MIETEK, "email", "odbiorca@example.test")
    with pytest.raises(NotificationError):
        store.set_enabled(MIETEK, endpoint.id, True, expected_version=endpoint.version)
    endpoint = store.record_verification(MIETEK, endpoint.id, expected_version=endpoint.version)
    with pytest.raises(NotificationError):
        store.set_enabled(MIETEK, endpoint.id, True, expected_version=endpoint.version)
    endpoint = store.record_consent(MIETEK, endpoint.id, expected_version=endpoint.version, source="test_form")
    endpoint = store.set_enabled(MIETEK, endpoint.id, True, expected_version=endpoint.version)
    assert endpoint.enabled and endpoint.activated_at and endpoint.consent_source == "test_form"


@pytest.mark.parametrize("operation", ["read", "verify", "consent", "enable", "address", "revoke", "delete"])
def test_other_owner_cannot_read_or_change_endpoint(notification_store, operation):
    store = notification_store
    endpoint = ready(store)
    actions = {
        "read": lambda: store.get_endpoint(OBCY, endpoint.id),
        "verify": lambda: store.record_verification(OBCY, endpoint.id, expected_version=endpoint.version),
        "consent": lambda: store.record_consent(OBCY, endpoint.id, expected_version=endpoint.version, source="test"),
        "enable": lambda: store.set_enabled(OBCY, endpoint.id, False, expected_version=endpoint.version),
        "address": lambda: store.change_address(OBCY, endpoint.id, "obcy@example.test"),
        "revoke": lambda: store.revoke_consent(OBCY, endpoint.id),
        "delete": lambda: store.delete_endpoint(OBCY, endpoint.id),
    }
    with pytest.raises(NotificationError):
        actions[operation]()
    assert store.get_endpoint(MIETEK, endpoint.id) == endpoint


def test_new_address_invalidates_proofs_and_queued_snapshot(notification_store):
    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    raw = store.repo.connection.execute("SELECT payload FROM notification_outbox WHERE id = ?", (ident,)).fetchone()[0]
    changed = store.change_address(MIETEK, endpoint.id, "nowy@example.test")
    assert changed.version > endpoint.version and not changed.enabled
    assert changed.verified_at is None and changed.consent_at is None and changed.activated_at is None
    row = store.repo.connection.execute("SELECT state, payload FROM notification_outbox WHERE id = ?", (ident,)).fetchone()
    assert row["state"] == "cancelled" and row["payload"] == raw
    with pytest.raises(NotificationError):
        store.record_verification(MIETEK, endpoint.id, expected_version=endpoint.version)


def test_revocation_cancels_queue_and_does_not_allow_reactivation(notification_store):
    store = notification_store
    endpoint = ready(store)
    ident, = enqueue(store, endpoint)
    revoked = store.revoke_consent(MIETEK, endpoint.id)
    assert revoked.consent_revoked_at and not revoked.enabled
    assert store.repo.connection.execute("SELECT state FROM notification_outbox WHERE id = ?", (ident,)).fetchone()[0] == "cancelled"
    with pytest.raises(NotificationError):
        store.set_enabled(MIETEK, endpoint.id, True, expected_version=revoked.version)


def test_telegram_delivery_does_not_suppress_channels(notification_store):
    store = notification_store
    email = ready(store)
    whatsapp = ready(store, "whatsapp", "+48123456789")
    BotStore(store.repo).record_delivery(MIETEK, [store.repo.get("A/1")], "raport")
    since = store.repo.now() - timedelta(days=1)
    assert [i.id_sprawy for i in store.candidates(MIETEK, email.id, since)] == ["A/1"]
    assert [i.id_sprawy for i in store.candidates(MIETEK, whatsapp.id, since)] == ["A/1"]
    history(store, email)
    assert store.candidates(MIETEK, email.id, since) == []
    assert len(store.candidates(MIETEK, whatsapp.id, since)) == 1


@pytest.mark.parametrize("column,value", [("activated_at", "nieznana-data"),
                                         ("activated_at", "2099-01-01T00:00:00+00:00"),
                                         ("consent_source", "   "), ("address", "nie jest adresem")])
def test_invalid_imported_proofs_or_address_do_not_open_dispatch(notification_store, column, value):
    store = notification_store
    endpoint = ready(store)
    # Nazwy pochodzą wyłącznie z literalnej parametryzacji testu, wartości są bindowane.
    store.repo.connection.execute(f"UPDATE notification_endpoints SET {column} = ? WHERE id = ?", (value, endpoint.id))
    assert store.candidates(MIETEK, endpoint.id, store.repo.now() - timedelta(days=1)) == []
    with pytest.raises(NotificationError):
        enqueue(store, endpoint)


@pytest.mark.parametrize("access,admins", [("open", ()), ("approval", (MIETEK,))])
def test_open_access_and_configured_admin_use_current_bot_access_policy(notification_store, access, admins):
    base = notification_store
    BotStore(base.repo).revoke_access(MIETEK)
    store = NotificationStore(base.repo, access=access, admins=admins)
    assert enqueue(store, ready(store))


def test_snapshot_owner_and_version_are_checked_before_enqueue(notification_store):
    store = notification_store
    endpoint = ready(store)
    part, = report(store, endpoint)
    for changed in (replace(part, chat_id=OBCY), replace(part, endpoint_version=endpoint.version + 1)):
        with pytest.raises(NotificationError):
            store.enqueue(MIETEK, endpoint.id, "bad-snapshot", (changed,),
                          expires_at=store.repo.now() + timedelta(hours=1))
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


def test_filters_are_applied_before_candidate_limit(notification_store):
    store = notification_store
    endpoint = ready(store)
    BotStore(store.repo).set_filters(MIETEK, UserFilters(kategorie=("komercyjna",)))
    for number in range(12):
        store.repo.upsert(lead(f"not-matching-{number}"))
    store.repo.upsert(lead("B/2", kategoria="komercyjna"))
    assert [i.id_sprawy for i in store.candidates(MIETEK, endpoint.id, store.repo.now() - timedelta(days=1), limit=1)] == ["B/2"]


def test_hot_only_uses_existing_scoring_marker(notification_store):
    store = notification_store
    endpoint = ready(store)
    BotStore(store.repo).set_hot_only(MIETEK, True)
    store.repo.upsert(lead("B/2", priorytet=HOT))
    assert [i.id_sprawy for i in store.candidates(MIETEK, endpoint.id, store.repo.now() - timedelta(days=1))] == ["B/2"]


def test_repeated_job_keeps_original_snapshot_and_pending_leads_are_reserved(notification_store):
    store = notification_store
    endpoint = ready(store)
    ids = enqueue(store, endpoint)
    first, = report(store, endpoint)
    changed = replace(first, body="Zmodyfikowana treść")
    assert store.enqueue(MIETEK, endpoint.id, "report:one", (changed,),
                         expires_at=store.repo.now() + timedelta(hours=1)) == ids
    assert store.repo.connection.execute("SELECT payload FROM notification_outbox").fetchone()[0] == first.to_json()
    assert enqueue(store, endpoint, "another-job") == ()
    assert store.candidates(MIETEK, endpoint.id, store.repo.now() - timedelta(days=1)) == []


@pytest.mark.parametrize("change", ["pause", "expired", "revoked_access", "blocked", "filters", "hidden", "noise", "revision"])
def test_enqueue_revalidates_account_and_leads(notification_store, clock, change):
    store = notification_store
    endpoint = ready(store)
    parts = report(store, endpoint)
    bot = BotStore(store.repo)
    if change == "pause":
        bot.set_paused(MIETEK, True)
    elif change == "expired":
        bot.set_access(MIETEK, store.repo.now().isoformat())
    elif change == "revoked_access":
        bot.revoke_access(MIETEK)
    elif change == "blocked":
        store.repo.connection.execute("UPDATE bot_users SET status = 'zablokowany' WHERE chat_id = ?", (MIETEK,))
    elif change == "filters":
        bot.set_filters(MIETEK, UserFilters(kategorie=("komercyjna",)))
    elif change == "hidden":
        bot.set_lead_state(MIETEK, "A/1", "ukryty")
    elif change == "noise":
        store.repo.upsert(lead("A/1", is_noise=True))
    else:
        clock.advance(seconds=1)
        store.repo.upsert(lead("A/1", status="odmowa"))
    with pytest.raises(NotificationError):
        store.enqueue(MIETEK, endpoint.id, "report:one", parts, expires_at=store.repo.now() + timedelta(hours=1))
    assert store.repo.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("hours", [0, 25])
def test_expiry_must_be_future_and_bounded(notification_store, hours):
    store = notification_store
    endpoint = ready(store)
    with pytest.raises(ValueError):
        store.enqueue(MIETEK, endpoint.id, "report:one", report(store, endpoint),
                      expires_at=store.repo.now() + timedelta(hours=hours))


def test_queue_limit_is_per_endpoint_and_known_history_is_not_requeued(notification_store):
    original = notification_store
    store = NotificationStore(original.repo, max_pending_parts=1)
    endpoint = ready(store)
    assert enqueue(store, endpoint)
    store.repo.upsert(lead("B/2"))
    with pytest.raises(NotificationError):
        enqueue(store, endpoint, "report:two", "B/2")
    other = ready(store, address="drugi@example.test")
    history(store, other, "A/1")
    assert enqueue(store, other, "already-accepted", "A/1") == ()
    assert enqueue(store, other, "new-for-other", "B/2")


def test_concurrent_events_cannot_reserve_same_revision(notification_store, tmp_path, clock):
    endpoint = ready(notification_store)
    barrier = threading.Barrier(2)

    def add(key):
        with LeadRepository(tmp_path / "notifications.sqlite", now=clock.now_utc) as repo:
            store = NotificationStore(repo)
            parts = report(store, endpoint)
            barrier.wait(timeout=5)
            return store.enqueue(MIETEK, endpoint.id, key, parts, expires_at=repo.now() + timedelta(hours=1))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(add, ("job:one", "job:two")))
    assert sorted(map(len, results)) == [0, 1]
    assert notification_store.repo.connection.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1
