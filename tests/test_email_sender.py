"""SMTP protocol fakes: no socket connection or real mailbox."""

from dataclasses import replace
from datetime import timedelta
from email import policy
from email.parser import BytesParser
import smtplib
import ssl

import pytest

from gunb_tool import email_sender
from gunb_tool.email_sender import SMTPEmailSender, SMTPSettings
from gunb_tool.notification_worker import NotificationWorker
from tests.bot_helpers import MIETEK
from tests.test_notification_store import enqueue, notification_store, ready, report


class FakeSMTP:
    def __init__(self):
        self.calls = []
        self.errors = {}
        self.replies = {}
        self.sock = self
        self.payload = None
        self.context = None

    def op(self, name, *args):
        self.calls.append((name, args))
        if name in self.errors:
            raise self.errors[name]
        return self.replies.get(name, (250, b'OK'))

    def settimeout(self, seconds):
        assert 0 < seconds <= 10

    def set_debuglevel(self, level):
        self.op('debug', level)

    def ehlo(self):
        return self.op('ehlo')

    def starttls(self, *, context):
        self.context = context
        self.op('starttls')
        return (220, b'Go ahead')

    def login(self, user, password):
        return self.op('login', user, password)

    def mail(self, address):
        return self.op('mail', address)

    def rcpt(self, address):
        return self.op('rcpt', address)

    def data(self, payload):
        self.payload = payload
        return self.op('data')

    def close(self):
        self.op('close')


@pytest.fixture
def transport(monkeypatch):
    fake = FakeSMTP()
    def connect(*args, **kwargs):
        fake.op('connect', args, kwargs)
        if 'context' in kwargs:
            fake.context = kwargs['context']
        return fake
    monkeypatch.setattr(email_sender.smtplib, 'SMTP', connect)
    monkeypatch.setattr(email_sender.smtplib, 'SMTP_SSL', connect)
    return fake


def sender(**options):
    return SMTPEmailSender(SMTPSettings('smtp.example.test', 587, 'alerts@example.test', **options))


@pytest.mark.parametrize('tls', ['starttls', 'implicit'])
def test_tls_precedes_auth_and_single_recipient_mime(notification_store, transport, tls):
    endpoint = ready(notification_store)
    part, = report(notification_store, endpoint)
    result = sender(tls=tls, username='operator', password='FAKE_PASSWORD').send(endpoint, part, idempotency_key='job-1')
    assert result.outcome == 'accepted'
    names = [name for name, _ in transport.calls]
    assert names.index('debug') < names.index('login') < names.index('mail') < names.index('rcpt') < names.index('data')
    if tls == 'starttls':
        assert names.index('starttls') < names.index('login')
        assert names.count('ehlo') == 2
    else:
        assert 'starttls' not in names
    assert transport.context.check_hostname and transport.context.verify_mode == ssl.CERT_REQUIRED
    assert [args for name, args in transport.calls if name == 'rcpt'] == [(endpoint.address,)]
    message = BytesParser(policy=policy.default).parsebytes(transport.payload)
    assert str(message['To']) == endpoint.address and str(message['From']) == 'alerts@example.test'
    assert message['Cc'] is None and message['Bcc'] is None
    assert str(message['Subject']) == part.title
    assert message.get_content().replace('\r\n', '\n').rstrip() == part.body.rstrip()
    assert result.provider_message_id == str(message['Message-ID'])
    first_id = message['Message-ID']
    sender(tls=tls).send(endpoint, part, idempotency_key='job-1')
    assert BytesParser(policy=policy.default).parsebytes(transport.payload)['Message-ID'] == first_id


@pytest.mark.parametrize('stage,code,outcome', [
    ('ehlo', 450, 'retry'), ('ehlo', 550, 'failed'),
    ('mail', 451, 'retry'), ('mail', 550, 'failed'),
    ('rcpt', 450, 'retry'), ('rcpt', 550, 'failed'),
    ('data', 451, 'retry'), ('data', 550, 'failed'), ('data', 354, 'unknown'),
])
def test_explicit_smtp_reply_classification(notification_store, transport, stage, code, outcome):
    endpoint = ready(notification_store)
    transport.replies[stage] = (code, b'PRIVATE_REMOTE_TEXT')
    assert sender().send(endpoint, report(notification_store, endpoint)[0], idempotency_key='job').outcome == outcome
    if stage != 'data':
        assert transport.payload is None
    assert transport.calls[-1][0] == 'close'


@pytest.mark.parametrize('stage,error,outcome', [
    ('connect', TimeoutError('SECRET'), 'retry'),
    ('starttls', smtplib.SMTPNotSupportedError('SECRET'), 'failed'),
    ('starttls', ssl.SSLCertVerificationError('SECRET'), 'failed'),
    ('login', smtplib.SMTPAuthenticationError(535, b'SECRET'), 'failed'),
    ('login', smtplib.SMTPAuthenticationError(454, b'SECRET'), 'retry'),
    ('mail', smtplib.SMTPServerDisconnected('SECRET'), 'retry'),
    ('rcpt', TimeoutError('SECRET'), 'retry'),
    ('data', TimeoutError('SECRET'), 'unknown'),
    ('data', smtplib.SMTPServerDisconnected('SECRET'), 'unknown'),
    ('data', smtplib.SMTPDataError(450, b'SECRET'), 'retry'),
    ('data', smtplib.SMTPDataError(550, b'SECRET'), 'failed'),
])
def test_transport_exceptions_never_leak_and_only_data_is_ambiguous(notification_store, transport, caplog, stage, error, outcome):
    endpoint = ready(notification_store)
    transport.errors[stage] = error
    result = sender(username='FAKE_USER', password='FAKE_PASSWORD').send(
        endpoint, report(notification_store, endpoint)[0], idempotency_key='job')
    assert result.outcome == outcome and 'SECRET' not in caplog.text and 'SECRET' not in repr(result)
    if stage != 'data':
        assert transport.payload is None


def test_cleanup_failure_cannot_erase_acceptance(notification_store, transport):
    endpoint = ready(notification_store)
    transport.errors['close'] = OSError('SECRET')
    assert sender().send(endpoint, report(notification_store, endpoint)[0], idempotency_key='job').outcome == 'accepted'


@pytest.mark.parametrize('options', [dict(tls='plain'), dict(host='smtp.test\r\nX: y'),
    dict(host='https://smtp.test'), dict(port=True), dict(port=0), dict(timeout_seconds=True),
    dict(timeout_seconds=0), dict(timeout_seconds=11), dict(timeout_seconds=float('nan')),
    dict(from_address='good@example.test\r\nBcc: bad@example.test'), dict(username='alone')])
def test_settings_reject_unsafe_input(options):
    values = dict(host='smtp.example.test', port=587, from_address='alerts@example.test')
    values.update(options)
    with pytest.raises(ValueError):
        SMTPSettings(**values)


def test_settings_repr_hides_credentials_and_sender():
    value = SMTPSettings('smtp.example.test', 587, 'alerts@example.test', username='FAKE_USER', password='FAKE_PASSWORD')
    assert all(text not in repr(value) for text in ('FAKE_USER', 'FAKE_PASSWORD', 'alerts@example.test'))


def test_adapter_rejects_wrong_owner_channel_version_and_address(notification_store, transport):
    endpoint = ready(notification_store)
    part, = report(notification_store, endpoint)
    for changed in (replace(endpoint, chat_id=123), replace(endpoint, version=99), replace(endpoint, channel='whatsapp'),
                    replace(endpoint, address='a@example.test\r\nBcc: bad@example.test'), replace(endpoint, enabled=False)):
        assert sender().send(changed, part, idempotency_key='job').outcome == 'failed'
    assert not transport.calls


def test_session_budget_stops_before_data(notification_store, transport, monkeypatch):
    endpoint = ready(notification_store)
    moment = [0.0]
    monkeypatch.setattr(email_sender.time, 'monotonic', lambda: moment[0])
    original = transport.rcpt
    def delayed_rcpt(address):
        result = original(address)
        moment[0] = 46.0
        return result
    transport.rcpt = delayed_rcpt
    assert sender().send(endpoint, report(notification_store, endpoint)[0], idempotency_key='job').outcome == 'retry'
    assert transport.payload is None


def test_verification_email_is_separate_from_reports(notification_store, transport):
    endpoint = notification_store.add_endpoint(MIETEK, 'email', 'odbiorca@example.test')
    expires = notification_store.repo.now() + timedelta(minutes=15)
    token = 'x' * 43
    assert sender().send_verification(endpoint, token, expires_at=expires, idempotency_key='verify-1').outcome == 'accepted'
    message = BytesParser(policy=policy.default).parsebytes(transport.payload)
    assert token in message.get_content() and 'nie włącza' in message.get_content()
    assert 'inwestycje' not in message.get_content()


def test_real_adapter_protocol_integrates_with_worker(notification_store, transport):
    endpoint = ready(notification_store)
    ident, = enqueue(notification_store, endpoint)
    assert NotificationWorker(notification_store, {'email': sender()}).run_once() == 1
    row = notification_store.repo.connection.execute('SELECT state FROM notification_outbox WHERE id = ?', (ident,)).fetchone()
    assert row[0] == 'accepted'
