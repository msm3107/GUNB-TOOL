"""Synthetic routing proof, migration and dispatch regressions; no live Meta calls."""

from dataclasses import FrozenInstanceError
import hashlib
import json
import sqlite3

import pytest

from gunb_tool import storage
from gunb_tool.migration import export_state, import_state, restore_backup
from gunb_tool.storage import LeadRepository, SchemaTooNew
from gunb_tool.whatsapp_routing import WhatsAppRouting
from gunb_tool.notification_models import DeliveryResult
from gunb_tool.notification_worker import NotificationWorker
from gunb_tool.whatsapp_inbox import WhatsAppInbox
from tests.bot_helpers import MIETEK, lead
from tests.test_migration import db_of, dump, make_state
from tests.test_notification_foundation import endpoint, isolated_env, queued, version
from tests.test_notification_store import enqueue, notification_store, ready
from tests.test_notification_worker import FakeSender
from tests.test_whatsapp_inbox import acknowledged, events, inbox, outbox, outcomes, signed, status
from tests.test_whatsapp_webhook import settings as webhook_settings

PHONE = '48111222333'


def expected_hash(items):
    return hashlib.sha256(json.dumps(items, separators=(',', ':')).encode('ascii')).hexdigest()


def test_route_is_frozen_private_and_domain_separated():
    route = WhatsAppRouting('1234', '5678')
    assert route.scope == expected_hash(['gunb-whatsapp-route-v1', '1234', '5678'])
    assert route.recipient_digest(PHONE) == expected_hash(['gunb-whatsapp-recipient-v1', route.scope, PHONE])
    assert route.scope != WhatsAppRouting('12345', '678').scope
    assert route.recipient_digest(PHONE) != WhatsAppRouting('1234', '5679').recipient_digest(PHONE)
    assert route.recipient_digest(PHONE) != route.recipient_digest('48111222334')
    assert repr(route) == 'WhatsAppRouting()'
    with pytest.raises(FrozenInstanceError):
        route.waba_id = '999'


@pytest.mark.parametrize('value', [None, True, 123, '', '0', '01', '+123', '１２３', '1' * 33, '1\n'])
@pytest.mark.parametrize('field', ['waba_id', 'phone_number_id'])
def test_route_rejects_invalid_account_ids_without_echo(value, field):
    args = {'waba_id': '1234', 'phone_number_id': '5678', field: value}
    with pytest.raises(ValueError, match='Niepoprawny'):
        WhatsAppRouting(**args)


@pytest.mark.parametrize('value', [None, True, 123, '', '+48111222333', '1234567', '1' * 16, '012345678'])
def test_recipient_validation(value):
    with pytest.raises(ValueError):
        WhatsAppRouting('1234', '5678').recipient_digest(value)


@pytest.fixture
def v14_state(tmp_path, clock, monkeypatch):
    with monkeypatch.context() as old:
        old.setattr(storage, '_MIGRATIONS', storage._MIGRATIONS[:14])
        old.setattr(storage, 'SCHEMA_VERSION', 14)
        config = make_state(tmp_path / 'source', clock)
        with LeadRepository(db_of(config), now=clock.now_utc) as repo:
            ident = endpoint(repo.connection, 'whatsapp', '+' + PHONE)
            queued(repo.connection, ident)
    assert version(db_of(config)) == 14
    return config


def test_v15_preserves_populated_v14_without_inventing_routes(v14_state, clock):
    path = db_of(v14_state)
    before = dump(path)
    assert all(before[t] for t in ('bot_users', 'wysylki', 'notification_outbox'))
    with LeadRepository(path, now=clock.now_utc) as repo:
        assert version(path) == 15
        assert not repo.connection.execute('PRAGMA foreign_key_check').fetchall()
    after = dump(path)
    assert {key: after[key] for key in before} == before
    assert set(after) - set(before) == {'whatsapp_dispatch_routes'}
    assert after['whatsapp_dispatch_routes'] == []
    backup, = (path.parent / 'backups').glob('*-przed-v15-*.sqlite')
    assert version(backup) == 14 and dump(backup) == before
    with LeadRepository(path):
        pass
    assert dump(path) == after
    assert list((path.parent / 'backups').glob('*-przed-v15-*.sqlite')) == [backup]


def test_v15_failed_ddl_rolls_back(v14_state, monkeypatch):
    path = db_of(v14_state)
    before = dump(path)
    monkeypatch.setattr(storage, '_MIGRATIONS', storage._MIGRATIONS[:-1] +
                        (storage._MIGRATIONS[-1] + '\nSELECT broken_v15;',))
    with pytest.raises(sqlite3.OperationalError, match='broken_v15'):
        LeadRepository(path)
    assert version(path) == 14 and dump(path) == before


def test_v15_backup_failure_prevents_any_migration(v14_state, tmp_path):
    path = db_of(v14_state)
    before = dump(path)
    blocked = tmp_path / 'blocked'
    blocked.write_text('fixture', encoding='utf-8')
    with pytest.raises(OSError):
        LeadRepository(path, backup_dir=blocked)
    assert version(path) == 14 and dump(path) == before


def test_v14_code_refuses_v15_and_can_open_restored_backup(v14_state, clock, monkeypatch):
    path = db_of(v14_state)
    before = dump(path)
    with LeadRepository(path):
        pass
    upgraded = dump(path)
    monkeypatch.setattr(storage, '_MIGRATIONS', storage._MIGRATIONS[:14])
    monkeypatch.setattr(storage, 'SCHEMA_VERSION', 14)
    with pytest.raises(SchemaTooNew, match='v15'):
        LeadRepository(path)
    assert dump(path) == upgraded and version(path) == 15
    backup, = (path.parent / 'backups').glob('*-przed-v15-*.sqlite')
    assert restore_backup(backup, v14_state, now=clock.now_utc) is not None
    assert version(path) == 14 and dump(path) == before
    with LeadRepository(path):
        pass


def insert_route(conn, job_id, **changes):
    values = dict(outbox_id=job_id, sender_scope='a' * 64, recipient_digest='b' * 64,
                  chat_id=MIETEK, endpoint_version=4, created_at='2026-10-12T00:00:00+00:00')
    values.update(changes)
    return conn.execute('INSERT INTO whatsapp_dispatch_routes VALUES (?, ?, ?, ?, ?, ?)', tuple(values.values()))


@pytest.mark.parametrize('changes', [dict(sender_scope='A' * 64), dict(sender_scope='z' * 64),
    dict(recipient_digest='b' * 63), dict(recipient_digest='x' * 64), dict(chat_id=0),
    dict(chat_id=1.5), dict(endpoint_version=0), dict(endpoint_version=1.5), dict(outbox_id=999)])
def test_route_constraints(v14_state, changes):
    with LeadRepository(db_of(v14_state)) as repo:
        job = repo.connection.execute('SELECT id FROM notification_outbox').fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            insert_route(repo.connection, job, **changes)


@pytest.mark.parametrize('remove', ['outbox', 'endpoint', 'owner'])
def test_route_cascades(v14_state, remove):
    with LeadRepository(db_of(v14_state)) as repo:
        conn = repo.connection
        job, ident = conn.execute('SELECT id, endpoint_id FROM notification_outbox').fetchone()
        insert_route(conn, job)
        with pytest.raises(sqlite3.IntegrityError):
            insert_route(conn, job)
        table, column, value = {'outbox': ('notification_outbox', 'id', job),
                               'endpoint': ('notification_endpoints', 'id', ident),
                               'owner': ('bot_users', 'chat_id', MIETEK)}[remove]
        conn.execute(f'DELETE FROM {table} WHERE {column} = ?', (value,))
        assert conn.execute('SELECT COUNT(*) FROM whatsapp_dispatch_routes').fetchone()[0] == 0
        assert not conn.execute('PRAGMA foreign_key_check').fetchall()


def test_route_survives_real_export_import(v14_state, tmp_path, clock):
    with LeadRepository(db_of(v14_state)) as repo:
        job = repo.connection.execute('SELECT id FROM notification_outbox').fetchone()[0]
        insert_route(repo.connection, job)
    before = dump(db_of(v14_state))
    package = export_state(v14_state, tmp_path / 'packages', now=clock.now_utc)
    target = tmp_path / 'target' / 'config.yaml'
    result = import_state(package, target, now=clock.now_utc)
    assert result.schema_version == 15 and dump(result.database) == before
    assert result.counts['whatsapp_dispatch_routes'] == 1


def saved_route(store, ident):
    row = store.repo.connection.execute('SELECT * FROM whatsapp_dispatch_routes WHERE outbox_id=?', (ident,)).fetchone()
    return dict(row) if row else None


@pytest.mark.parametrize('routing', [None, object(), 'invalid'])
def test_direct_preflight_without_valid_routing_fails_closed(notification_store, routing):
    store = notification_store
    ep = ready(store, 'whatsapp', '+' + PHONE)
    ident, = enqueue(store, ep)
    claim = store.claim(['whatsapp'])
    assert store.prepare_send(claim, whatsapp_routing=routing) is None
    assert not saved_route(store, ident)
    assert outbox(store, ident)['state'] == 'failed'
    assert not store.get_endpoint(MIETEK, ep.id).enabled
    assert not store.repo.connection.in_transaction


def test_worker_requires_explicit_valid_whatsapp_routing_before_claim(notification_store):
    class Unscoped:
        def send(self, *args, **kwargs):
            raise AssertionError('No HTTP expected')
    store = notification_store
    ep = ready(store, 'whatsapp', '+' + PHONE)
    ident, = enqueue(store, ep)
    with pytest.raises(ValueError, match='routing'):
        NotificationWorker(store, {'whatsapp': Unscoped()})
    assert outbox(store, ident)['state'] == 'queued'


@pytest.mark.parametrize('rotation', [False, True])
def test_retry_preserves_route_and_rejects_sender_rotation(notification_store, clock, rotation):
    store = notification_store
    ep = ready(store, 'whatsapp', '+' + PHONE)
    ident, = enqueue(store, ep)
    first = FakeSender(DeliveryResult('retry'))
    NotificationWorker(store, {'whatsapp': first}).run_once()
    proof = saved_route(store, ident)
    assert proof is not None
    clock.advance(seconds=80)
    second = FakeSender()
    if rotation:
        second.routing = WhatsAppRouting('1234', '9999')
    NotificationWorker(store, {'whatsapp': second}).run_once()
    assert saved_route(store, ident) == proof
    assert len(second.calls) == (0 if rotation else 1)
    assert outbox(store, ident)['state'] == ('failed' if rotation else 'accepted')


@pytest.mark.parametrize('field,value', [('sender_scope', 'c' * 64), ('recipient_digest', 'c' * 64),
                                      ('chat_id', 999), ('endpoint_version', 99)])
def test_conflicting_saved_proof_never_overwrites_or_sends(notification_store, field, value):
    store = notification_store
    ep = ready(store, 'whatsapp', '+' + PHONE)
    ident, = enqueue(store, ep)
    claim = store.claim(['whatsapp'])
    assert store.prepare_send(claim, whatsapp_routing=FakeSender.routing)
    store.repo.connection.execute(f'UPDATE whatsapp_dispatch_routes SET {field}=? WHERE outbox_id=?', (value, ident))
    before = saved_route(store, ident)
    assert store.prepare_send(claim, whatsapp_routing=FakeSender.routing) is None
    assert saved_route(store, ident) == before
    assert outbox(store, ident)['state'] == 'failed'
    assert not store.get_endpoint(MIETEK, ep.id).enabled


def test_route_commit_failure_prevents_sender_and_rolls_back_owned_transaction(notification_store):
    store = notification_store
    ep = ready(store, 'whatsapp', '+' + PHONE)
    ident, = enqueue(store, ep)
    conn = store.repo.connection
    conn.execute('CREATE TABLE route_guard(endpoint_id INTEGER REFERENCES notification_endpoints(id) DEFERRABLE INITIALLY DEFERRED)')
    conn.execute('CREATE TRIGGER fail_route_commit AFTER INSERT ON whatsapp_dispatch_routes'
                 ' BEGIN INSERT INTO route_guard VALUES(999999); END')
    sender = FakeSender()
    with pytest.raises(sqlite3.IntegrityError, match='FOREIGN KEY'):
        NotificationWorker(store, {'whatsapp': sender}).run_once()
    assert sender.calls == [] and not conn.in_transaction
    assert saved_route(store, ident) is None and conn.execute('SELECT * FROM route_guard').fetchall() == []
    claim_row = outbox(store, ident)
    assert claim_row['state'] == 'sending' and claim_row['provider_message_id'] is None


@pytest.mark.parametrize('outcome', ['read', 'failed'])
def test_original_recipient_status_cannot_quarantine_reconfigured_endpoint(notification_store, outcome):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    old_route = saved_route(store, ident)
    current = store.change_address(MIETEK, ep.id, '+48999888777')
    current = store.record_consent(MIETEK, ep.id, expected_version=current.version, source='new_form')
    current = store.record_verification(MIETEK, ep.id, expected_version=current.version)
    current = store.set_enabled(MIETEK, ep.id, True, expected_version=current.version)
    store.repo.upsert(lead('B/2'))
    fresh, = enqueue(store, current, 'fresh', 'B/2')
    receiver = inbox(store)
    receiver.receive(*signed(status(outcome)))
    assert receiver.reconcile() == 1
    assert outbox(store, ident)['state'] == outcome
    assert outcomes(store, ident) == (['read'] if outcome == 'read' else ['accepted'])
    assert outbox(store, fresh)['state'] == 'queued'
    assert store.get_endpoint(MIETEK, ep.id) == current
    assert saved_route(store, ident) == old_route


@pytest.mark.parametrize('mismatch', ['sender_scope', 'recipient_digest', 'chat_id', 'endpoint_version', 'missing_route', 'legacy_v1'])
def test_unprovable_status_remains_pending_without_mutation(notification_store, mismatch):
    store = notification_store
    ep, ident, _ = acknowledged(store)
    receiver = inbox(store)
    receiver.receive(*signed(status('read')))
    conn = store.repo.connection
    if mismatch == 'missing_route':
        conn.execute('DELETE FROM whatsapp_dispatch_routes WHERE outbox_id=?', (ident,))
    elif mismatch == 'legacy_v1':
        row = events(store)[0]
        data = json.loads(row['payload'])
        data.pop('sender_scope')
        data['version'] = 1
        data['recipient_digest'] = hashlib.sha256(('wamid.test1\0' + PHONE).encode()).hexdigest()
        conn.execute('UPDATE notification_webhook_events SET payload=? WHERE id=?', (json.dumps(data), row['id']))
        original = events(store)[0]['payload']
        assert receiver.receive(*signed(status('read'))) == 0
        assert events(store)[0]['payload'] == original
    else:
        value = 'c' * 64 if 'scope' in mismatch or 'digest' in mismatch else 999
        conn.execute(f'UPDATE whatsapp_dispatch_routes SET {mismatch}=? WHERE outbox_id=?', (value, ident))
    before = outbox(store, ident)
    assert receiver.reconcile() == 0
    assert outbox(store, ident) == before and outcomes(store, ident) == ['accepted']
    assert store.get_endpoint(MIETEK, ep.id) == ep and events(store)[0]['processed_at'] is None


def test_persisted_event_uses_original_scope_after_inbox_config_rotation(notification_store):
    store = notification_store
    _, ident, _ = acknowledged(store)
    inbox(store).receive(*signed(status('read')))
    rotated = WhatsAppInbox(store, webhook_settings(waba_id='999', phone_number_id='888'))
    assert rotated.reconcile() == 1 and outbox(store, ident)['state'] == 'read'


@pytest.mark.parametrize('changes', [dict(waba_id='999'), dict(phone_number_id='888')])
def test_same_provider_and_recipient_from_other_sender_cannot_match(notification_store, changes):
    from tests.test_whatsapp_webhook import payload, signature, value
    store = notification_store
    ep, ident, _ = acknowledged(store)
    settings = webhook_settings(**changes)
    body = payload()
    body['entry'][0]['id'] = settings.waba_id
    value(body)['metadata']['phone_number_id'] = settings.phone_number_id
    value(body)['statuses'] = [status('read')]
    raw = json.dumps(body).encode()
    receiver = WhatsAppInbox(store, settings)
    assert receiver.receive(raw, signature(raw)) == 1
    assert receiver.reconcile() == 0
    assert outbox(store, ident)['state'] == 'accepted'
    assert store.get_endpoint(MIETEK, ep.id) == ep
