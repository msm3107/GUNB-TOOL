"""Signed synthetic bodies and real temporary SQLite, never network/production."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading

import pytest

from gunb_tool import whatsapp_inbox
from gunb_tool.bot_store import BotStore
from gunb_tool.notification_store import NotificationStore
from gunb_tool.storage import LeadRepository
from gunb_tool.whatsapp_webhook import WebhookError
from tests.bot_helpers import MIETEK
from tests.test_notification_store import notification_store
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
