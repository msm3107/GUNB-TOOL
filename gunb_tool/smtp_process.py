"""One disposable spawn process per SMTP operation; no database in the child."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
import json
import math
import multiprocessing
import time

from .email_sender import SMTPEmailSender, SMTPSettings
from .notification_models import DeliveryResult


def _smtp_child(pipe, settings, method, endpoint, payload, metadata) -> None:
    try:
        sender = SMTPEmailSender(settings)
        if method == 'report':
            result = sender.send(endpoint, payload, **metadata)
        elif method == 'verification':
            result = sender.send_verification(endpoint, payload, **metadata)
        else:
            result = DeliveryResult('unknown')
    except BaseException:
        # Never print private provider exceptions, credentials or a verification code.
        result = DeliveryResult('unknown')
    try:
        pipe.send_bytes(json.dumps(dict(outcome=result.outcome, provider_message_id=result.provider_message_id,
                                        retry_after_seconds=result.retry_after_seconds)).encode('utf-8'))
    except (OSError, ValueError):
        pass
    finally:
        pipe.close()


class SupervisedSMTP:
    """A bounded wait, then conservative quarantine if SMTP acknowledgement is lost.

    The 45s budget covers child startup and SMTP/DNS; OS process cleanup adds at most
    two 1s joins under normal OS operation. No persistent pipe, queue or lock is shared.
    """

    def __init__(self, settings: SMTPSettings, *, should_stop=lambda: False, timeout_seconds=45,
                 _context=None) -> None:
        if (not isinstance(settings, SMTPSettings) or type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 45):
            raise ValueError('Niepoprawny nadzór SMTP')
        self.settings, self.should_stop, self.timeout = settings, should_stop, timeout_seconds
        self.context = _context or multiprocessing.get_context('spawn')

    def send(self, endpoint, report, *, idempotency_key: str) -> DeliveryResult:
        return self._run('report', endpoint, report, dict(idempotency_key=idempotency_key))

    def send_verification(self, endpoint, token: str, *, expires_at: datetime,
                          idempotency_key: str) -> DeliveryResult:
        return self._run('verification', endpoint, token, dict(expires_at=expires_at, idempotency_key=idempotency_key))

    def _run(self, method, endpoint, payload, metadata) -> DeliveryResult:
        if self.should_stop():
            return DeliveryResult('retry')  # No process and no SMTP operation have started.
        result = DeliveryResult('unknown')
        reader = writer = process = None
        deadline = time.monotonic() + self.timeout
        try:
            reader, writer = self.context.Pipe(duplex=False)
            process = self.context.Process(target=_smtp_child,
                                           args=(writer, self.settings, method, endpoint, payload, metadata), daemon=True)
            process.start()
            writer.close()
            while not self.should_stop():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                if reader.poll(min(.2, remaining)):
                    raw = json.loads(reader.recv_bytes(maxlength=2048))
                    if isinstance(raw, dict) and set(raw) == {'outcome', 'provider_message_id', 'retry_after_seconds'}:
                        result = DeliveryResult(**raw)
                    break
                if not process.is_alive():
                    break
        except Exception:
            pass  # A started child might have sent DATA; never infer a safe retry here.
        finally:
            if process is not None:
                with suppress(Exception):
                    if process.is_alive():
                        process.terminate()
                with suppress(Exception):
                    process.join(1)
                with suppress(Exception):
                    if process.is_alive():
                        process.kill()
                with suppress(Exception):
                    process.join(1)
                with suppress(Exception):
                    if not process.is_alive():
                        process.close()
            for pipe in (reader, writer):
                if pipe is not None:
                    with suppress(Exception):
                        pipe.close()
        return result
