"""In-process WSGI requests and temporary SQLite only; no listener or real HTTP."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import replace
import io
import sqlite3
import threading
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import pytest

from gunb_tool.notification_store import NotificationStore
from gunb_tool.storage import LeadRepository
from gunb_tool.whatsapp_inbox import WhatsAppInbox
from tests.test_whatsapp_inbox import signed, status
from tests.test_whatsapp_webhook import TOKEN, settings, signature


class Factory:
    def __init__(self, path):
        self.path, self.opens, self.closes = path, 0, 0
        self.settings, self.max_events, self.failure = settings(), 10000, None
        self.connections, self.threads = [], []
        with LeadRepository(path):
            pass  # Startup already migrated; requests never initialize this fixture DB.

    @contextmanager
    def __call__(self):
        self.opens += 1
        if self.failure == 'open':
            raise OSError('PRIVATE_OPEN_SECRET')
        try:
            with LeadRepository(self.path) as repo:
                self.connections.append(repo.connection)
                self.threads.append(threading.get_ident())
                yield WhatsAppInbox(NotificationStore(repo), self.settings, max_events=self.max_events)
            if self.failure == 'close':
                raise OSError('PRIVATE_CLOSE_SECRET')
        finally:
            self.closes += 1

    def rows(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute('SELECT * FROM notification_webhook_events')]


@pytest.fixture
def factory(tmp_path):
    return Factory(tmp_path / 'ingress.sqlite')


def application(factory, **kwargs):
    from gunb_tool.whatsapp_ingress import WhatsAppWebhookApplication
    return WhatsAppWebhookApplication(settings(), factory, **kwargs)


def environment(raw=None, **changes):
    raw, header = signed() if raw is None else (raw, signature(raw))
    env = {}
    setup_testing_defaults(env)
    env.update(REQUEST_METHOD='POST', PATH_INFO='/webhooks/whatsapp', QUERY_STRING='',
               CONTENT_TYPE='application/json', CONTENT_LENGTH=str(len(raw)), HTTP_X_HUB_SIGNATURE_256=header)
    env.update({'wsgi.url_scheme': 'https', 'wsgi.input': io.BytesIO(raw), 'wsgi.errors': io.StringIO()})
    env.update(changes)
    return env


def invoke(app, env=None, *, on_start=None, validate=False):
    env = environment() if env is None else env
    responses = []

    def start(code, headers, exc_info=None):
        if on_start:
            on_start(code)
        responses.append((code, dict(headers)))
        return lambda data: None

    result = (validator(app) if validate else app)(env, start)
    try:
        body = b''.join(result)
    finally:
        if hasattr(result, 'close'):
            result.close()
    assert len(responses) == 1
    code, headers = responses[0]
    assert headers['Content-Length'] == str(len(body))
    assert headers['Content-Type'] == 'text/plain; charset=utf-8'
    assert headers['Cache-Control'] == 'no-store'
    assert headers['X-Content-Type-Options'] == 'nosniff'
    if code.startswith('503'):
        assert headers['Retry-After'] == '5'
    return int(code[:3]), headers, body


def query(**changes):
    values = {'hub.mode': 'subscribe', 'hub.verify_token': TOKEN, 'hub.challenge': '123456'}
    values.update(changes)
    return urlencode(values)


def test_get_exact_challenge_wsgi_contract_without_body_or_db(factory):
    env = environment(REQUEST_METHOD='GET', CONTENT_LENGTH='0', QUERY_STRING=query())
    env['wsgi.input'] = NoRead()
    code, _, body = invoke(application(factory), env, validate=True)
    assert (code, body) == (200, b'123456') and factory.opens == 0


class NoRead(io.BytesIO):
    def read(self, size=-1):
        raise AssertionError('Input must not be read')

    readline = read
    readlines = read


@pytest.mark.parametrize('value,expected', [
    ('', 400), (query() + '&hub.challenge=123456', 400), (query() + '&extra=value', 400),
    (query().replace('hub.challenge=123456', 'hub.challenge=%ZZ'), 400),
    (query().replace('hub.challenge=123456', 'hub.challenge=%FF'), 400),
    ('x' * 2049, 400), (query() + 'é', 400),
    (query(**{'hub.mode': 'other'}), 403), (query(**{'hub.verify_token': 'PRIVATE_BAD_TOKEN'}), 403),
    (query(**{'hub.challenge': '<script>'}), 403),
])
def test_invalid_get_query_rejected_without_db_or_secret_echo(factory, value, expected, caplog):
    env = environment(REQUEST_METHOD='GET', CONTENT_LENGTH='', QUERY_STRING=value)
    code, _, body = invoke(application(factory), env)
    assert code == expected and factory.opens == 0
    assert TOKEN not in body.decode() + caplog.text and 'PRIVATE_BAD_TOKEN' not in body.decode() + caplog.text


@pytest.mark.parametrize('changes,expected', [
    ({'PATH_INFO': '/other'}, 404), ({'PATH_INFO': '/webhooks/whatsapp/'}, 404),
    ({'REQUEST_METHOD': 'PUT'}, 405), ({'REQUEST_METHOD': 'HEAD'}, 405),
    ({'wsgi.url_scheme': 'http', 'HTTP_X_FORWARDED_PROTO': 'https'}, 403),
    ({'wsgi.url_scheme': 'http', 'HTTP_FORWARDED': 'proto=https'}, 403),
    ({'CONTENT_LENGTH': ''}, 411), ({'CONTENT_LENGTH': None}, 411),
    ({'CONTENT_LENGTH': '0'}, 400), ({'CONTENT_LENGTH': '-1'}, 400),
    ({'CONTENT_LENGTH': '1,1'}, 400), ({'CONTENT_LENGTH': '+1'}, 400),
    ({'CONTENT_LENGTH': ' 1'}, 400), ({'CONTENT_LENGTH': '١'}, 400),
    ({'CONTENT_LENGTH': '65537'}, 413), ({'CONTENT_LENGTH': '9999999999'}, 413),
    ({'CONTENT_TYPE': 'text/plain'}, 415), ({'CONTENT_TYPE': 'application/json; charset=latin1'}, 415),
    ({'CONTENT_TYPE': 'application/json; charset=utf-8; charset=latin1'}, 415),
    ({'HTTP_CONTENT_ENCODING': 'gzip'}, 415), ({'HTTP_TRANSFER_ENCODING': 'chunked'}, 400),
    ({'QUERY_STRING': 'private=query'}, 400),
    ({'REQUEST_METHOD': 'GET', 'QUERY_STRING': query(), 'CONTENT_LENGTH': '1'}, 400),
])
def test_early_rejections_do_not_read_or_open_db(factory, changes, expected):
    env = environment(**changes)
    stream = io.BytesIO(b'PRIVATE_BODY')
    env['wsgi.input'] = stream
    code, headers, _ = invoke(application(factory), env)
    assert code == expected and factory.opens == 0 and stream.tell() == 0
    if expected == 405:
        assert headers['Allow'] == 'GET, POST'


def test_post_ack_only_after_commit_and_context_exit_and_replay_is_idempotent(factory):
    app = application(factory)

    def committed(code):
        assert code == '200 OK'
        assert len(factory.rows()) == 1  # Independently visible before HTTP start_response.
        assert factory.closes == factory.opens
        with pytest.raises(sqlite3.ProgrammingError):
            factory.connections[-1].execute('SELECT 1')

    assert invoke(app, on_start=committed, validate=True)[::2] == (200, b'OK\n')
    first = factory.rows()[0]
    assert invoke(app, on_start=committed)[0] == 200
    assert factory.rows() == [first]


@pytest.mark.parametrize('media', ['application/json', 'Application/JSON; charset=UTF-8', 'application/json; charset="utf-8"'])
def test_raw_short_reads_preserve_hmac_and_never_read_past_declared_length(factory, media):
    raw, _ = signed()
    raw = b' \n' + raw + b'\n '

    class ShortReader(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 8192
            return super().read(min(size, 7))

    stream = ShortReader(raw + b'UNREAD_NEXT_REQUEST')
    env = environment(raw, CONTENT_TYPE=media, HTTP_CONTENT_ENCODING='identity')
    env['wsgi.input'] = stream
    assert invoke(application(factory), env)[0] == 200
    assert stream.tell() == len(raw) and not stream.closed and len(factory.rows()) == 1


def test_exact_maximum_body_accepted(factory):
    raw, _ = signed()
    raw += b' ' * (65536 - len(raw))
    assert invoke(application(factory), environment(raw))[0] == 200


@pytest.mark.parametrize('kind,expected', [('truncated', 400), ('text', 400), ('overread', 400), ('io', 503)])
def test_bad_input_stream_never_opens_db_and_releases_capacity(factory, kind, expected, caplog):
    class Broken:
        def read(self, size):
            if kind == 'io':
                raise OSError('PRIVATE_STREAM_SECRET')
            return {'truncated': b'', 'text': 'bad', 'overread': b'x' * (size + 1)}[kind]

    app = application(factory, max_inflight=1)
    env = environment()
    env['wsgi.input'] = Broken()
    code, _, body = invoke(app, env)
    assert code == expected and factory.opens == 0 and 'PRIVATE' not in body.decode() + caplog.text
    assert invoke(app)[0] == 200


@pytest.mark.parametrize('kind', ['bad_signature', 'missing_signature', 'signed_invalid_json'])
def test_authentication_and_entire_batch_validation_precede_factory(factory, kind):
    env = environment()
    if kind == 'bad_signature':
        env['HTTP_X_HUB_SIGNATURE_256'] = 'sha256=' + '0' * 64
    elif kind == 'missing_signature':
        del env['HTTP_X_HUB_SIGNATURE_256']
    else:
        env = environment(b'{')
    assert invoke(application(factory), env)[0] == 403
    assert factory.opens == 0


def test_authenticated_irrelevant_batch_still_commits_and_acks(factory):
    raw, _ = signed(status('future_status'))
    assert invoke(application(factory), environment(raw))[0] == 200
    assert factory.opens == factory.closes == 1 and factory.rows() == []


@pytest.mark.parametrize('field,value', [('waba_id', '9999'), ('phone_number_id', '9999'),
                                         ('app_secret', 'other-secret'), ('verify_token', 'other-verify-token')])
def test_factory_must_use_same_settings_to_prevent_silent_ack_of_wrong_scope(factory, field, value):
    factory.settings = replace(settings(), **{field: value})
    assert invoke(application(factory))[0] == 503
    assert factory.opens == factory.closes == 1 and factory.rows() == []


@pytest.mark.parametrize('kind', ['open', 'close', 'wrong_type'])
def test_factory_failures_do_not_ack_or_leak_and_capacity_is_reusable(factory, kind, caplog):
    @contextmanager
    def wrong():
        yield object()

    factory.failure = kind
    app = application(wrong if kind == 'wrong_type' else factory, max_inflight=1)
    env = environment()
    code, _, body = invoke(app, env)
    assert code == 503 and 'PRIVATE' not in body.decode() + caplog.text + env['wsgi.errors'].getvalue()
    if kind != 'wrong_type':
        factory.failure = None
        assert invoke(app)[0] == 200 and len(factory.rows()) == 1


def test_inbox_capacity_no_ack_but_duplicate_still_ack(factory):
    factory.max_events = 1
    app = application(factory)
    assert invoke(app)[0] == 200
    raw, _ = signed(status(ident='wamid.other'))
    assert invoke(app, environment(raw))[0] == 503
    assert invoke(app)[0] == 200 and len(factory.rows()) == 1


def test_real_deferred_commit_failure_returns_503_and_next_request_can_retry(factory):
    with LeadRepository(factory.path) as repo:
        repo.connection.execute('CREATE TABLE commit_guard(endpoint_id INTEGER REFERENCES notification_endpoints(id) DEFERRABLE INITIALLY DEFERRED)')
        repo.connection.execute('CREATE TRIGGER fail_commit AFTER INSERT ON notification_webhook_events BEGIN INSERT INTO commit_guard VALUES(999999); END')
    app = application(factory, max_inflight=1)
    assert invoke(app)[0] == 503 and factory.rows() == []
    with LeadRepository(factory.path) as repo:
        repo.connection.execute('DROP TRIGGER fail_commit')
    assert invoke(app)[0] == 200 and len(factory.rows()) == 1


def test_context_suppressing_receive_failure_cannot_authorize_ack(factory):
    factory.max_events = 1
    assert invoke(application(factory))[0] == 200

    @contextmanager
    def suppressing():
        try:
            with factory() as receiver:
                yield receiver
        except Exception:
            pass

    raw, _ = signed(status(ident='wamid.other'))
    assert invoke(application(suppressing), environment(raw))[0] == 503
    assert len(factory.rows()) == 1


def test_concurrent_requests_use_distinct_connections_in_their_own_threads(factory):
    gate = threading.Barrier(2)

    @contextmanager
    def owned():
        with factory() as receiver:
            gate.wait(timeout=5)
            yield receiver

    app = application(owned, max_inflight=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(app)[0], range(2)))
    assert results == [200, 200] and len(factory.rows()) == 1
    assert len(set(factory.threads)) == 2 and factory.connections[0] is not factory.connections[1]


def test_overload_is_nonblocking_without_read_and_capacity_recovers(factory):
    entered, release = threading.Event(), threading.Event()

    @contextmanager
    def held():
        with factory() as receiver:
            entered.set()
            assert release.wait(timeout=5)
            yield receiver

    app = application(held, max_inflight=1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(invoke, app)
        assert entered.wait(timeout=5)
        try:
            env = environment()
            assert invoke(app, env)[0] == 503 and env['wsgi.input'].tell() == 0
            assert factory.opens == 1
        finally:
            release.set()
        assert first.result(timeout=5)[0] == 200
    assert invoke(app)[0] == 200


def test_start_response_failure_is_not_retried_and_resources_already_closed(factory):
    app = application(factory, max_inflight=1)

    def fail(code, headers, exc_info=None):
        assert factory.closes == 1
        raise RuntimeError('transport closed')

    with pytest.raises(RuntimeError, match='transport closed'):
        app(environment(), fail)
    assert invoke(app)[0] == 200 and len(factory.rows()) == 1


@pytest.mark.parametrize('capacity', [0, 17, True, 1.5, '4'])
def test_invalid_capacity_is_generic(factory, capacity):
    with pytest.raises(ValueError):
        application(factory, max_inflight=capacity)


def test_invalid_factory_or_settings_rejected_without_echo():
    from gunb_tool.whatsapp_ingress import WhatsAppWebhookApplication
    for args in [('PRIVATE_SECRET', lambda: None), (settings(), 'PRIVATE_SECRET')]:
        with pytest.raises(ValueError) as caught:
            WhatsAppWebhookApplication(*args)
        assert 'PRIVATE' not in str(caught.value)
