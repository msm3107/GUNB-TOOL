"""Bounded process/IPC boundary using synthetic data, never real Meta HTTP."""

from datetime import timedelta
import json
import multiprocessing
import time

import pytest

from gunb_tool.notification_models import DeliveryResult
from tests.test_notification_store import notification_store, ready, report as stored_report
from tests.test_whatsapp_sender import KEY, endpoint, report, settings
from tests.whatsapp_process_helpers import ACK, ack_child, stalled_writer


def supervisor(**options):
    from gunb_tool.whatsapp_process import SupervisedWhatsApp
    return SupervisedWhatsApp(settings(), **options)


class ControlledPipe:
    def __init__(self, context, reply=None):
        self.context, self.reply, self.closed = context, reply, False
        self.reads = 0

    def poll(self, timeout):
        return self.reply is not None

    def recv_bytes(self, maxlength):
        # A readable pipe with a live partial writer must never enter blocking recv.
        assert not self.context.process.live
        assert maxlength == 2048
        self.reads += 1
        if isinstance(self.reply, BaseException):
            raise self.reply
        if len(self.reply) > maxlength:
            raise OSError('private oversized response')
        return self.reply

    def close(self):
        self.closed = True


class ControlledProcess:
    def __init__(self, context, *, stalled=False, fail_start=False, fail_terminate=False,
                 fail_close=False, crash=False):
        self.context, self.live, self.started = context, False, False
        self.stalled, self.fail_start, self.fail_terminate = stalled, fail_start, fail_terminate
        self.fail_close, self.crash, self.closed = fail_close, crash, False
        self.terminated = self.killed = False

    def start(self):
        self.started = True
        if self.fail_start:
            raise OSError('private start token')
        self.live = True

    def is_alive(self):
        return self.live

    def join(self, timeout):
        assert 0 <= timeout <= 1
        if self.live and not self.stalled:
            self.live = False
            if self.crash:
                self.context.reader.reply = None
        self.context.elapsed += timeout

    def terminate(self):
        self.terminated = True
        if self.fail_terminate:
            raise OSError('private terminate token')
        self.live = False

    def kill(self):
        self.killed, self.live = True, False

    def close(self):
        self.closed = True
        if self.fail_close:
            raise OSError('private close token')


class ControlledContext:
    def __init__(self, reply=ACK, **faults):
        self.elapsed, self.starts, self.args = 0, 0, None
        self.reader, self.writer = ControlledPipe(self, reply), ControlledPipe(self)
        self.process = ControlledProcess(self, **faults)

    def Pipe(self, *, duplex):
        assert duplex is False
        return self.reader, self.writer

    def Process(self, *, target, args, daemon):
        assert daemon is True
        self.starts += 1
        self.args = args
        return self.process


def controlled(monkeypatch, context, **options):
    from gunb_tool import whatsapp_process
    monkeypatch.setattr(whatsapp_process.time, 'monotonic', lambda: context.elapsed)
    return supervisor(_context=context, timeout_seconds=.5, **options)


@pytest.mark.parametrize('raw,want', [
    (ACK, DeliveryResult('accepted', 'wamid.synthetic_1=')),
    (b'{"outcome":"retry","provider_message_id":null,"retry_after_seconds":17}',
     DeliveryResult('retry', retry_after_seconds=17)),
    (b'{"outcome":"failed","provider_message_id":null,"retry_after_seconds":null}', DeliveryResult('failed')),
    (b'{"outcome":"unknown","provider_message_id":null,"retry_after_seconds":null}', DeliveryResult('unknown')),
])
def test_completed_child_preserves_only_valid_delivery_result(monkeypatch, raw, want):
    context = ControlledContext(raw)
    result = controlled(monkeypatch, context).send(endpoint(), report(), idempotency_key=KEY)
    assert result == want
    assert context.reader.closed and context.writer.closed and context.process.closed
    # Only DTOs and settings cross into the child, not a repository or connection.
    assert context.args[1:] == (settings(), endpoint(), report(), KEY)


@pytest.mark.parametrize('raw', [
    None, EOFError('private payload'), b'not JSON', b'\xff', b'[]', b'{}', b'x' * 2049,
    ACK[:-1] + b',"outcome":"retry"}',
    ACK[:-1] + b',"extra":"private body"}',
    ACK.replace(b'accepted', b'delivered'), ACK.replace(b'accepted', b'read'),
    ACK.replace(b'"wamid.synthetic_1="', b'null'),
    ACK.replace(b'"wamid.synthetic_1="', b'"private token"'),
    ACK.replace(b'"accepted"', b'"failed"'),
    ACK.replace(b'"accepted"', b'"retry"'),
    ACK.replace(b'"retry_after_seconds":null', b'"retry_after_seconds":17'),
    b'{"outcome":"retry","provider_message_id":null,"retry_after_seconds":true}',
    b'{"outcome":"retry","provider_message_id":null,"retry_after_seconds":86401}',
    b'{"outcome":"retry","provider_message_id":null,"retry_after_seconds":-1}',
    b'{"outcome":"retry","provider_message_id":null,"retry_after_seconds":NaN}',
    b'{"outcome":"retry","provider_message_id":null,"retry_after_seconds":1.5}',
    b'[' * 21 + b'0' + b']' * 21,
])
def test_missing_or_untrusted_ipc_is_unknown_never_retry(monkeypatch, raw, caplog):
    context = ControlledContext(raw)
    assert controlled(monkeypatch, context).send(endpoint(), report(), idempotency_key=KEY) == DeliveryResult('unknown')
    assert context.reader.closed and context.writer.closed and context.process.closed
    assert 'private' not in caplog.text and 'synthetic-secret' not in caplog.text


def test_readable_live_writer_times_out_without_blocking_recv(monkeypatch):
    context = ControlledContext(ACK, stalled=True)
    assert controlled(monkeypatch, context).send(endpoint(), report(), idempotency_key=KEY).outcome == 'unknown'
    assert context.elapsed < 3
    assert context.reader.reads == 0
    assert context.process.terminated and not context.process.live


def test_stop_before_start_is_retry_without_allocating_process(monkeypatch):
    context = ControlledContext()
    assert controlled(monkeypatch, context, should_stop=lambda: True).send(
        endpoint(), report(), idempotency_key=KEY).outcome == 'retry'
    assert context.starts == 0


def test_stop_after_start_is_unknown_and_child_terminated(monkeypatch):
    context = ControlledContext(stalled=True)
    checks = iter([False, False, True])
    result = controlled(monkeypatch, context, should_stop=lambda: next(checks)).send(
        endpoint(), report(), idempotency_key=KEY)
    assert result.outcome == 'unknown' and context.process.terminated
    assert context.reader.closed and context.writer.closed


@pytest.mark.parametrize('fault', ['fail_start', 'crash'])
def test_start_error_or_crash_never_retries_and_hides_private_details(monkeypatch, fault, caplog):
    context = ControlledContext(**{fault: True})
    assert controlled(monkeypatch, context).send(endpoint(), report(), idempotency_key=KEY).outcome == 'unknown'
    assert context.reader.closed and context.writer.closed
    assert 'private' not in caplog.text


def test_kill_fallback_stops_stalled_child(monkeypatch):
    context = ControlledContext(stalled=True, fail_terminate=True)
    assert controlled(monkeypatch, context).send(endpoint(), report(), idempotency_key=KEY).outcome == 'unknown'
    assert context.process.killed and not context.process.live


def test_valid_ack_survives_process_handle_cleanup_error(monkeypatch):
    context = ControlledContext(fail_close=True)
    assert controlled(monkeypatch, context).send(endpoint(), report(), idempotency_key=KEY) == DeliveryResult(
        'accepted', 'wamid.synthetic_1=')
    assert context.reader.closed and context.writer.closed


@pytest.mark.parametrize('budget', [True, None, '45', 0, -1, 46, pytest.param(10**1000, id='huge-int'),
                                    float('nan'), float('inf')])
def test_invalid_budget_raises_generic_error(budget):
    with pytest.raises(ValueError, match='^Niepoprawny nadzór WhatsApp$'):
        supervisor(timeout_seconds=budget)


def test_invalid_settings_or_stop_raises_generic_error():
    from gunb_tool.whatsapp_process import SupervisedWhatsApp
    for options in ({'settings': 'private token'}, {'settings': settings(), 'should_stop': True}):
        with pytest.raises(ValueError, match='^Niepoprawny nadzór WhatsApp$'):
            SupervisedWhatsApp(**options)


def test_deadline_during_preparation_does_not_start_child(monkeypatch):
    class SlowPreparation(ControlledContext):
        def Pipe(self, **kwargs):
            result = super().Pipe(**kwargs)
            self.elapsed = 1
            return result

    context = SlowPreparation()
    assert controlled(monkeypatch, context).send(endpoint(), report(), idempotency_key=KEY).outcome == 'retry'
    assert not context.process.started and context.reader.closed and context.writer.closed


def test_stop_during_preparation_does_not_start_child(monkeypatch):
    context = ControlledContext()
    checks = iter([False, True])
    assert controlled(monkeypatch, context, should_stop=lambda: next(checks)).send(
        endpoint(), report(), idempotency_key=KEY).outcome == 'retry'
    assert not context.process.started and context.reader.closed and context.writer.closed


def test_child_calls_adapter_once_and_sends_only_compact_result(monkeypatch):
    from gunb_tool import whatsapp_process
    from gunb_tool.whatsapp_sender import WhatsAppSender
    received, messages = [], []

    def send(self, target, part, *, idempotency_key):
        received.append((self.settings, target, part, idempotency_key))
        return DeliveryResult('accepted', 'wamid.synthetic_1=')

    class Writer:
        closed = False

        def send_bytes(self, raw):
            messages.append(raw)

        def close(self):
            self.closed = True

    monkeypatch.setattr(WhatsAppSender, 'send', send)
    writer = Writer()
    whatsapp_process._whatsapp_child(writer, settings(), endpoint(), report(), KEY)
    assert received == [(settings(), endpoint(), report(), KEY)]
    assert len(messages) == 1 and json.loads(messages[0]) == json.loads(ACK) and writer.closed
    assert 'synthetic-secret' not in messages[0].decode() and report().body not in messages[0].decode()


@pytest.mark.parametrize('failure', [SystemExit('private token'), RuntimeError('private body'),
                                     DeliveryResult('delivered', 'wamid.synthetic_1='), object()])
def test_child_errors_or_invalid_adapter_results_are_private_unknown(monkeypatch, failure, capsys):
    from gunb_tool import whatsapp_process
    from gunb_tool.whatsapp_sender import WhatsAppSender
    messages = []

    def send(*args, **kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure

    class Writer:
        def send_bytes(self, raw):
            messages.append(raw)

        def close(self):
            pass

    monkeypatch.setattr(WhatsAppSender, 'send', send)
    whatsapp_process._whatsapp_child(Writer(), settings(), endpoint(), report(), KEY)
    assert json.loads(messages[0]) == dict(outcome='unknown', provider_message_id=None, retry_after_seconds=None)
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


class SpawnContext:
    def __init__(self, target=None):
        self.context = multiprocessing.get_context('spawn')
        self.target, self.processes = target, []

    def Pipe(self, **kwargs):
        return self.context.Pipe(**kwargs)

    def Process(self, *, target, **kwargs):
        process = self.context.Process(target=self.target or target, **kwargs)
        self.processes.append(process)
        return process


def test_real_spawn_transports_ack_and_uses_new_closed_process_per_operation():
    context = SpawnContext(ack_child)
    sender = supervisor(_context=context, timeout_seconds=10)
    for _ in range(2):
        assert sender.send(endpoint(), report(), idempotency_key=KEY) == DeliveryResult('accepted', 'wamid.synthetic_1=')
    assert len(context.processes) == 2 and context.processes[0] is not context.processes[1]
    for process in context.processes:
        with pytest.raises(ValueError):
            process.is_alive()  # Dead process handles were closed by production cleanup.


def test_real_spawn_invalid_recipient_rejected_without_network():
    assert supervisor(timeout_seconds=10).send(None, None, idempotency_key=KEY).outcome == 'failed'


def test_real_stalled_writer_returns_unknown_within_budget_and_is_reaped():
    context = SpawnContext(stalled_writer)
    before = time.monotonic()
    assert supervisor(_context=context, timeout_seconds=2).send(endpoint(), report(), idempotency_key=KEY).outcome == 'unknown'
    assert time.monotonic() - before < 8  # Broad scheduling margin, not a real-time OS promise.
    with pytest.raises(ValueError):
        context.processes[0].is_alive()


@pytest.mark.parametrize('raw,state,history,enabled', [(ACK, 'accepted', 1, 1), (None, 'unknown', 0, 0)])
def test_worker_commits_route_before_process_and_does_not_resend_unknown(
        notification_store, clock, monkeypatch, raw, state, history, enabled):
    from gunb_tool.notification_worker import NotificationWorker
    store = notification_store
    target = ready(store, channel='whatsapp', address='+48111222333')
    parts = stored_report(store, target)
    store.enqueue(target.chat_id, target.id, 'synthetic', parts, expires_at=clock.now_utc() + timedelta(hours=1))

    class AfterCommit(ControlledContext):
        def Process(self, **kwargs):
            assert not store.repo.connection.in_transaction
            route = store.repo.connection.execute('SELECT * FROM whatsapp_dispatch_routes').fetchone()
            assert route is not None and route['chat_id'] == target.chat_id
            return super().Process(**kwargs)

    context = AfterCommit(raw)
    worker = NotificationWorker(store, {'whatsapp': controlled(monkeypatch, context)})
    assert worker.run_once() == 1
    assert store.repo.connection.execute('SELECT state FROM notification_outbox').fetchone()[0] == state
    assert store.repo.connection.execute('SELECT count(*) FROM notification_deliveries').fetchone()[0] == history
    assert store.get_endpoint(target.chat_id, target.id).enabled == bool(enabled)
    assert worker.run_once() == 0 and context.starts == 1
