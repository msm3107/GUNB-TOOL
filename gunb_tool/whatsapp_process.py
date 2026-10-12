"""Disposable WhatsApp spawn process; bounded wait, no database in the child."""

from __future__ import annotations

from contextlib import suppress
import json
import math
import multiprocessing
import time

from .notification_models import DeliveryResult, NotificationEndpoint, ReportPart
from .whatsapp_models import message_id, strict_json
from .whatsapp_routing import WhatsAppRouting
from .whatsapp_sender import WhatsAppSender, WhatsAppSettings

MAX_RESULT_BYTES = 2048


def _decode_result(raw: bytes) -> DeliveryResult:
    try:
        data = strict_json(raw, MAX_RESULT_BYTES)
        if not isinstance(data, dict) or set(data) != {'outcome', 'provider_message_id', 'retry_after_seconds'}:
            raise ValueError
        result = DeliveryResult(**data)
        if result.outcome not in ('accepted', 'retry', 'failed', 'unknown'):
            raise ValueError
        if result.outcome == 'accepted':
            message_id(result.provider_message_id)
        elif result.provider_message_id is not None:
            raise ValueError
        return result
    except (ValueError, TypeError, AttributeError):
        return DeliveryResult('unknown')


def _whatsapp_child(pipe, settings, endpoint, report, key) -> None:
    try:
        result = WhatsAppSender(settings).send(endpoint, report, idempotency_key=key)
        if not isinstance(result, DeliveryResult):
            raise ValueError
        raw = json.dumps(dict(outcome=result.outcome, provider_message_id=result.provider_message_id,
                              retry_after_seconds=result.retry_after_seconds)).encode('utf-8')
        # Enforce the wire contract in both processes, never forward provider diagnostics.
        result = _decode_result(raw)
    except BaseException:
        result = DeliveryResult('unknown')
    try:
        pipe.send_bytes(json.dumps(dict(outcome=result.outcome, provider_message_id=result.provider_message_id,
                                        retry_after_seconds=result.retry_after_seconds)).encode('utf-8'))
    except BaseException:
        pass
    finally:
        with suppress(BaseException):
            pipe.close()


class SupervisedWhatsApp:
    """45s wait + two <=1s cleanup joins under normal OS operation, default lease 120s.

    Future runtime must use a lease >=60s and its own worker thread. OS start/calls
    and scheduling are not hard real-time. A lost ACK stays unknown, never resent.
    """

    def __init__(self, settings: WhatsAppSettings, *, should_stop=lambda: False, timeout_seconds=45,
                 _context=None) -> None:
        if (not isinstance(settings, WhatsAppSettings) or not callable(should_stop)
                or type(timeout_seconds) not in (int, float) or not 0 < timeout_seconds <= 45
                or not math.isfinite(timeout_seconds)):
            raise ValueError('Niepoprawny nadzór WhatsApp')
        self.settings, self.should_stop, self.timeout = settings, should_stop, timeout_seconds
        self.routing = WhatsAppRouting(settings.waba_id, settings.phone_number_id)
        self.context = _context or multiprocessing.get_context('spawn')

    def send(self, endpoint: NotificationEndpoint, report: ReportPart, *, idempotency_key: str) -> DeliveryResult:
        result = DeliveryResult('unknown')
        reader = writer = process = None
        deadline = time.monotonic() + self.timeout
        try:
            if self.should_stop():
                return DeliveryResult('retry')  # No child or HTTP operation has started.
            reader, writer = self.context.Pipe(duplex=False)
            process = self.context.Process(target=_whatsapp_child,
                                           args=(writer, self.settings, endpoint, report, idempotency_key), daemon=True)
            if self.should_stop() or time.monotonic() >= deadline:
                return DeliveryResult('retry')  # Preparation exhausted budget, still no child.
            process.start()
            writer.close()
            while not self.should_stop():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                process.join(min(.2, remaining))
                if not process.is_alive():
                    # Readability alone is unsafe: a live writer could be suspended
                    # mid-frame. After exit, closed writers ensure complete data or EOF.
                    if reader.poll(0):
                        result = _decode_result(reader.recv_bytes(maxlength=MAX_RESULT_BYTES))
                    break
        except Exception:
            pass  # A child might already have POSTed; never infer a safe retry.
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
