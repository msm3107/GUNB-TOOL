"""Authenticated Meta payloads built entirely from synthetic fixture values."""

from dataclasses import FrozenInstanceError
import hashlib
import hmac
import json

import pytest


SECRET = 'synthetic-app-secret'
TOKEN = 'synthetic-verification-token'
BODY = (b'{"object":"whatsapp_business_account","entry":[{"id":"1234","changes":[{"field":"messages",'
        b'"value":{"messaging_product":"whatsapp","metadata":{"phone_number_id":"5678"},"statuses":'
        b'[{"id":"wamid.test1","recipient_id":"48111222333","status":"delivered","timestamp":"1700000000"}]}}]}]}')
SIGNATURE = 'sha256=0dd4d3b2004e088e7d05a262f8b8bce4663e5af3840d7a8f1370f598d6a46bc9'


def settings(**changes):
    from gunb_tool.whatsapp_webhook import WhatsAppWebhookSettings
    args = dict(waba_id='1234', phone_number_id='5678', app_secret=SECRET, verify_token=TOKEN)
    args.update(changes)
    return WhatsAppWebhookSettings(**args)


def receiver():
    from gunb_tool.whatsapp_webhook import WhatsAppWebhook
    return WhatsAppWebhook(settings())


def signature(body, secret=SECRET):
    return 'sha256=' + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def payload():
    return json.loads(BODY)


def value(data):
    return data['entry'][0]['changes'][0]['value']


def parse(data):
    body = json.dumps(data, separators=(',', ':')).encode()
    return receiver().parse_statuses(body, signature(body))


def test_literal_signed_payload_returns_minimal_immutable_status():
    status, = receiver().parse_statuses(BODY, SIGNATURE)
    assert status.provider_message_id == 'wamid.test1'
    assert status.recipient_id == '48111222333'
    assert status.outcome == 'delivered' and status.timestamp == '2023-11-14T22:13:20.000000+00:00'
    assert status.event_key.startswith('meta:') and len(status.event_key) == 69
    assert 'wamid' not in repr(status) and '48111222333' not in repr(status)
    with pytest.raises(FrozenInstanceError):
        status.outcome = 'read'


@pytest.mark.parametrize('header', [None, '', 'sha1=' + '0' * 64, 'sha256=' + '0' * 64,
                                     SIGNATURE + ',' + SIGNATURE, 'sha256=' + 'é' * 64])
def test_authentication_rejects_before_json_parsing(monkeypatch, header):
    import gunb_tool.whatsapp_webhook as module
    def forbidden(*_):
        pytest.fail('Unauthenticated payload reached JSON parser')
    monkeypatch.setattr(module, 'strict_json', forbidden)
    with pytest.raises(module.WebhookError):
        receiver().parse_statuses(BODY, header)


def test_raw_bytes_are_authenticated_and_verify_token_is_not_app_secret():
    from gunb_tool.whatsapp_webhook import WebhookError
    with pytest.raises(WebhookError):
        receiver().parse_statuses(BODY + b' ', SIGNATURE)
    with pytest.raises(WebhookError):
        receiver().parse_statuses(BODY, signature(BODY, TOKEN))
    assert receiver().parse_statuses(BODY + b' ', signature(BODY + b' '))[0].outcome == 'delivered'


def test_challenge_requires_separate_token_and_subscribe_mode():
    from gunb_tool.whatsapp_webhook import WebhookError
    webhook = receiver()
    assert webhook.challenge('subscribe', TOKEN, '123456789') == '123456789'
    for mode, token, challenge in [
        ('subscribe', SECRET, '123'), ('other', TOKEN, '123'), ('subscribe', TOKEN + 'x', '123'),
        ('subscribe', TOKEN, '<script>'), ('subscribe', TOKEN, '1' * 257),
        ('subscribe', TOKEN, ''), ('subscribe', 'é', '123'), ('subscribe', None, '123'),
    ]:
        with pytest.raises(WebhookError) as caught:
            webhook.challenge(mode, token, challenge)
        assert TOKEN not in str(caught.value) and SECRET not in str(caught.value)


@pytest.mark.parametrize('field,bad', [('waba_id', '1/other'), ('phone_number_id', '0123'),
    ('waba_id', 1234), ('app_secret', ''), ('app_secret', 'x\nsecret'),
    ('app_secret', 'x' * 257), ('verify_token', ''), ('verify_token', 'x' * 257),
    ('verify_token', 'é' * 20), ('verify_token', SECRET)])
def test_webhook_settings_fail_closed_with_generic_private_errors(field, bad):
    with pytest.raises(ValueError) as caught:
        settings(**{field: bad})
    assert str(caught.value) == 'Niepoprawna konfiguracja webhooka WhatsApp'
    assert caught.value.__suppress_context__


@pytest.mark.parametrize('kind,want', [('sent', 'accepted'), ('delivered', 'delivered'), ('read', 'read'),
                                     ('failed', 'failed')])
def test_all_known_statuses_are_typed_without_provider_error_or_conversation(kind, want):
    data = payload()
    event = value(data)['statuses'][0]
    event.update(status=kind, errors=[{'message': 'synthetic-private-error'}],
                 conversation={'id': 'private-conversation'}, pricing={'billable': True})
    status, = parse(data)
    assert status.outcome == want
    assert set(vars(status)) == {'provider_message_id', 'recipient_id', 'outcome', 'timestamp', 'event_key'}
    assert 'private' not in repr(status)


@pytest.mark.parametrize('scope', ['waba', 'phone', 'field', 'product', 'future_status', 'incoming'])
def test_unrelated_accounts_fields_and_incoming_messages_do_not_become_statuses(scope):
    data = payload()
    if scope == 'waba':
        data['entry'][0]['id'] = '9999'
    elif scope == 'phone':
        value(data)['metadata']['phone_number_id'] = '9999'
    elif scope == 'field':
        data['entry'][0]['changes'][0]['field'] = 'account_update'
    elif scope == 'product':
        value(data)['messaging_product'] = 'other'
    elif scope == 'future_status':
        value(data)['statuses'][0]['status'] = 'future_status'
    else:
        value(data).pop('statuses')
        value(data)['messages'] = [{'text': {'body': 'STOP'}, 'from': '48111222333'}]
    assert parse(data) == ()


def test_mixed_batch_is_scoped_deduplicated_and_stable_across_replay():
    data = payload()
    entry = data['entry'][0]
    other = json.loads(BODY)['entry'][0]
    other['id'] = '9999'
    data['entry'] = [other, entry]
    event = value(payload())['statuses'][0]
    entry['changes'][0]['value']['statuses'] = [event, dict(event), dict(event, status='read')]
    statuses = parse(data)
    assert [s.outcome for s in statuses] == ['delivered', 'read']
    assert statuses == parse(data)
    assert statuses[0].event_key != statuses[1].event_key


@pytest.mark.parametrize('field,bad', [
    ('id', 'private-invalid-id'), ('id', 'wamid.' + 'x' * 251), ('id', 'wamid.secret\n'),
    ('recipient_id', '+48111222333'), ('recipient_id', '048111222333'), ('recipient_id', '1' * 16),
    ('timestamp', 'bad'), ('timestamp', True), ('timestamp', '-1'), ('timestamp', '999999999999'),
])
def test_invalid_known_status_is_rejected_atomically_without_raw_value(field, bad):
    from gunb_tool.whatsapp_webhook import WebhookError
    data = payload()
    value(data)['statuses'].append(dict(value(data)['statuses'][0], **{field: bad}))
    with pytest.raises(WebhookError) as caught:
        parse(data)
    assert str(caught.value) == 'Niepoprawny webhook WhatsApp' and caught.value.__suppress_context__


@pytest.mark.parametrize('raw', [b'{', b'\xff', b'{"object":NaN}', b'{"object":Infinity}',
    b'{"object":"a","object":"b"}', b'[' * 30 + b'0' + b']' * 30, b' ' * 65537],
    ids=['syntax', 'utf8', 'nan', 'infinity', 'duplicate', 'depth', 'size'])
def test_signed_malformed_and_oversize_data_does_not_expose_content(raw):
    from gunb_tool.whatsapp_webhook import WebhookError
    with pytest.raises(WebhookError) as caught:
        receiver().parse_statuses(raw, signature(raw))
    assert str(caught.value) == 'Niepoprawny webhook WhatsApp'


@pytest.mark.parametrize('mutate', [
    lambda d: d.update(object='page'), lambda d: d.update(entry=None),
    lambda d: d.update(entry=[d['entry'][0]] * 21),
    lambda d: d['entry'][0].update(changes=None),
    lambda d: d['entry'][0].update(changes=d['entry'][0]['changes'] * 21),
    lambda d: value(d).update(statuses=value(d)['statuses'] * 101),
    lambda d: value(d).update(statuses={}), lambda d: value(d).update(metadata=[]),
])
def test_signed_structure_and_count_limits_fail_as_one_payload(mutate):
    from gunb_tool.whatsapp_webhook import WebhookError
    data = payload()
    mutate(data)
    with pytest.raises(WebhookError):
        parse(data)


def test_status_limit_applies_across_changes_not_only_each_list():
    from gunb_tool.whatsapp_webhook import WebhookError
    data = payload()
    change = data['entry'][0]['changes'][0]
    change['value']['statuses'] *= 51
    data['entry'][0]['changes'] = [change, change]
    with pytest.raises(WebhookError):
        parse(data)


def test_secret_settings_and_errors_never_log(caplog):
    config = settings()
    for private in (SECRET, TOKEN, '1234', '5678'):
        assert private not in repr(config)
    receiver().parse_statuses(BODY, SIGNATURE)
    assert caplog.text == ''


def test_json_exponent_overflow_is_not_a_finite_wire_value():
    from gunb_tool.whatsapp_models import strict_json
    with pytest.raises(ValueError):
        strict_json(b'{"unrelated":1e999}', 100)
