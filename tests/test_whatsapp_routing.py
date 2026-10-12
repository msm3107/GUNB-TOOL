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
from tests.bot_helpers import MIETEK
from tests.test_migration import db_of, dump, make_state
from tests.test_notification_foundation import endpoint, isolated_env, queued, version

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
