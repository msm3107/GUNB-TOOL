"""Durable verification with fake sender and private SQLite files only."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import hashlib
import sqlite3
import threading

import pytest

from gunb_tool import storage
from gunb_tool.bot_store import BotStore
from gunb_tool.email_verification import EmailVerification
from gunb_tool.migration import restore_backup
from gunb_tool.notification_models import DeliveryResult, utc_iso
from gunb_tool.notification_store import NotificationStore
from gunb_tool.storage import LeadRepository, SchemaTooNew
from tests.bot_helpers import MIETEK, OBCY, lead
from tests.test_migration import dump
from tests.test_notification_store import enqueue, history, notification_store, ready


class CaptureSender:
    def __init__(self, result=None, callback=None):
        self.calls = []
        self.result = result or DeliveryResult('accepted')
        self.callback = callback

    def send_verification(self, endpoint, token, **kwargs):
        self.calls.append((endpoint, token, kwargs))
        if self.callback:
            self.callback()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def address(store, value='odbiorca@example.test', owner=MIETEK):
    return store.add_endpoint(owner, 'email', value)


def rows(store):
    return store.repo.connection.execute('SELECT * FROM email_verifications ORDER BY id').fetchall()


def allow(store, owner):
    bot = BotStore(store.repo)
    bot.register(owner, 'Fikcyjny odbiorca', None, status='aktywny', backlog_days=7)
    bot.set_access(owner, (store.repo.now() + timedelta(days=2)).isoformat())


def test_token_is_hashed_single_use_and_does_not_enable_or_consent(notification_store):
    store = notification_store
    endpoint = address(store)
    sender = CaptureSender(callback=lambda: assert_no_transaction(store))
    service = EmailVerification(store, sender)
    assert service.request(MIETEK, endpoint.id).outcome == 'accepted'
    _, token, options = sender.calls[0]
    row, = rows(store)
    assert len(token) == 43 and row['token_digest'] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in '\n'.join(store.repo.connection.iterdump())
    assert options['expires_at'] == store.repo.now() + timedelta(minutes=15)
    assert service.consume(MIETEK, endpoint.id, token)
    changed = store.get_endpoint(MIETEK, endpoint.id)
    assert changed.verified_at == utc_iso(store.repo.now())
    assert not changed.enabled and changed.consent_at is None and changed.version == endpoint.version
    assert rows(store)[0]['consumed_at'] is not None
    assert not service.consume(MIETEK, endpoint.id, token)
    assert store.repo.connection.execute('SELECT COUNT(*) FROM notification_deliveries').fetchone()[0] == 0


def assert_no_transaction(store):
    assert not store.repo.connection.in_transaction


@pytest.mark.parametrize('seconds,valid', [(899, True), (900, False), (901, False)])
def test_expiry_boundary(notification_store, clock, seconds, valid):
    store = notification_store
    endpoint, sender = address(store), CaptureSender()
    service = EmailVerification(store, sender)
    service.request(MIETEK, endpoint.id)
    clock.advance(seconds=seconds)
    assert service.consume(MIETEK, endpoint.id, sender.calls[0][1]) is valid


def test_wrong_tokens_have_five_attempts_and_do_not_leak(notification_store, caplog):
    store = notification_store
    endpoint, sender = address(store), CaptureSender()
    service = EmailVerification(store, sender)
    service.request(MIETEK, endpoint.id)
    for _ in range(5):
        assert not service.consume(MIETEK, endpoint.id, 'z' * 43)
    assert rows(store)[0]['attempts'] == 5
    assert not service.consume(MIETEK, endpoint.id, sender.calls[0][1])
    assert sender.calls[0][1] not in caplog.text
    for token in (None, 123, '', 'x\n' * 22, 'x' * 10000):
        assert not service.consume(MIETEK, endpoint.id, token)


@pytest.mark.parametrize('change', ['address', 'consent', 'delete', 'pause', 'access', 'blocked'])
def test_changed_endpoint_or_access_cannot_consume_old_token(notification_store, change):
    store = notification_store
    endpoint, sender = address(store), CaptureSender()
    service = EmailVerification(store, sender)
    service.request(MIETEK, endpoint.id)
    if change == 'address':
        store.change_address(MIETEK, endpoint.id, 'nowy@example.test')
    elif change == 'consent':
        store.record_consent(MIETEK, endpoint.id, expected_version=endpoint.version, source='new_form')
    elif change == 'delete':
        store.delete_endpoint(MIETEK, endpoint.id)
    elif change == 'pause':
        BotStore(store.repo).set_paused(MIETEK, True)
    elif change == 'access':
        BotStore(store.repo).set_access(MIETEK, store.repo.now().isoformat())
    else:
        store.repo.connection.execute("UPDATE bot_users SET status = 'zablokowany' WHERE chat_id = ?", (MIETEK,))
    assert not service.consume(MIETEK, endpoint.id, sender.calls[0][1])


def test_ownership_channel_activation_and_inactive_account_denied(notification_store):
    store = notification_store
    endpoint, sender = address(store), CaptureSender()
    service = EmailVerification(store, sender)
    assert service.request(OBCY, endpoint.id) is None
    assert not service.consume(OBCY, endpoint.id, 'x' * 43)
    whatsapp = store.add_endpoint(MIETEK, 'whatsapp', '+48123456789')
    assert service.request(MIETEK, whatsapp.id) is None
    active = ready(store, address='inny@example.test')
    assert service.request(MIETEK, active.id) is None
    BotStore(store.repo).set_paused(MIETEK, True)
    assert service.request(MIETEK, endpoint.id) is None
    assert not sender.calls and not rows(store)


def test_reissue_invalidates_previous_token(notification_store, clock):
    store = notification_store
    endpoint, sender = address(store), CaptureSender()
    service = EmailVerification(store, sender)
    service.request(MIETEK, endpoint.id)
    clock.advance(seconds=61)
    service.request(MIETEK, endpoint.id)
    assert sender.calls[0][1] != sender.calls[1][1]
    assert not service.consume(MIETEK, endpoint.id, sender.calls[0][1])
    assert service.consume(MIETEK, endpoint.id, sender.calls[1][1])


def test_new_token_after_restoring_counter_has_a_fresh_message_key(notification_store):
    store, sender = notification_store, CaptureSender()
    endpoint = address(store)
    service = EmailVerification(store, sender)
    service.request(MIETEK, endpoint.id)
    # A backup predating the first request also predates the AUTOINCREMENT value.
    store.repo.connection.execute('DELETE FROM email_verifications')
    store.repo.connection.execute("DELETE FROM sqlite_sequence WHERE name = 'email_verifications'")
    service.request(MIETEK, endpoint.id)
    assert sender.calls[0][2]['idempotency_key'] != sender.calls[1][2]['idempotency_key']
    assert not service.consume(MIETEK, endpoint.id, sender.calls[0][1])
    assert service.consume(MIETEK, endpoint.id, sender.calls[1][1])


def test_owner_rate_limit_spans_addresses(notification_store, clock):
    store, sender = notification_store, CaptureSender()
    service = EmailVerification(store, sender)
    endpoints = [address(store, f'odbiorca{i}@example.test') for i in range(4)]
    assert service.request(MIETEK, endpoints[0].id)
    assert service.request(MIETEK, endpoints[1].id) is None
    for i in (1, 2):
        clock.advance(seconds=61)
        assert service.request(MIETEK, endpoints[i].id)
    clock.advance(seconds=61)
    assert service.request(MIETEK, endpoints[3].id) is None
    clock.advance(hours=1)
    assert service.request(MIETEK, endpoints[3].id)


def test_address_limit_spans_owners_and_case_variants(notification_store, clock):
    store, sender = notification_store, CaptureSender()
    service = EmailVerification(store, sender)
    for i in range(4):
        owner = 9000 + i
        allow(store, owner)
        endpoint = address(store, 'Target@example.test' if i % 2 else 'target@EXAMPLE.TEST', owner)
        result = service.request(owner, endpoint.id)
        assert (result is not None) is (i < 3)
        clock.advance(seconds=61)
    assert len(sender.calls) == 3


def seed_requests(store, count, issued):
    store.repo.connection.executemany(
        'INSERT INTO email_verifications (endpoint_version, address_digest, token_digest, issued_at, expires_at)'
        ' VALUES (1, ?, ?, ?, ?)',
        [(hashlib.sha256(f'address-{i}'.encode()).hexdigest(), hashlib.sha256(f'token-{i}'.encode()).hexdigest(),
          utc_iso(issued), utc_iso(issued + timedelta(minutes=15))) for i in range(count)],
    )


def test_global_limit_and_bounded_retention(notification_store, clock):
    store, sender = notification_store, CaptureSender()
    endpoint = address(store)
    service = EmailVerification(store, sender)
    seed_requests(store, 100, store.repo.now() - timedelta(minutes=5))
    assert service.request(MIETEK, endpoint.id) is None and not sender.calls
    clock.advance(hours=1)
    assert service.request(MIETEK, endpoint.id)
    store.repo.connection.execute('DELETE FROM email_verifications')
    seed_requests(store, 250, store.repo.now() - timedelta(days=8))
    clock.advance(seconds=61)
    assert service.request(MIETEK, endpoint.id)
    assert len(rows(store)) == 51  # 200 old rows pruned, one new request.


def test_limit_survives_restart_endpoint_and_account_deletion(notification_store, tmp_path, clock):
    store, sender = notification_store, CaptureSender()
    endpoint = address(store)
    EmailVerification(store, sender).request(MIETEK, endpoint.id)
    old_token = sender.calls[0][1]
    store.delete_endpoint(MIETEK, endpoint.id)
    assert rows(store)[0]['endpoint_id'] is None
    endpoint = address(store)
    with LeadRepository(tmp_path / 'notifications.sqlite', now=clock.now_utc) as repo:
        other = EmailVerification(NotificationStore(repo), sender)
        assert other.request(MIETEK, endpoint.id) is None
        assert not other.consume(MIETEK, endpoint.id, old_token)
    store.repo.connection.execute('DELETE FROM bot_users WHERE chat_id = ?', (MIETEK,))
    assert rows(store)[0]['chat_id'] is None
    allow(store, MIETEK)
    endpoint = address(store)
    assert EmailVerification(store, sender).request(MIETEK, endpoint.id) is None


@pytest.mark.parametrize('result,usable', [(DeliveryResult('retry'), False), (DeliveryResult('failed'), False),
    (DeliveryResult('unknown'), True), (TimeoutError('PRIVATE_ERROR'), True), ('invalid', True)])
def test_provider_outcome_and_no_automatic_verification_retry(notification_store, caplog, result, usable):
    store = notification_store
    endpoint, sender = address(store), CaptureSender(result)
    service = EmailVerification(store, sender)
    response = service.request(MIETEK, endpoint.id)
    assert response.outcome in ('retry', 'failed', 'unknown')
    assert service.request(MIETEK, endpoint.id) is None and len(sender.calls) == 1
    assert service.consume(MIETEK, endpoint.id, sender.calls[0][1]) is usable
    assert 'PRIVATE_ERROR' not in caplog.text


def test_verification_writes_refuse_external_transactions(notification_store):
    store, sender = notification_store, CaptureSender()
    endpoint = address(store)
    service = EmailVerification(store, sender)
    with store.repo.transaction():
        with pytest.raises(RuntimeError):
            service.request(MIETEK, endpoint.id)
    assert not rows(store) and not sender.calls
    service.request(MIETEK, endpoint.id)
    with store.repo.transaction():
        with pytest.raises(RuntimeError):
            service.consume(MIETEK, endpoint.id, sender.calls[0][1])
    assert service.consume(MIETEK, endpoint.id, sender.calls[0][1])


def test_failed_endpoint_update_rolls_back_token_consumption(notification_store):
    store, sender = notification_store, CaptureSender()
    endpoint = address(store)
    service = EmailVerification(store, sender)
    service.request(MIETEK, endpoint.id)
    store.repo.connection.execute(
        "CREATE TEMP TRIGGER reject_verification BEFORE UPDATE OF verified_at ON notification_endpoints"
        " BEGIN SELECT RAISE(ABORT, 'simulated proof failure'); END",
    )
    with pytest.raises(sqlite3.IntegrityError, match='simulated proof failure'):
        service.consume(MIETEK, endpoint.id, sender.calls[0][1])
    assert rows(store)[0]['consumed_at'] is None
    assert store.get_endpoint(MIETEK, endpoint.id).verified_at is None
    store.repo.connection.execute('DROP TRIGGER reject_verification')
    assert service.consume(MIETEK, endpoint.id, sender.calls[0][1])


@pytest.mark.parametrize('access', ['open', 'admin'])
def test_existing_access_exceptions_are_respected(notification_store, access):
    store, sender = notification_store, CaptureSender()
    BotStore(store.repo).revoke_access(MIETEK)
    store = NotificationStore(store.repo, access='open' if access == 'open' else 'approval',
                              admins=(MIETEK,) if access == 'admin' else ())
    endpoint = address(store)
    service = EmailVerification(store, sender)
    assert service.request(MIETEK, endpoint.id)
    assert service.consume(MIETEK, endpoint.id, sender.calls[0][1])


def test_change_during_dispatch_does_not_verify_new_address(notification_store):
    store = notification_store
    endpoint = address(store)
    sender = CaptureSender(callback=lambda: store.change_address(MIETEK, endpoint.id, 'nowy@example.test'))
    service = EmailVerification(store, sender)
    assert service.request(MIETEK, endpoint.id).outcome == 'accepted'
    assert sender.calls[0][0].address == 'odbiorca@example.test'
    assert not service.consume(MIETEK, endpoint.id, sender.calls[0][1])
    assert store.get_endpoint(MIETEK, endpoint.id).verified_at is None


@pytest.mark.parametrize('operation', ['request', 'consume'])
def test_concurrent_operations_cannot_bypass_one_time_or_limits(notification_store, tmp_path, clock, operation):
    store, sender = notification_store, CaptureSender()
    endpoint = address(store)
    token = None
    if operation == 'consume':
        EmailVerification(store, sender).request(MIETEK, endpoint.id)
        token = sender.calls[0][1]
    barrier = threading.Barrier(2)
    def run(_):
        with LeadRepository(tmp_path / 'notifications.sqlite', now=clock.now_utc) as repo:
            service = EmailVerification(NotificationStore(repo), sender)
            barrier.wait(timeout=5)
            return service.consume(MIETEK, endpoint.id, token) if token else service.request(MIETEK, endpoint.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (1, 2)))
    assert sum(bool(result) for result in results) == 1
    assert len(sender.calls) == 1


@pytest.fixture
def v13_state(tmp_path, clock, monkeypatch):
    path = tmp_path / 'v13.sqlite'
    with monkeypatch.context() as old:
        old.setattr(storage, '_MIGRATIONS', storage._MIGRATIONS[:13])
        with LeadRepository(path, now=clock.now_utc) as repo:
            store = NotificationStore(repo)
            allow(store, MIETEK)
            repo.upsert(lead('A/1'))
            repo.upsert(lead('B/2'))
            endpoint = ready(store)
            enqueue(store, endpoint)
            history(store, endpoint, 'B/2')
    return path


def version(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute('PRAGMA user_version').fetchone()[0]


def test_v14_preserves_v13_and_makes_exactly_one_backup(v13_state, clock):
    path = v13_state
    before = dump(path)
    with LeadRepository(path, now=clock.now_utc) as repo:
        assert repo.connection.execute('PRAGMA user_version').fetchone()[0] == 14
        assert not repo.connection.execute('PRAGMA foreign_key_check').fetchall()
    after = dump(path)
    assert {key: after[key] for key in before} == before
    assert set(after) - set(before) == {'email_verifications'}
    backup, = (path.parent / 'backups').glob('*-przed-v14-*.sqlite')
    assert version(backup) == 13 and dump(backup) == before
    with LeadRepository(path):
        pass
    assert dump(path) == after and list((path.parent / 'backups').glob('*-przed-v14-*.sqlite')) == [backup]


def test_v14_partial_failure_rolls_back(v13_state, monkeypatch):
    before = dump(v13_state)
    broken = storage._MIGRATIONS[:-1] + (storage._MIGRATIONS[-1] + '\nSELECT broken_v14;',)
    monkeypatch.setattr(storage, '_MIGRATIONS', broken)
    with pytest.raises(sqlite3.OperationalError, match='broken_v14'):
        LeadRepository(v13_state)
    assert version(v13_state) == 13 and dump(v13_state) == before


def test_v13_backup_restore_allows_actual_old_code(v13_state, tmp_path, clock, monkeypatch):
    before = dump(v13_state)
    with LeadRepository(v13_state):
        pass
    backup, = (v13_state.parent / 'backups').glob('*-przed-v14-*.sqlite')
    config_path = tmp_path / 'config.yaml'
    config_path.write_text("gunb:\n  voivodeships: ['28']\nstorage:\n  db_path: v13.sqlite\n", encoding='utf-8')
    assert restore_backup(backup, config_path, now=clock.now_utc) is not None
    assert version(v13_state) == 13 and dump(v13_state) == before
    monkeypatch.setattr(storage, '_MIGRATIONS', storage._MIGRATIONS[:13])
    with LeadRepository(v13_state):
        pass


def test_v14_backup_failure_and_old_code_refusal(v13_state, tmp_path, monkeypatch):
    before = dump(v13_state)
    blocked = tmp_path / 'blocked'
    blocked.write_text('fixture', encoding='utf-8')
    with pytest.raises(OSError):
        LeadRepository(v13_state, backup_dir=blocked)
    assert version(v13_state) == 13 and dump(v13_state) == before
    with LeadRepository(v13_state):
        pass
    upgraded = dump(v13_state)
    monkeypatch.setattr(storage, '_MIGRATIONS', storage._MIGRATIONS[:13])
    with pytest.raises(SchemaTooNew, match='v14'):
        LeadRepository(v13_state)
    assert version(v13_state) == 14 and dump(v13_state) == upgraded
