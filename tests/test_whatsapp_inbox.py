"""Signed synthetic bodies and real temporary SQLite, never network/production."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading

import pytest

from gunb_tool import whatsapp_inbox
from gunb_tool.bot_store import BotStore
from gunb_tool.notification_models import DeliveryResult
from gunb_tool.notification_store import NotificationStore
from gunb_tool.storage import LeadRepository
from gunb_tool.whatsapp_webhook import WebhookError
from tests.bot_helpers import MIETEK, OBCY, lead
from tests.test_notification_store import enqueue, notification_store, ready
from tests.test_whatsapp_webhook import SECRET, TOKEN, payload, settings, signature, value

PHONE = '48111222333'


def status(outcome='delivered', ident='wamid.test1', recipient=PHONE, timestamp='1700000000'):
    return {'id': ident, 'recipient_id': recipient, 'status': outcome, 'timestamp': timestamp}


def signed(*statuses):
    data = payload()
    value(data)['statuses'] = list(statuses or (status(),))
    raw = json.dumps(data, separators=(',', ':')).encode('utf-8')
    return raw, signature(raw)


def inbox(store, **kwargs):
    return whatsapp_inbox.WhatsAppInbox(store, settings(), **kwargs)


def events(store):
    return store.repo.connection.execute('SELECT * FROM notification_webhook_events ORDER BY id').fetchall()


def test_signed_intake_commits_only_minimal_private_event(notification_store, caplog):
    store = notification_store
    data = payload()
    value(data)['statuses'][0]['errors'] = [{'message': 'PRIVATE_PROVIDER_ERROR'}]
    value(data)['messages'] = [{'text': {'body': 'PRIVATE_INCOMING_MESSAGE'}}]
    raw = json.dumps(data).encode('utf-8')
    assert inbox(store).receive(raw, signature(raw)) == 1
    row, = events(store)
    assert row['channel'] == 'whatsapp' and row['provider_message_id'] == 'wamid.test1'
    assert row['endpoint_id'] is None and row['processed_at'] is None
    assert row['event_key'].startswith('meta:') and len(row['event_key']) == 69
    assert json.loads(row['payload']) == {
        'version': 1, 'outcome': 'delivered', 'timestamp': '2023-11-14T22:13:20.000000+00:00',
        'recipient_digest': hashlib.sha256(b'wamid.test1\x0048111222333').hexdigest(),
    }
    assert not store.repo.connection.in_transaction
    saved = json.dumps(dict(row)) + caplog.text + repr(inbox(store))
    for private in (PHONE, SECRET, TOKEN, 'PRIVATE_PROVIDER_ERROR', 'PRIVATE_INCOMING_MESSAGE'):
        assert private not in saved
    # Separate connection sees the committed row immediately, without any shared in-memory cache.
    with LeadRepository(store.repo._file, now=store.repo.now) as reopened:
        assert reopened.connection.execute('SELECT COUNT(*) FROM notification_webhook_events').fetchone()[0] == 1
        assert inbox(NotificationStore(reopened)).receive(raw, signature(raw)) == 0


def test_replay_does_not_extend_first_receipt_or_reset_processing(notification_store, clock):
    store = notification_store
    receiver = inbox(store)
    assert receiver.receive(*signed()) == 1
    before = dict(events(store)[0])
    store.repo.connection.execute('UPDATE notification_webhook_events SET processed_at = ?', (before['received_at'],))
    clock.advance(days=1)
    assert inbox(store).receive(*signed()) == 0
    after = dict(events(store)[0])
    assert after == {**before, 'processed_at': before['received_at']}


@pytest.mark.parametrize('kind', ['signature', 'malformed', 'oversize', 'invalid_status', 'mixed_batch'])
def test_invalid_authenticated_batch_never_writes(notification_store, kind):
    store = notification_store
    raw, header = signed()
    if kind == 'signature':
        header = 'sha256=' + '0' * 64
    elif kind == 'malformed':
        raw, header = b'{', signature(b'{')
    elif kind == 'oversize':
        raw = b' ' * 65537
        header = signature(raw)
    elif kind == 'invalid_status':
        raw, header = signed(status(ident='untrusted'))
    elif kind == 'mixed_batch':
        raw, header = signed(status(), status(ident='bad'))
    before = store.repo.connection.total_changes
    with pytest.raises(WebhookError, match='Niepoprawny webhook') as caught:
        inbox(store).receive(raw, header)
    assert not events(store) and store.repo.connection.total_changes == before
    assert PHONE not in str(caught.value) and caught.value.__cause__ is None


@pytest.mark.parametrize('capacity', [True, 0, -1, 100001, 1.0, '10', None])
def test_capacity_must_be_bounded_integer(notification_store, capacity):
    with pytest.raises(ValueError):
        inbox(notification_store, max_events=capacity)


def test_duplicates_succeed_at_capacity_but_novel_batch_is_atomic(notification_store):
    store = notification_store
    receiver = inbox(store, max_events=2)
    assert receiver.receive(*signed()) == 1
    with pytest.raises(whatsapp_inbox.WebhookInboxFull):
        receiver.receive(*signed(status(ident='wamid.second'), status(ident='wamid.third')))
    assert len(events(store)) == 1
    assert receiver.receive(*signed(status(), status(ident='wamid.second'))) == 1
    assert receiver.receive(*signed(status(), status(ident='wamid.second'))) == 0
    assert len(events(store)) == 2
    with pytest.raises(whatsapp_inbox.WebhookInboxFull):
        receiver.receive(*signed(status(ident='wamid.third')))


def test_processed_and_other_channel_rows_count_toward_whole_table_budget(notification_store):
    store = notification_store
    store.repo.connection.execute(
        "INSERT INTO notification_webhook_events(channel,event_key,payload,received_at,processed_at)"
        " VALUES ('email','synthetic','{}','2026-10-11','2026-10-11')")
    with pytest.raises(whatsapp_inbox.WebhookInboxFull):
        inbox(store, max_events=1).receive(*signed())
    assert len(events(store)) == 1


def test_intake_refuses_external_transaction_without_committing_owner_work(notification_store):
    store = notification_store
    with store.repo.transaction():
        BotStore(store.repo).set_company(MIETEK, 'Unrelated transaction')
        with pytest.raises(RuntimeError, match='transakcj'):
            inbox(store).receive(*signed())
        assert store.repo.connection.in_transaction
    assert not events(store)
    assert BotStore(store.repo).get_user(MIETEK).firma == 'Unrelated transaction'


def test_database_failure_rolls_back_whole_batch_before_return(notification_store):
    store = notification_store
    store.repo.connection.execute(
        "CREATE TRIGGER fail_second BEFORE INSERT ON notification_webhook_events"
        " WHEN NEW.provider_message_id = 'wamid.reject'"
        " BEGIN SELECT RAISE(ABORT, 'synthetic insert failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic insert failure'):
        inbox(store).receive(*signed(status(), status(ident='wamid.reject')))
    assert not events(store) and not store.repo.connection.in_transaction


def test_concurrent_replays_on_independent_connections_insert_once(notification_store):
    store = notification_store
    gate = threading.Barrier(4)
    raw, header = signed()
    def receive(_):
        with LeadRepository(store.repo._file, now=store.repo.now) as own_repo:
            receiver = inbox(NotificationStore(own_repo))
            gate.wait(timeout=10)
            return receiver.receive(raw, header)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(receive, range(4)))
    assert sorted(results) == [0, 0, 0, 1]
    assert len(events(store)) == 1


def acknowledged(store, *, channel='whatsapp', address='+' + PHONE, ident='wamid.test1'):
    endpoint = ready(store, channel=channel, address=address)
    ident_outbox, = enqueue(store, endpoint)
    claim = store.claim([channel])
    assert claim.id == ident_outbox and store.prepare_send(claim) is not None
    assert store.complete(claim, DeliveryResult('accepted', provider_message_id=ident))
    return endpoint, ident_outbox, claim


def outbox(store, ident):
    return dict(store.repo.connection.execute('SELECT * FROM notification_outbox WHERE id = ?', (ident,)).fetchone())


def outcomes(store, ident):
    return [r[0] for r in store.repo.connection.execute(
        'SELECT outcome FROM notification_deliveries WHERE outbox_id = ? ORDER BY id_sprawy', (ident,))]


@pytest.mark.parametrize('first,second,wanted,enabled', [
    ('sent', 'sent', 'accepted', True), ('sent', 'delivered', 'delivered', True),
    ('sent', 'read', 'read', True), ('sent', 'failed', 'failed', False),
    ('delivered', 'sent', 'delivered', True), ('delivered', 'delivered', 'delivered', True),
    ('delivered', 'read', 'read', True), ('delivered', 'failed', 'delivered', True),
    ('read', 'sent', 'read', True), ('read', 'delivered', 'read', True),
    ('read', 'read', 'read', True), ('read', 'failed', 'read', True),
    ('failed', 'sent', 'failed', False), ('failed', 'delivered', 'delivered', False),
    ('failed', 'read', 'read', False), ('failed', 'failed', 'failed', False),
])
def test_status_order_is_monotonic_without_automatic_reenable(notification_store, first, second, wanted, enabled):
    store = notification_store
    ep, ident, claim = acknowledged(store)
    receiver = inbox(store)
    assert receiver.receive(*signed(status(first))) == 1
    assert receiver.reconcile() == 1
    # Older timestamp intentionally arrives later; order follows delivery evidence.
    assert receiver.receive(*signed(status(second, timestamp='1699999999'))) == 1
    assert receiver.reconcile() == 1
    assert outbox(store, ident)['state'] == wanted
    assert outcomes(store, ident) == ['accepted' if wanted == 'failed' else wanted]
    current = store.get_endpoint(MIETEK, ep.id)
    assert current.enabled == enabled and current.consent_at == ep.consent_at
    assert current.consent_revoked_at == ep.consent_revoked_at and current.verified_at == ep.verified_at
    assert all(row['processed_at'] and row['endpoint_id'] == ep.id for row in events(store))
    assert not store.complete(claim, DeliveryResult('accepted', provider_message_id='wamid.test1'))
    assert outbox(store, ident)['state'] == wanted


def test_failure_quarantines_only_matching_endpoint_and_keeps_reservations(notification_store):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    store.repo.upsert(lead('B/2'))
    sibling, = enqueue(store, ep, 'report:two', 'B/2')
    email = ready(store)
    email_job, = enqueue(store, email)
    user_before = BotStore(store.repo).get_user(MIETEK)
    receiver = inbox(store)
    receiver.receive(*signed(status('failed')))
    assert receiver.reconcile() == 1
    assert outbox(store, ident)['state'] == 'failed'
    assert outbox(store, sibling)['state'] == 'cancelled'
    assert outbox(store, email_job)['state'] == 'queued'
    assert outcomes(store, ident) == ['accepted'] and outcomes(store, email_job) == []
    assert not store.get_endpoint(MIETEK, ep.id).enabled
    assert store.get_endpoint(MIETEK, email.id) == email
    assert BotStore(store.repo).get_user(MIETEK) == user_before


def test_event_before_send_ack_survives_restart_and_then_reconciles(notification_store):
    store = notification_store
    ep = ready(store, 'whatsapp', '+' + PHONE)
    ident, = enqueue(store, ep)
    claim = store.claim(['whatsapp'])
    assert store.prepare_send(claim)
    receiver = inbox(store)
    receiver.receive(*signed(status('read')))
    assert receiver.reconcile() == 0
    assert events(store)[0]['processed_at'] is None and outbox(store, ident)['state'] == 'sending'
    with LeadRepository(store.repo._file, now=store.repo.now) as own_repo:
        own_store = NotificationStore(own_repo)
        assert own_store.complete(claim, DeliveryResult('accepted', provider_message_id='wamid.test1'))
        assert inbox(own_store).reconcile() == 1
    assert outbox(store, ident)['state'] == 'read' and outcomes(store, ident) == ['read']


def test_unknown_without_ack_id_cannot_be_assigned_by_recipient(notification_store):
    store = notification_store
    ep = ready(store, 'whatsapp', '+' + PHONE)
    ident, = enqueue(store, ep)
    assert store.complete(store.claim(['whatsapp']), DeliveryResult('unknown'))
    receiver = inbox(store)
    receiver.receive(*signed(status('read')))
    assert receiver.reconcile() == 0
    assert outbox(store, ident)['state'] == 'unknown' and outbox(store, ident)['provider_message_id'] is None
    assert not outcomes(store, ident) and events(store)[0]['processed_at'] is None
    assert not store.get_endpoint(MIETEK, ep.id).enabled


@pytest.mark.parametrize('mismatch', ['recipient', 'address_changed', 'owner', 'email', 'invalid_address', 'queued'])
def test_mismatched_status_stays_pending_without_any_delivery_mutation(notification_store, mismatch):
    store = notification_store
    if mismatch == 'email':
        ep, ident, _ = acknowledged(store, channel='email', address='test@example.test')
    else:
        ep, ident, _ = acknowledged(store)
    recipient = '48999888777' if mismatch == 'recipient' else PHONE
    if mismatch == 'address_changed':
        store.change_address(MIETEK, ep.id, '+48999888777')
    elif mismatch == 'owner':
        snapshot = json.loads(outbox(store, ident)['payload'])
        snapshot['chat_id'] = OBCY
        store.repo.connection.execute('UPDATE notification_outbox SET payload = ? WHERE id = ?', (json.dumps(snapshot), ident))
    elif mismatch == 'invalid_address':
        store.repo.connection.execute('UPDATE notification_endpoints SET address = ? WHERE id = ?', ('not a phone', ep.id))
    elif mismatch == 'queued':
        store.repo.connection.execute("UPDATE notification_outbox SET state = 'queued' WHERE id = ?", (ident,))
    before = outbox(store, ident)
    endpoint_before = store.get_endpoint(MIETEK, ep.id)
    receiver = inbox(store)
    receiver.receive(*signed(status('read', recipient=recipient)))
    assert receiver.reconcile() == 0
    assert outbox(store, ident) == before and outcomes(store, ident) == ['accepted']
    assert store.get_endpoint(MIETEK, ep.id) == endpoint_before
    assert events(store)[0]['processed_at'] is None and events(store)[0]['endpoint_id'] is None


@pytest.mark.parametrize('second_channel', ['whatsapp', 'email'])
def test_provider_id_ambiguity_is_not_resolved_by_channel_or_recipient(notification_store, second_channel):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    other, other_id, _ = acknowledged(store, channel=second_channel,
                                     address='+48999888777' if second_channel == 'whatsapp' else 'other@example.test')
    receiver = inbox(store)
    receiver.receive(*signed(status('failed')))
    assert receiver.reconcile() == 0
    assert outbox(store, ident)['state'] == outbox(store, other_id)['state'] == 'accepted'
    assert store.get_endpoint(MIETEK, ep.id).enabled and store.get_endpoint(MIETEK, other.id).enabled
    assert events(store)[0]['processed_at'] is None


@pytest.mark.parametrize('action', ['revoke', 'pause'])
def test_status_bookkeeping_after_revoke_or_pause_does_not_reenable(notification_store, action):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    if action == 'revoke':
        store.revoke_consent(MIETEK, ep.id)
    else:
        store.set_enabled(MIETEK, ep.id, False, expected_version=ep.version)
    before = store.get_endpoint(MIETEK, ep.id)
    receiver = inbox(store)
    receiver.receive(*signed(status('read')))
    assert receiver.reconcile() == 1
    assert outbox(store, ident)['state'] == 'read' and outcomes(store, ident) == ['read']
    assert store.get_endpoint(MIETEK, ep.id) == before


@pytest.mark.parametrize('corruption', ['json', 'duplicate_keys', 'large', 'version', 'outcome', 'timestamp',
                                       'digest', 'extra', 'provider_id', 'event_key'])
def test_malformed_persisted_events_are_consumed_without_effects(notification_store, corruption, caplog):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    receiver = inbox(store)
    receiver.receive(*signed())
    event = events(store)[0]
    if corruption in ('provider_id', 'event_key'):
        column = corruption if corruption == 'event_key' else 'provider_message_id'
        # Closed test-controlled column list, never external SQL text.
        store.repo.connection.execute(f'UPDATE notification_webhook_events SET {column} = ?', ('PRIVATE_INVALID',))
    else:
        body = json.loads(event['payload'])
        modifications = {'version': {'version': True}, 'outcome': {'outcome': 'queued'},
                         'timestamp': {'timestamp': 'PRIVATE_INVALID'}, 'digest': {'recipient_digest': 'bad'},
                         'extra': {'PRIVATE_INVALID': 'PRIVATE_VALUE'}}
        body.update(modifications.get(corruption, {}))
        raw = {'json': 'PRIVATE_INVALID', 'duplicate_keys': '{"version":1,"version":1}',
               'large': 'x' * 1025}.get(corruption, json.dumps(body))
        store.repo.connection.execute('UPDATE notification_webhook_events SET payload = ?', (raw,))
    assert receiver.reconcile() == 1
    assert outbox(store, ident)['state'] == 'accepted' and outcomes(store, ident) == ['accepted']
    assert store.get_endpoint(MIETEK, ep.id) == ep and events(store)[0]['processed_at']
    assert 'PRIVATE_' not in caplog.text and PHONE not in caplog.text


def test_malformed_outbox_snapshot_is_not_used_as_delivery_proof(notification_store):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    store.repo.connection.execute('UPDATE notification_outbox SET payload = ? WHERE id = ?', ('{}', ident))
    receiver = inbox(store)
    receiver.receive(*signed(status('read')))
    assert receiver.reconcile() == 0
    assert outbox(store, ident)['state'] == 'accepted' and outcomes(store, ident) == ['accepted']
    assert store.get_endpoint(MIETEK, ep.id) == ep and events(store)[0]['processed_at'] is None


def test_deleted_delivery_history_is_never_recreated(notification_store):
    store = notification_store
    _, ident, _ = acknowledged(store)
    store.repo.connection.execute('DELETE FROM notification_deliveries WHERE outbox_id = ?', (ident,))
    receiver = inbox(store)
    receiver.receive(*signed(status('read')))
    assert receiver.reconcile() == 1 and outbox(store, ident)['state'] == 'read'
    assert not outcomes(store, ident)


def test_history_update_is_limited_to_original_outbox_and_lead_revision(notification_store):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    other, other_id, _ = acknowledged(store, address='+48999888777', ident='wamid.other')
    store.repo.upsert(lead('B/2'))
    store.repo.connection.execute(
        "INSERT INTO notification_deliveries(endpoint_id,id_sprawy,revision,kind,outcome,processed_at,outbox_id)"
        " VALUES (?, 'B/2', ?, 'report', 'accepted', ?, ?)",
        (ep.id, store.repo.get('B/2').status_zmieniony, store.repo.now().isoformat(), ident))
    receiver = inbox(store)
    receiver.receive(*signed(status('read')))
    assert receiver.reconcile() == 1
    assert outcomes(store, ident) == ['read', 'accepted'] and outcomes(store, other_id) == ['accepted']
    assert store.get_endpoint(MIETEK, other.id) == other


def test_limited_reconciliation_advances_past_orphan_rows_and_wraps(notification_store):
    store = notification_store
    _, ident, _ = acknowledged(store)
    receiver = inbox(store)
    receiver.receive(*signed(*(status(ident=f'wamid.orphan{i}') for i in range(12)), status('read')))
    assert receiver.reconcile(limit=5) == 0
    assert receiver.reconcile(limit=5) == 0
    assert receiver.reconcile(limit=5) == 1
    assert outbox(store, ident)['state'] == 'read'
    assert len([row for row in events(store) if row['processed_at'] is None]) == 12
    # Give an old orphan its missing provider ACK; wrap eventually revisits it.
    store.repo.connection.execute('UPDATE notification_outbox SET provider_message_id = ? WHERE id = ?', ('wamid.orphan0', ident))
    assert receiver.reconcile(limit=5) == 1


@pytest.mark.parametrize('limit', [True, 0, -1, 101, 1.0, '1', None])
def test_reconcile_budget_must_be_bounded_integer(notification_store, limit):
    with pytest.raises(ValueError):
        inbox(notification_store).reconcile(limit=limit)


def test_reconcile_refuses_outer_transaction_before_any_writes(notification_store):
    store = notification_store
    _, ident, _ = acknowledged(store)
    receiver = inbox(store)
    receiver.receive(*signed(status('read')))
    with store.repo.transaction():
        with pytest.raises(RuntimeError, match='transakcj'):
            receiver.reconcile()
        assert outbox(store, ident)['state'] == 'accepted' and events(store)[0]['processed_at'] is None


@pytest.mark.parametrize('outcome', ['delivered', 'failed'])
def test_reconcile_failure_rolls_back_state_history_quarantine_and_cursor(notification_store, outcome):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    receiver = inbox(store)
    receiver.receive(*signed(status(ident='wamid.orphan'), status(outcome)))
    assert receiver.reconcile(limit=1) == 0
    store.repo.connection.execute(
        "CREATE TRIGGER fail_processing BEFORE UPDATE OF processed_at ON notification_webhook_events"
        " WHEN NEW.processed_at IS NOT NULL BEGIN SELECT RAISE(ABORT, 'synthetic processing failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic processing failure'):
        receiver.reconcile(limit=1)
    assert outbox(store, ident)['state'] == 'accepted' and outcomes(store, ident) == ['accepted']
    assert store.get_endpoint(MIETEK, ep.id) == ep
    assert all(row['processed_at'] is None for row in events(store))
    store.repo.connection.execute('DROP TRIGGER fail_processing')
    assert receiver.reconcile(limit=1) == 1
    assert outbox(store, ident)['state'] == outcome


def test_concurrent_reconcilers_process_event_once(notification_store):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    inbox(store).receive(*signed(status('failed')))
    gate = threading.Barrier(4)
    def reconcile(_):
        with LeadRepository(store.repo._file, now=store.repo.now) as own_repo:
            receiver = inbox(NotificationStore(own_repo))
            gate.wait(timeout=10)
            return receiver.reconcile()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reconcile, range(4)))
    assert sorted(results) == [0, 0, 0, 1]
    assert outbox(store, ident)['state'] == 'failed'
    assert store.get_endpoint(MIETEK, ep.id).version == ep.version + 1


def test_api_ack_and_webhook_race_on_separate_connections_converges(notification_store):
    store = notification_store
    ep = ready(store, 'whatsapp', '+' + PHONE)
    ident, = enqueue(store, ep)
    claim = store.claim(['whatsapp'])
    gate = threading.Barrier(2)
    def run(is_ack):
        with LeadRepository(store.repo._file, now=store.repo.now) as own_repo:
            own_store = NotificationStore(own_repo)
            receiver = inbox(own_store)
            gate.wait(timeout=10)
            if is_ack:
                assert own_store.complete(claim, DeliveryResult('accepted', provider_message_id='wamid.test1'))
            else:
                receiver.receive(*signed(status('read')))
                receiver.reconcile()
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, [True, False]))
    inbox(store).reconcile()
    assert outbox(store, ident)['state'] == 'read' and outcomes(store, ident) == ['read']
    assert events(store)[0]['processed_at']


@pytest.mark.parametrize('advance_cursor', [False, True])
def test_pending_batch_uses_ordered_pending_index_without_sort(notification_store, advance_cursor):
    store = notification_store
    receiver = inbox(store)
    receiver.receive(*signed(status(ident='wamid.orphan1'), status(ident='wamid.orphan2')))
    if advance_cursor:
        receiver.reconcile(limit=1)
    statements = []
    store.repo.connection.set_trace_callback(statements.append)
    try:
        receiver.reconcile(limit=1)
    finally:
        store.repo.connection.set_trace_callback(None)
    query, = [sql for sql in statements if sql.startswith('SELECT * FROM notification_webhook_events')]
    plan = '\n'.join(row[3] for row in store.repo.connection.execute('EXPLAIN QUERY PLAN ' + query))
    assert 'ix_notification_webhook_pending' in plan and 'USE TEMP B-TREE' not in plan


@pytest.mark.parametrize('operation', ['receive', 'reconcile'])
def test_commit_failure_rolls_back_owned_transaction_and_allows_safe_retry(notification_store, operation):
    store = notification_store
    receiver = inbox(store)
    if operation == 'reconcile':
        _, ident, _ = acknowledged(store)
        receiver.receive(*signed(status(ident='wamid.orphan'), status('read')))
        assert receiver.reconcile(limit=1) == 0
    # Deferred FK fails at COMMIT rather than inside a statement. Model a real
    # commit failure without filesystem damage or altering the repository helper.
    store.repo.connection.execute(
        'CREATE TABLE commit_guard(endpoint_id INTEGER REFERENCES notification_endpoints(id)'
        ' DEFERRABLE INITIALLY DEFERRED)')
    trigger_event = 'INSERT' if operation == 'receive' else 'UPDATE OF processed_at'
    store.repo.connection.execute(
        f'CREATE TRIGGER fail_commit AFTER {trigger_event} ON notification_webhook_events'
        ' BEGIN INSERT INTO commit_guard(endpoint_id) VALUES (999999); END')
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        if operation == 'receive':
            receiver.receive(*signed())
        else:
            receiver.reconcile(limit=1)
    assert not store.repo.connection.in_transaction
    assert not store.repo.connection.execute('SELECT * FROM commit_guard').fetchall()
    if operation == 'receive':
        assert not events(store)
    else:
        assert outbox(store, ident)['state'] == 'accepted' and outcomes(store, ident) == ['accepted']
        assert all(event['processed_at'] is None for event in events(store))
    store.repo.connection.execute('DROP TRIGGER fail_commit')
    if operation == 'receive':
        assert receiver.receive(*signed()) == 1
    else:
        assert receiver.reconcile(limit=1) == 1 and outbox(store, ident)['state'] == 'read'


def test_authenticated_unsupported_status_is_successful_empty_batch(notification_store):
    store = notification_store
    assert inbox(store).receive(*signed(status('future_status'))) == 0
    assert not events(store) and not store.repo.connection.in_transaction


@pytest.mark.parametrize('timestamp', ['9999-12-31T23:59:59.000000-01:00', '0001-01-01T00:00:00.000000+01:00'])
def test_corrupt_event_timezone_overflow_cannot_block_valid_status_behind_it(notification_store, timestamp):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    receiver = inbox(store)
    receiver.receive(*signed(status('sent'), status('read')))
    first = events(store)[0]
    malformed = json.loads(first['payload'])
    malformed['timestamp'] = timestamp
    store.repo.connection.execute('UPDATE notification_webhook_events SET payload = ? WHERE id = ?',
                                  (json.dumps(malformed), first['id']))
    assert receiver.reconcile() == 2
    assert outbox(store, ident)['state'] == 'read' and outcomes(store, ident) == ['read']
    assert store.get_endpoint(MIETEK, ep.id) == ep
    assert events(store)[0]['endpoint_id'] is None and all(event['processed_at'] for event in events(store))
    receiver.receive(*signed(status('failed', timestamp='1700000001')))
    assert receiver.reconcile() == 1 and outbox(store, ident)['state'] == 'read'


@pytest.mark.parametrize('revision', ['9999-12-31T23:59:59.000000-01:00', '0001-01-01T00:00:00.000000+01:00'])
def test_corrupt_snapshot_timezone_overflow_stays_pending_without_blocking_next_job(notification_store, revision):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    other, other_id, _ = acknowledged(store, address='+48999888777', ident='wamid.other')
    malformed = json.loads(outbox(store, ident)['payload'])
    malformed['leads'][0]['revision'] = revision
    store.repo.connection.execute('UPDATE notification_outbox SET payload = ? WHERE id = ?', (json.dumps(malformed), ident))
    receiver = inbox(store)
    receiver.receive(*signed(status('read'), status('read', ident='wamid.other', recipient='48999888777')))
    assert receiver.reconcile(limit=1) == 0
    assert receiver.reconcile(limit=1) == 1
    assert outbox(store, ident)['state'] == 'accepted' and outcomes(store, ident) == ['accepted']
    assert events(store)[0]['processed_at'] is None
    assert store.get_endpoint(MIETEK, ep.id) == ep
    assert outbox(store, other_id)['state'] == 'read' and outcomes(store, other_id) == ['read']
    assert store.get_endpoint(MIETEK, other.id) == other
    receiver.receive(*signed(status('delivered', ident='wamid.other', recipient='48999888777', timestamp='1700000001')))
    assert receiver.reconcile(limit=1) == 1 and outbox(store, other_id)['state'] == 'read'
