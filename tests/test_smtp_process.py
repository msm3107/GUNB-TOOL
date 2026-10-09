import json

import pytest

from gunb_tool.email_sender import SMTPSettings


SETTINGS = SMTPSettings('smtp.example.org', 587, 'reports@example.org')


class Pipe:
    def __init__(self, reply):
        self.reply, self.closed = reply, False

    def poll(self, timeout):
        assert 0 <= timeout <= .2
        return self.reply is not None

    def recv_bytes(self, maxlength):
        assert maxlength == 2048
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def close(self):
        self.closed = True


class Process:
    def __init__(self, fail_start=False, fail_cleanup=False):
        self.fail_start, self.fail_cleanup = fail_start, fail_cleanup
        self.calls, self.live = [], False

    def start(self):
        self.calls.append('start')
        if self.fail_start:
            raise OSError('private input')
        self.live = True

    def is_alive(self):
        return self.live

    def terminate(self):
        self.calls.append('terminate')
        if self.fail_cleanup:
            raise OSError('cleanup')
        self.live = False

    def kill(self):
        self.calls.append('kill')
        self.live = False

    def join(self, timeout):
        assert timeout <= 1
        self.calls.append('join')

    def close(self):
        self.calls.append('close')


class Context:
    def __init__(self, reply=None, **options):
        self.read, self.write, self.process = Pipe(reply), Pipe(None), Process(**options)

    def Pipe(self, *, duplex):
        assert not duplex
        return self.read, self.write

    def Process(self, **kwargs):
        assert kwargs['daemon'] is True
        self.args = kwargs['args']
        return self.process


def supervisor(context, monkeypatch, **options):
    from gunb_tool import smtp_process
    ticks = iter(i * .1 for i in range(10000))
    monkeypatch.setattr(smtp_process.time, 'monotonic', lambda: next(ticks))
    return smtp_process.SupervisedSMTP(SETTINGS, _context=context, timeout_seconds=.5, **options)


@pytest.mark.parametrize('outcome', ['accepted', 'retry', 'failed', 'unknown'])
def test_valid_results_and_cleanup(monkeypatch, outcome):
    context = Context(json.dumps(dict(outcome=outcome, provider_message_id=None, retry_after_seconds=None)).encode())
    result = supervisor(context, monkeypatch).send(None, None, idempotency_key='test')
    assert result.outcome == outcome
    assert context.read.closed and context.write.closed
    assert 'terminate' in context.process.calls and 'close' in context.process.calls


@pytest.mark.parametrize('reply', [None, b'not JSON', b'{}', b'[]', EOFError('private'), b'{"outcome":"other"}'])
def test_ambiguous_reply_or_deadline_never_retried(monkeypatch, reply):
    context = Context(reply)
    assert supervisor(context, monkeypatch).send(None, None, idempotency_key='test').outcome == 'unknown'
    assert not context.process.live


def test_ack_survives_cleanup_failure(monkeypatch):
    context = Context(b'{"outcome":"accepted","provider_message_id":null,"retry_after_seconds":null}', fail_cleanup=True)
    assert supervisor(context, monkeypatch).send(None, None, idempotency_key='test').outcome == 'accepted'
    assert 'kill' in context.process.calls


def test_stop_before_start_is_safe_retry(monkeypatch):
    context = Context()
    assert supervisor(context, monkeypatch, should_stop=lambda: True).send(None, None, idempotency_key='test').outcome == 'retry'
    assert 'start' not in context.process.calls


def test_stop_after_start_is_unknown(monkeypatch):
    context = Context()
    checks = iter([False, True])
    assert supervisor(context, monkeypatch, should_stop=lambda: next(checks)).send(None, None, idempotency_key='test').outcome == 'unknown'
    assert 'terminate' in context.process.calls


def test_start_failure_is_generic(monkeypatch, caplog):
    context = Context(fail_start=True)
    assert supervisor(context, monkeypatch).send(None, None, idempotency_key='private').outcome == 'unknown'
    assert 'private' not in caplog.text


def test_real_spawn_rejects_invalid_recipient_without_network():
    from gunb_tool.smtp_process import SupervisedSMTP
    assert SupervisedSMTP(SETTINGS, timeout_seconds=10).send(None, None, idempotency_key='test').outcome == 'failed'
