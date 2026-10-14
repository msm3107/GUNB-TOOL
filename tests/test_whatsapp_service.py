"""Environment/preflight/server tests; synthetic data, no listener or network."""

import logging
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from gunb_tool.storage import LeadRepository, SCHEMA_VERSION
from tests.test_whatsapp_ingress import invoke
from tests.test_whatsapp_inbox_factory import connections, db
from tests.test_whatsapp_webhook import settings


def service():
    from gunb_tool import whatsapp_service
    return whatsapp_service


def env(path):
    value = settings()
    return {'WHATSAPP_WEBHOOK_ENABLED': 'true', 'WHATSAPP_WEBHOOK_DB_PATH': str(path),
            'WHATSAPP_WABA_ID': value.waba_id, 'WHATSAPP_PHONE_NUMBER_ID': value.phone_number_id,
            'WHATSAPP_APP_SECRET': value.app_secret, 'WHATSAPP_VERIFY_TOKEN': value.verify_token}


@pytest.mark.parametrize('enabled', [None, '', 'false', 'TRUE', '1', 'PRIVATE'])
def test_disabled_or_invalid_gate_precedes_database(db, connections, enabled):
    values = env(db)
    if enabled is None:
        values.pop('WHATSAPP_WEBHOOK_ENABLED')
    else:
        values['WHATSAPP_WEBHOOK_ENABLED'] = enabled
    module = service()
    with pytest.raises(module.WebhookServiceError) as caught:
        module.build_application(values)
    assert connections[0] == [] and 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('key', ['WHATSAPP_WEBHOOK_DB_PATH', 'WHATSAPP_WABA_ID', 'WHATSAPP_PHONE_NUMBER_ID',
                               'WHATSAPP_APP_SECRET', 'WHATSAPP_VERIFY_TOKEN'])
@pytest.mark.parametrize('value', [None, '', 'PRIVATE_INVALID'])
def test_invalid_settings_are_private_and_precede_db(db, connections, key, value):
    values = env(db)
    if value is None:
        values.pop(key)
    else:
        values[key] = value
    # App secret permits arbitrary printable ASCII, but this test must be invalid.
    if key == 'WHATSAPP_APP_SECRET' and value == 'PRIVATE_INVALID':
        values[key] += '\n'
    module = service()
    with pytest.raises(module.WebhookServiceError) as caught:
        module.build_application(values)
    assert connections[0] == [] and 'PRIVATE' not in str(caught.value) and str(db) not in str(caught.value)


def test_preflight_closes_before_return_and_real_post_uses_new_full_connection(db, connections):
    app = service().build_application(env(db))
    opened, _ = connections
    assert len(opened) == 1 and opened[0].closed
    assert 'PRAGMA synchronous = FULL' in opened[0].statements
    assert not any(sql.startswith(('BEGIN', 'INSERT', 'UPDATE', 'DELETE')) for sql in opened[0].statements)
    assert invoke(app)[0] == 200 and len(opened) == 2 and all(conn.closed for conn in opened)
    assert invoke(app)[0] == 200
    with LeadRepository(db) as bot:
        assert bot.connection.execute('SELECT COUNT(*) FROM notification_webhook_events').fetchone()[0] == 1


@pytest.mark.parametrize('kind', ['missing', 'old_schema', 'missing_table', 'non_wal'])
def test_preflight_rejects_unready_db_without_migration(db, kind):
    if kind == 'missing':
        db.unlink()  # Temporary synthetic fixture only.
    else:
        with LeadRepository(db) as repo:
            if kind == 'old_schema':
                repo.connection.execute(f'PRAGMA user_version={SCHEMA_VERSION - 1}')
            elif kind == 'missing_table':
                repo.connection.execute('DROP TABLE notification_webhook_events')
        if kind == 'non_wal':
            import sqlite3
            from contextlib import closing
            with closing(sqlite3.connect(db)) as conn:
                conn.execute('PRAGMA journal_mode=DELETE')
    module = service()
    with pytest.raises(module.WebhookServiceError) as caught:
        module.build_application(env(db))
    assert str(db) not in str(caught.value)
    assert not (db.parent / 'backups').exists()
    if kind == 'missing':
        assert not db.exists()


def test_check_uses_environment_only_without_server_or_dotenv(db, monkeypatch, capsys, tmp_path):
    module = service()
    values = env(db)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(tmp_path)
    (tmp_path / '.env').write_text('WHATSAPP_APP_SECRET=PRIVATE_DOTENV\n', encoding='utf-8')

    def forbidden(app):
        raise AssertionError('Must not start/import server')

    monkeypatch.setattr(module, '_serve', forbidden)
    assert module.main(['--check']) == 0
    output = capsys.readouterr()
    assert 'PRIVATE' not in output.out + output.err and str(db) not in output.out + output.err
    assert settings().app_secret not in output.out + output.err


@pytest.mark.parametrize('platform', ['win32', 'darwin'])
def test_serve_unsupported_platform_rejected_before_db_or_import(db, monkeypatch, connections, platform):
    module = service()
    monkeypatch.setattr(module.sys, 'platform', platform)
    monkeypatch.setattr(module, 'build_application', lambda *_: pytest.fail('DB/preflight forbidden'))
    monkeypatch.setattr(module, '_serve', lambda *_: pytest.fail('server forbidden'))
    assert module.main([]) == 2 and connections[0] == []


def test_cli_unknown_arguments_and_config_errors_never_echo_values(monkeypatch, capsys):
    module = service()
    assert module.main(['--token', 'PRIVATE_TOKEN']) == 2
    monkeypatch.delenv('WHATSAPP_WEBHOOK_ENABLED', raising=False)
    assert module.main(['--check']) == 2
    assert 'PRIVATE' not in ''.join(capsys.readouterr())


def test_systemd_config_failure_exits_main_without_prestart_restart_loop(monkeypatch, connections):
    module = service()
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    monkeypatch.setenv('WHATSAPP_WEBHOOK_ENABLED', 'false')
    monkeypatch.setattr(module, '_serve', lambda *_: pytest.fail('No server for invalid config'))
    assert module.main([]) == 2 and connections[0] == []
    unit = (Path(__file__).resolve().parents[1] / 'deploy/gunb-whatsapp-webhook.service.example').read_text(encoding='utf-8')
    # RestartPreventExitStatus applies to ExecStart, not a failing ExecStartPre.
    assert not any(line.startswith('ExecStartPre=') for line in unit.splitlines())
    assert 'RestartPreventExitStatus=2' in unit
    assert 'ExecStart=/opt/gunb-tool/current/.venv/bin/python -m gunb_tool.whatsapp_service\n' in unit


@pytest.mark.parametrize('fault,code', [(RuntimeError('PRIVATE_RUNTIME'), 1),
                                     (ImportError('PRIVATE_DEPENDENCY'), 2),
                                     (SystemExit('PRIVATE_EXIT'), 1), (SystemExit(0), 0)])
def test_serve_errors_are_private_with_correct_exit(monkeypatch, capsys, fault, code):
    module = service()
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    sentinel = object()
    monkeypatch.setattr(module, 'build_application', lambda: sentinel)

    def failed(app):
        assert app is sentinel
        raise fault

    monkeypatch.setattr(module, '_serve', failed)
    assert module.main([]) == code
    assert 'PRIVATE' not in ''.join(capsys.readouterr())


def test_serve_success_passes_constructed_app_to_separate_boundary(monkeypatch):
    module = service()
    monkeypatch.setattr(module.sys, 'platform', 'linux')
    sentinel, received = object(), []
    monkeypatch.setattr(module, 'build_application', lambda: sentinel)
    monkeypatch.setattr(module, '_serve', received.append)
    assert module.main([]) == 0 and received == [sentinel]


def test_serve_strips_inherited_socket_activation_and_server_options(monkeypatch):
    module = service()
    names = ('GUNICORN_CMD_ARGS', 'GUNICORN_FD', 'GUNICORN_PID', 'LISTEN_FDS', 'LISTEN_PID',
             'LISTEN_FDNAMES', 'WEB_CONCURRENCY', 'FORWARDED_ALLOW_IPS', 'PORT', 'NOTIFY_SOCKET')
    for name in names:
        monkeypatch.setenv(name, 'PRIVATE_OVERRIDE')
    observed = []
    monkeypatch.setitem(sys.modules, 'gunb_tool.whatsapp_server', SimpleNamespace(
        serve=lambda app: observed.append(all(name not in module.os.environ for name in names))))
    module._serve(object())
    assert observed == [True]


def test_fixed_profile_bounds_workers_and_trust_without_raw_logging():
    options = service().server_options()
    expected = {'bind': '127.0.0.1:8765', 'workers': 2, 'worker_class': 'sync', 'threads': 1,
                'timeout': 30, 'graceful_timeout': 10, 'backlog': 32, 'max_requests': 1000,
                'max_requests_jitter': 100, 'keepalive': 0, 'limit_request_line': 4094,
                'limit_request_fields': 32, 'limit_request_field_size': 4096,
                'forwarded_allow_ips': '127.0.0.1', 'secure_scheme_headers': {'X-FORWARDED-PROTO': 'https'},
                'forwarder_headers': '', 'header_map': 'refuse', 'proxy_protocol': False,
                'http_protocols': 'h1', 'http_parser': 'python', 'control_socket_disable': True,
                'preload_app': True, 'reload': False, 'spew': False, 'daemon': False,
                'accesslog': None, 'capture_output': False, 'worker_tmp_dir': '/dev/shm'}
    assert {key: options[key] for key in expected} == expected


def test_private_logger_discards_message_args_tracebacks_and_access():
    module, records = service(), []

    class Logger(module.PrivateLogMixin):
        error_log = SimpleNamespace(log=lambda *args, **kwargs: records.append((args, kwargs)))

    logger = Logger()
    for method in ('critical', 'error', 'warning', 'info', 'debug', 'exception'):
        getattr(logger, method)('PRIVATE_QUERY_TOKEN', 'PRIVATE_BODY', exc_info=True)
    logger.log(logging.ERROR, 'PRIVATE_SIGNATURE', exc_info=True)
    logger.access('PRIVATE_URL', 'PRIVATE_HEADERS', 'PRIVATE_BODY', 'PRIVATE_TIME')
    assert records and all('PRIVATE' not in repr(record) and not record[1] for record in records)


@pytest.mark.skipif(sys.platform != 'linux', reason='Gunicorn serving profile is Linux-only')
def test_actual_gunicorn_config_ignores_environment_and_config_file(tmp_path, monkeypatch):
    from gunb_tool.whatsapp_server import make_server, PrivateLogger
    import gunicorn
    assert gunicorn.__version__ == '26.2.0'
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'gunicorn.conf.py').write_text("raise RuntimeError('PRIVATE_CONFIG_LOADED')", encoding='utf-8')
    monkeypatch.setenv('GUNICORN_CMD_ARGS', '--bind=0.0.0.0:443 --workers=99 --access-logfile=- --config=PRIVATE')
    monkeypatch.setenv('WEB_CONCURRENCY', '99')
    monkeypatch.setenv('FORWARDED_ALLOW_IPS', '*')
    app = object()
    server = make_server(app)
    assert server.wsgi() is app
    assert server.cfg.bind == ['127.0.0.1:8765'] and server.cfg.workers == 2
    assert server.cfg.forwarded_allow_ips == ['127.0.0.1'] and server.cfg.accesslog is None
    assert server.cfg.forwarder_headers == []
    assert server.cfg.logger_class is PrivateLogger and server.cfg.control_socket_disable


@pytest.mark.skipif(sys.platform != 'linux', reason='Gunicorn serving profile is Linux-only')
def test_actual_logger_and_arbiter_boundary_do_not_bind_or_expose_details(monkeypatch, caplog):
    from gunb_tool import whatsapp_server
    server = whatsapp_server.make_server(object())
    logger = whatsapp_server.PrivateLogger(server.cfg)
    logger.warning('PRIVATE_QUERY', 'PRIVATE_HEADER')
    try:
        raise ValueError('PRIVATE_BODY')
    except ValueError:
        logger.exception('PRIVATE_EXCEPTION')
    assert 'PRIVATE' not in caplog.text and 'Traceback' not in caplog.text
    called = []

    class Arbiter:
        def __init__(self, app):
            called.append(app)

        def run(self):
            called.append('run')

    monkeypatch.setattr(whatsapp_server, 'Arbiter', Arbiter)
    whatsapp_server.serve(object())
    assert len(called) == 2 and called[-1] == 'run'


@pytest.mark.skipif(sys.platform != 'linux', reason='Gunicorn serving profile is Linux-only')
@pytest.mark.parametrize('headers', [b'Content-Length: 1\r\nContent-Length: 1',
                                    b'Content-Length: 1\r\nTransfer-Encoding: chunked',
                                    b'X_HUB_SIGNATURE_256: private'])
def test_actual_http_parser_rejects_ambiguous_framing_and_header_mapping(headers):
    from gunb_tool.whatsapp_server import make_server
    from gunicorn.http.parser import RequestParser
    from gunicorn.http.errors import InvalidHeader, InvalidHeaderName
    raw = b'POST /webhooks/whatsapp HTTP/1.1\r\nHost: localhost\r\n' + headers + b'\r\n\r\nx'
    with pytest.raises((InvalidHeader, InvalidHeaderName)):
        next(RequestParser(make_server(object()).cfg, [raw], ('127.0.0.1', 10000)))


@pytest.mark.skipif(sys.platform != 'linux', reason='Gunicorn serving profile is Linux-only')
@pytest.mark.parametrize('address,scheme', [('127.0.0.1', 'https'), ('203.0.113.1', 'http')])
def test_actual_http_parser_trusts_scheme_only_from_fixed_loopback(address, scheme):
    from gunb_tool.whatsapp_server import make_server
    from gunicorn.http.parser import RequestParser
    raw = b'GET /webhooks/whatsapp HTTP/1.1\r\nHost: localhost\r\nX-Forwarded-Proto: https\r\n\r\n'
    assert next(RequestParser(make_server(object()).cfg, [raw], (address, 10000))).scheme == scheme
