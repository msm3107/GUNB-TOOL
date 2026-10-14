"""Request factory/storage boundary: temporary files only, no server or sends."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from gunb_tool import storage
from gunb_tool.storage import LeadRepository, SCHEMA_VERSION
from gunb_tool.whatsapp_ingress import WhatsAppWebhookApplication
from tests.test_whatsapp_ingress import environment, invoke
from tests.test_whatsapp_inbox import signed, status
from tests.test_whatsapp_webhook import settings


def factory(path, **kwargs):
    from gunb_tool.whatsapp_inbox_factory import WhatsAppInboxFactory
    return WhatsAppInboxFactory(path, settings(), **kwargs)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'inbox.sqlite'
    with LeadRepository(path):
        pass
    return path


@pytest.fixture
def connections(monkeypatch):
    original, opened = sqlite3.connect, []

    class Tracked(sqlite3.Connection):
        fault = None

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.closed, self.owner, self.statements = False, threading.get_ident(), []

        def execute(self, sql, *args, **kwargs):
            self.statements.append(sql)
            if self.fault == 'setup' and sql == 'PRAGMA foreign_keys = ON':
                raise sqlite3.OperationalError('PRIVATE_SETUP')
            if self.fault == 'full_set' and sql == 'PRAGMA synchronous = FULL':
                raise sqlite3.OperationalError('PRIVATE_FULL')
            if self.fault == 'full_verify' and sql == 'PRAGMA synchronous':
                return SimpleNamespace(fetchone=lambda: (1,))
            return super().execute(sql, *args, **kwargs)

        def close(self):
            super().close()
            self.closed = True
            if self.fault == 'close':
                raise sqlite3.OperationalError('PRIVATE_CLOSE')

    def connect(*args, **kwargs):
        kwargs['factory'] = Tracked
        connection = original(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(storage.sqlite3, 'connect', connect)
    return opened, Tracked


def snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        return (conn.execute('PRAGMA user_version').fetchone()[0],
                conn.execute('PRAGMA journal_mode').fetchone()[0],
                conn.execute('SELECT type,name,sql FROM sqlite_master ORDER BY type,name').fetchall())


def test_existing_only_requires_current_wal_without_migration_and_allows_inbox_dml(db, monkeypatch):
    def forbidden(self):
        raise AssertionError('Migration must not be invoked')

    monkeypatch.setattr(LeadRepository, '_migrate', forbidden)
    with LeadRepository(db, existing_only=True) as repo:
        assert repo.journal_mode == 'wal' and repo.busy_timeout_ms == 5000
        assert repo.connection.execute('PRAGMA foreign_keys').fetchone()[0] == 1
        with repo.transaction():
            repo.connection.execute("INSERT INTO zadania(nazwa, ostatnio) VALUES('factory-test', '2026-10-13T00:00:00+00:00')")


def test_normal_repository_defaults_and_migrations_unchanged(tmp_path):
    with LeadRepository(tmp_path / 'new' / 'normal.sqlite') as repo:
        assert repo.connection.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
        assert repo.journal_mode == 'wal'
        assert repo.connection.execute('PRAGMA synchronous').fetchone()[0] == 1


def test_normal_connection_setup_fault_closes_handle(db, connections):
    opened, tracked = connections
    tracked.fault = 'setup'
    with pytest.raises(sqlite3.OperationalError):
        LeadRepository(db)
    assert opened[-1].closed


def test_factory_pins_relative_path_across_working_directory_change(db, monkeypatch, tmp_path):
    monkeypatch.chdir(db.parent)
    make = factory(db.name)
    elsewhere = tmp_path / 'other'
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    with make() as inbox:
        assert Path(inbox.repo.connection.execute('PRAGMA database_list').fetchone()[2]).resolve() == db.resolve()


@pytest.mark.parametrize('target', ['missing.sqlite', 'absent/dir/missing.sqlite'])
def test_existing_only_never_creates_file_or_parent(tmp_path, target):
    path = tmp_path / target
    with pytest.raises((ValueError, RuntimeError, sqlite3.Error)):
        LeadRepository(path, existing_only=True)
    assert not path.exists()
    if '/' in target:
        assert not (tmp_path / 'absent').exists()


@pytest.mark.parametrize('version', [0, SCHEMA_VERSION - 1, SCHEMA_VERSION + 1])
def test_existing_only_refuses_wrong_version_without_schema_change_or_backup(db, version, connections):
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(f'PRAGMA user_version={version}')
    before = snapshot(db)
    with pytest.raises(RuntimeError):
        LeadRepository(db, existing_only=True)
    assert connections[0][-1].closed
    assert snapshot(db) == before and not (db.parent / 'backups').exists()


def test_existing_only_refuses_non_wal_without_conversion(db, connections):
    with closing(sqlite3.connect(db)) as conn:
        conn.execute('PRAGMA journal_mode=DELETE')
    before = snapshot(db)
    with pytest.raises(RuntimeError):
        LeadRepository(db, existing_only=True)
    assert connections[0][-1].closed
    assert snapshot(db) == before


@pytest.mark.parametrize('value', [':memory:'])
def test_existing_only_rejects_memory(value):
    with pytest.raises(ValueError):
        LeadRepository(value, existing_only=True)


@pytest.mark.parametrize('name', ['with space.sqlite', 'żółta#%25.sqlite'])
def test_escaped_path_names_real_file_without_uri_injection(tmp_path, name):
    path = tmp_path / name
    with LeadRepository(path):
        pass
    with factory(path)() as inbox:
        assert Path(inbox.repo.connection.execute('PRAGMA database_list').fetchone()[2]).resolve() == path.resolve()
        assert inbox.repo.connection.execute('PRAGMA synchronous').fetchone()[0] == 2


def test_factory_constructor_opens_nothing_and_hmac_rejection_precedes_real_open(db, connections):
    make = factory(db)
    assert connections[0] == []
    env = environment(HTTP_X_HUB_SIGNATURE_256='sha256=' + '0' * 64)
    assert invoke(WhatsAppWebhookApplication(settings(), make), env)[0] == 403
    assert connections[0] == []


def test_each_request_full_before_receive_with_bot_connection_kept_normal(db, connections):
    with LeadRepository(db) as bot:
        make = factory(db)
        app = WhatsAppWebhookApplication(settings(), make)

        def committed(code):
            assert code == '200 OK' and connections[0][-1].closed
            assert bot.connection.execute('SELECT COUNT(*) FROM notification_webhook_events').fetchone()[0] == 1

        for _ in range(2):
            with make() as inbox:
                assert inbox.webhook_settings == settings()
                assert inbox.repo.connection.execute('PRAGMA synchronous').fetchone()[0] == 2
                assert not inbox.repo.connection.in_transaction
                assert bot.connection.execute('PRAGMA synchronous').fetchone()[0] == 1
            assert invoke(app, on_start=committed)[0] == 200
        requests = connections[0][1:]
        assert len({id(conn) for conn in requests}) == 4 and all(conn.closed for conn in requests)
        for conn in requests:
            full_index = conn.statements.index('PRAGMA synchronous = FULL')
            begin = next((i for i, sql in enumerate(conn.statements) if sql == 'BEGIN IMMEDIATE'), len(conn.statements))
            assert full_index < begin
        assert bot.connection.execute('PRAGMA synchronous').fetchone()[0] == 1


@pytest.mark.parametrize('fault', ['setup', 'full_set', 'full_verify', 'close'])
def test_setup_or_close_failure_closes_and_never_acks_or_leaks(db, connections, fault, caplog):
    opened, tracked = connections
    tracked.fault = fault
    env = environment()
    app = WhatsAppWebhookApplication(settings(), factory(db), max_inflight=1)
    code, _, body = invoke(app, env)
    assert code == 503 and opened[-1].closed
    assert 'PRIVATE' not in body.decode() + caplog.text + env['wsgi.errors'].getvalue()
    tracked.fault = None
    assert invoke(app)[0] == 200 and all(conn.closed for conn in opened)


def test_deleted_database_is_not_recreated_by_request(db, connections):
    make = factory(db)
    db.unlink()  # Synthetic file only; no open handles at this point.
    assert invoke(WhatsAppWebhookApplication(settings(), make))[0] == 503
    assert not db.exists() and connections[0] == []


def test_wrong_schema_factory_returns_503_without_migration(db):
    with closing(sqlite3.connect(db)) as conn:
        conn.execute(f'PRAGMA user_version={SCHEMA_VERSION - 1}')
    before = snapshot(db)
    assert invoke(WhatsAppWebhookApplication(settings(), factory(db)))[0] == 503
    assert snapshot(db) == before


def test_real_deferred_commit_failure_closes_full_connection_and_retries(db, connections):
    with LeadRepository(db) as bot:
        bot.connection.execute('CREATE TABLE guard(endpoint_id INTEGER REFERENCES notification_endpoints(id) DEFERRABLE INITIALLY DEFERRED)')
        bot.connection.execute('CREATE TRIGGER fail_commit AFTER INSERT ON notification_webhook_events BEGIN INSERT INTO guard VALUES(999999); END')
        app = WhatsAppWebhookApplication(settings(), factory(db))
        assert invoke(app)[0] == 503 and connections[0][-1].closed
        assert bot.connection.execute('SELECT COUNT(*) FROM notification_webhook_events').fetchone()[0] == 0
        bot.connection.execute('DROP TRIGGER fail_commit')
        assert invoke(app)[0] == 200 and connections[0][-1].closed


def test_real_factory_capacity_and_replay(db):
    app = WhatsAppWebhookApplication(settings(), factory(db, max_events=1))
    assert invoke(app)[0] == 200
    raw, _ = signed(status(ident='wamid.other'))
    assert invoke(app, environment(raw))[0] == 503
    assert invoke(app)[0] == 200


def test_concurrent_real_factory_has_thread_owned_connections(db, connections):
    barrier = threading.Barrier(2)
    make = factory(db)

    @contextmanager
    def simultaneous():
        with make() as inbox:
            assert inbox.repo.connection.execute('PRAGMA synchronous').fetchone()[0] == 2
            barrier.wait(timeout=5)
            yield inbox

    app = WhatsAppWebhookApplication(settings(), simultaneous)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: invoke(app)[0], range(2))) == [200, 200]
    assert len({conn.owner for conn in connections[0]}) == 2
    assert len(connections[0]) == 2 and all(conn.closed for conn in connections[0])


@pytest.mark.parametrize('path', ['', ':memory:', 'file:PRIVATE?mode=memory', r'\\PRIVATE\share\db', '//PRIVATE/share/db'])
def test_factory_rejects_non_file_paths_privately(path):
    with pytest.raises(ValueError) as caught:
        factory(path)
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('capacity', [0, 100001, True, 1.5, '4'])
def test_factory_rejects_invalid_capacity(db, capacity):
    with pytest.raises(ValueError):
        factory(db, max_events=capacity)


def test_factory_rejects_settings_privately_and_repr_contains_no_path_or_secret(db):
    from gunb_tool.whatsapp_inbox_factory import WhatsAppInboxFactory
    with pytest.raises(ValueError) as caught:
        WhatsAppInboxFactory(db, 'PRIVATE_SECRET')
    assert 'PRIVATE' not in str(caught.value)
    assert str(db) not in repr(factory(db)) and settings().app_secret not in repr(factory(db))
