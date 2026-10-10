"""Meta boundary tests: synthetic recipients and intercepted HTTP only."""

from dataclasses import replace
import json

import pytest
import requests

from gunb_tool.notification_models import LeadRef, NotificationEndpoint, ReportPart
from tests.test_notification_store import notification_store, ready, report as stored_report


PHONE = '48111222333'
KEY = 'gunb-outbox:' + 'a' * 64
STAMP = '2026-10-10T07:00:00+00:00'


def settings(**changes):
    from gunb_tool.whatsapp_sender import WhatsAppSettings
    args = dict(api_version='v24.0', phone_number_id='123456', access_token='synthetic-secret',
                template_name='gunb_report', language='pl', timeout_seconds=2)
    args.update(changes)
    return WhatsAppSettings(**args)


def endpoint(**changes):
    value = NotificationEndpoint(1, 700, 'whatsapp', '+' + PHONE, True, 'rano', STAMP,
                                 STAMP, 'synthetic-consent', None, STAMP, 1, STAMP, STAMP)
    return replace(value, **changes)


def report(**changes):
    value = ReportPart(700, 1, 'Inwestycje do sprawdzenia',
                       'Dane z rejestru GUNB.\n\nSprawa A/1: budowa domu.\nStatus rejestrowy, sprawdź etap.',
                       (LeadRef('A/1', STAMP),))
    return replace(value, **changes)


def accepted(**changes):
    value = {'messaging_product': 'whatsapp', 'contacts': [{'input': PHONE, 'wa_id': PHONE}],
             'messages': [{'id': 'wamid.synthetic_1='}]}
    value.update(changes)
    return value


class Response:
    def __init__(self, data=None, *, status=200, headers=None, raw=None, close_error=False):
        self.status_code = status
        self.headers = headers or {'Content-Type': 'application/json'}
        self.raw = raw if raw is not None else json.dumps(accepted() if data is None else data).encode()
        self.closed = False
        self.close_error = close_error
        self.consumed = 0

    def iter_content(self, chunk_size):
        for i in range(0, len(self.raw), chunk_size):
            chunk = self.raw[i:i + chunk_size]
            self.consumed += len(chunk)
            yield chunk

    def close(self):
        self.closed = True
        if self.close_error:
            raise RuntimeError('synthetic provider secret in cleanup')


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.adapters = {}
        self.closed = False
        self.trust_env = True

    def mount(self, prefix, adapter):
        self.adapters[prefix] = adapter

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


def sender(monkeypatch, response=None):
    from gunb_tool.whatsapp_sender import WhatsAppSender
    transport = Transport(Response() if response is None else response)
    monkeypatch.setattr(requests, 'Session', lambda: transport)
    return WhatsAppSender(settings()), transport


def test_template_request_is_fixed_tls_scoped_and_contains_complete_snapshot(monkeypatch):
    adapter, transport = sender(monkeypatch)
    result = adapter.send(endpoint(), report(), idempotency_key=KEY)
    assert result.outcome == 'accepted' and result.provider_message_id == 'wamid.synthetic_1='
    assert len(transport.calls) == 1
    url, request = transport.calls[0]
    assert url == 'https://graph.facebook.com/v24.0/123456/messages'
    assert request['verify'] is True and request['allow_redirects'] is False and request['stream'] is True
    assert request['timeout'] == (2, 2) and transport.trust_env is False
    assert transport.adapters['https://'].max_retries.total == 0
    assert request['headers']['Authorization'] == 'Bearer synthetic-secret'
    assert json.loads(request['data']) == {
        'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': PHONE, 'type': 'template',
        'template': {'name': 'gunb_report', 'language': {'code': 'pl'}, 'components': [
            {'type': 'body', 'parameters': [
                {'type': 'text', 'text': 'Inwestycje do sprawdzenia'},
                {'type': 'text', 'text': 'Dane z rejestru GUNB. | Sprawa A/1: budowa domu. | Status rejestrowy, sprawdź etap.'},
            ]},
        ]},
    }
    assert transport.closed and transport.response.closed


@pytest.mark.parametrize('field,value', [
    ('api_version', 'https://attacker.example'), ('api_version', 'v24.0/../../other'),
    ('api_version', 'v0.0'), ('phone_number_id', '0123'), ('phone_number_id', '1?token=x'),
    ('phone_number_id', 123), ('phone_number_id', '1' * 33),
    ('access_token', ''), ('access_token', 'secret\r\nX-Other: bad'), ('access_token', 'é'),
    ('template_name', 'UPPER'), ('template_name', 'a/b'), ('template_name', 'a' * 513),
    ('language', 'pl\n'), ('language', 'pl-PL'), ('timeout_seconds', True),
    ('timeout_seconds', float('nan')), ('timeout_seconds', 0), ('timeout_seconds', 11),
])
def test_invalid_settings_raise_generic_error_without_echoing_value(field, value):
    with pytest.raises(ValueError) as caught:
        settings(**{field: value})
    assert str(caught.value) == 'Niepoprawna konfiguracja WhatsApp'
    assert caught.value.__suppress_context__


@pytest.mark.parametrize('changes', [
    {'channel': 'email'}, {'enabled': False}, {'verified_at': None}, {'consent_at': None},
    {'consent_revoked_at': STAMP}, {'version': 2}, {'chat_id': 701}, {'address': PHONE},
])
def test_invalid_or_unconsented_endpoint_never_calls_http(monkeypatch, changes):
    adapter, transport = sender(monkeypatch)
    assert adapter.send(endpoint(**changes), report(), idempotency_key=KEY).outcome == 'failed'
    assert not transport.calls


@pytest.mark.parametrize('body', ['x' * 900, 'a\x00b', 'a\u202eb'])
def test_large_or_controlled_text_is_rejected_before_http(monkeypatch, body):
    adapter, transport = sender(monkeypatch)
    assert adapter.send(endpoint(), report(body=body), idempotency_key=KEY).outcome == 'failed'
    assert not transport.calls


@pytest.mark.parametrize('key', ['', 'x' * 257, 'secret\nheader'])
def test_invalid_provider_key_never_calls_http(monkeypatch, key):
    adapter, transport = sender(monkeypatch)
    assert adapter.send(endpoint(), report(), idempotency_key=key).outcome == 'failed'
    assert not transport.calls


@pytest.mark.parametrize('status,data,want', [
    (400, {'error': {'code': 100, 'message': 'private provider detail'}}, 'failed'),
    (401, {'error': {'code': 190}}, 'failed'), (403, {'error': {'code': 10}}, 'failed'),
    (404, {'error': {'code': 100}}, 'failed'), (429, {'error': {'code': 130429}}, 'retry'),
    (429, {'error': {'code': True}}, 'unknown'), (429, {}, 'unknown'),
    (429, accepted(error={'code': 130429}), 'unknown'),
    (408, {'error': {'code': 1}}, 'unknown'), (409, {'error': {'code': 1}}, 'unknown'),
    (500, {'error': {'code': 1}}, 'unknown'), (503, {'error': {'code': 2}}, 'unknown'),
    (302, {}, 'unknown'), (201, accepted(), 'unknown'),
    (200, accepted(contacts=[{'wa_id': '48999888777'}]), 'unknown'),
    (200, accepted(messages=[]), 'unknown'), (200, accepted(messages=[{'id': 'secret\n'}]), 'unknown'),
    (200, accepted(messages=[{'id': 'wamid.a'}, {'id': 'wamid.b'}]), 'unknown'),
    (200, accepted(error={'code': 1}), 'unknown'), (200, accepted(messaging_product='other'), 'unknown'),
])
def test_http_result_is_conservative_and_never_retries_in_transport(monkeypatch, status, data, want):
    response = Response(data, status=status, headers={'Retry-After': '180'})
    adapter, transport = sender(monkeypatch, response)
    result = adapter.send(endpoint(), report(), idempotency_key=KEY)
    assert result.outcome == want and len(transport.calls) == 1
    assert result.retry_after_seconds == (180 if want == 'retry' else None)
    assert result.provider_message_id is None
    assert transport.closed and response.closed


@pytest.mark.parametrize('value,want', [('90000', 86400), ('0', 0), ('bad', None), ('-1', None), ('1.5', None)])
def test_retry_after_is_bounded_and_not_interpreted_as_provider_text(monkeypatch, value, want):
    adapter, _ = sender(monkeypatch, Response({'error': {'code': 130429}}, status=429,
                                            headers={'Retry-After': value}))
    result = adapter.send(endpoint(), report(), idempotency_key=KEY)
    assert result.outcome == 'retry' and result.retry_after_seconds == want


@pytest.mark.parametrize('raw', [b'{', b'\xff', b'{"error":NaN}', b'{"error":{},"error":{}}',
                                 b'[' * 25 + b'0' + b']' * 25, b' ' * 17000],
                         ids=['syntax', 'utf8', 'nan', 'duplicate', 'depth', 'size'])
def test_invalid_or_oversize_response_is_unknown_and_closed(monkeypatch, raw):
    response = Response(raw=raw)
    adapter, transport = sender(monkeypatch, response)
    assert adapter.send(endpoint(), report(), idempotency_key=KEY).outcome == 'unknown'
    assert transport.closed and response.closed
    assert response.consumed <= 17408


@pytest.mark.parametrize('error', [requests.Timeout('private token'), requests.ConnectionError('private number'),
                                   requests.exceptions.SSLError('private body'), RuntimeError('private provider')])
def test_network_uncertainty_is_unknown_with_no_logs_or_raw_error(monkeypatch, caplog, error):
    adapter, transport = sender(monkeypatch, error)
    result = adapter.send(endpoint(), report(), idempotency_key=KEY)
    assert result.outcome == 'unknown' and transport.closed and len(transport.calls) == 1
    assert caplog.text == '' and 'private' not in repr(result)
    assert 'synthetic-secret' not in repr(settings()) and '123456' not in repr(settings())


def test_cleanup_does_not_replace_received_acceptance(monkeypatch):
    adapter, transport = sender(monkeypatch, Response(close_error=True))
    def fail_close():
        raise RuntimeError('private session cleanup')
    transport.close = fail_close
    result = adapter.send(endpoint(), report(), idempotency_key=KEY)
    assert result.outcome == 'accepted' and result.provider_message_id == 'wamid.synthetic_1='


def test_cooperative_response_deadline_is_checked_between_chunks(monkeypatch):
    import gunb_tool.whatsapp_sender as module
    adapter, _ = sender(monkeypatch)
    values = iter([0, 0, 31])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(values))
    assert adapter.send(endpoint(), report(), idempotency_key=KEY).outcome == 'unknown'


def test_unicode_parameters_at_capacity_are_sent_without_truncation(monkeypatch):
    adapter, transport = sender(monkeypatch)
    part = report(title='T', body='ą' * 899)
    assert adapter.send(endpoint(), part, idempotency_key=KEY).outcome == 'accepted'
    params = json.loads(transport.calls[0][1]['data'])['template']['components'][0]['parameters']
    assert params == [{'type': 'text', 'text': 'T'}, {'type': 'text', 'text': 'ą' * 899}]


def test_compressed_response_is_not_decompressed_or_consumed(monkeypatch):
    response = Response(headers={'Content-Encoding': 'gzip'}, raw=b'fake compressed bytes')
    adapter, _ = sender(monkeypatch, response)
    assert adapter.send(endpoint(), report(), idempotency_key=KEY).outcome == 'unknown'
    assert response.consumed == 0 and response.closed


@pytest.mark.parametrize('outcome', ['accepted', 'unknown', 'retry'])
def test_real_worker_persists_result_and_isolates_channels_without_http_transaction(
        monkeypatch, notification_store, clock, outcome):
    from datetime import timedelta
    from gunb_tool.notification_worker import NotificationWorker
    store = notification_store
    wa = ready(store, 'whatsapp', '+' + PHONE)
    email = ready(store)
    original, = stored_report(store, wa)
    part = replace(original, body='Dane z rejestru GUNB. Inwestycja A/1 do sprawdzenia. Etap robót wymaga sprawdzenia.')
    ident, = store.enqueue(wa.chat_id, wa.id, 'meta:synthetic', (part,),
                          expires_at=store.repo.now() + timedelta(hours=1))
    response = (Response() if outcome == 'accepted' else requests.Timeout('synthetic-private')
                if outcome == 'unknown' else Response({'error': {'code': 130429}}, status=429))
    adapter, transport = sender(monkeypatch, response)
    post = transport.post
    def checked_post(url, **kwargs):
        assert not store.repo.connection.in_transaction
        return post(url, **kwargs)
    transport.post = checked_post
    worker = NotificationWorker(store, {'whatsapp': adapter})
    assert worker.run_once() == 1
    row = store.repo.connection.execute('SELECT * FROM notification_outbox WHERE id=?', (ident,)).fetchone()
    assert row['state'] == outcome
    assert bool(store.get_endpoint(wa.chat_id, wa.id).enabled) == (outcome != 'unknown')
    assert store.get_endpoint(email.chat_id, email.id).enabled
    history = store.repo.connection.execute('SELECT * FROM notification_deliveries').fetchall()
    assert len(history) == (1 if outcome == 'accepted' else 0)
    assert store.repo.connection.execute('SELECT COUNT(*) FROM deliveries').fetchone()[0] == 0
    if outcome == 'accepted':
        assert row['provider_message_id'] == 'wamid.synthetic_1='
        assert history[0]['endpoint_id'] == wa.id and history[0]['outbox_id'] == ident
        assert history[0]['outcome'] == 'accepted'
    if outcome == 'unknown':
        assert worker.run_once() == 0 and len(transport.calls) == 1
    if outcome == 'retry':
        immutable = row['payload']
        transport.response = Response()
        clock.advance(seconds=80)
        assert worker.run_once() == 1
        after = store.repo.connection.execute('SELECT * FROM notification_outbox WHERE id=?', (ident,)).fetchone()
        assert after['state'] == 'accepted' and after['payload'] == immutable and after['attempts'] == 2
        assert transport.calls[0][1]['data'] == transport.calls[1][1]['data']
