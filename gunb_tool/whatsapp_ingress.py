"""Inactive bounded WSGI boundary. Deployment must supply a trusted HTTPS server."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
import re
import threading
from urllib.parse import parse_qsl

from .whatsapp_inbox import WhatsAppInbox
from .whatsapp_webhook import MAX_WEBHOOK_BYTES, WebhookError, WhatsAppWebhook, WhatsAppWebhookSettings


_JSON = re.compile(r'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?', re.IGNORECASE)
_RESPONSES = {
    200: ('OK', b'OK\n'), 400: ('Bad Request', b'Bad Request\n'),
    403: ('Forbidden', b'Forbidden\n'), 404: ('Not Found', b'Not Found\n'),
    405: ('Method Not Allowed', b'Method Not Allowed\n'),
    411: ('Length Required', b'Length Required\n'),
    413: ('Content Too Large', b'Content Too Large\n'),
    415: ('Unsupported Media Type', b'Unsupported Media Type\n'),
    503: ('Service Unavailable', b'Service Unavailable\n'),
}


class WhatsAppWebhookApplication:
    def __init__(self, settings: WhatsAppWebhookSettings,
                 inbox_factory: Callable[[], AbstractContextManager[WhatsAppInbox]], *, max_inflight: int = 4) -> None:
        if (not isinstance(settings, WhatsAppWebhookSettings) or not callable(inbox_factory)
                or type(max_inflight) is not int or not 1 <= max_inflight <= 16):
            raise ValueError('Niepoprawna konfiguracja ingressu WhatsApp')
        self._webhook = WhatsAppWebhook(settings)
        self._inbox_factory = inbox_factory
        self._capacity = threading.BoundedSemaphore(max_inflight)

    def __call__(self, environ, start_response) -> list[bytes]:
        code, body = self._request(environ)
        reason, default_body = _RESPONSES[code]
        body = default_body if body is None else body
        headers = [('Content-Type', 'text/plain; charset=utf-8'), ('Content-Length', str(len(body))),
                   ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff')]
        if code == 503:
            headers.append(('Retry-After', '5'))
        if code == 405:
            headers.append(('Allow', 'GET, POST'))
        # Context exit and semaphore release precede the transport. Never retry it.
        start_response(f'{code} {reason}', headers)
        return [body]

    def _request(self, env) -> tuple[int, bytes | None]:
        if env.get('PATH_INFO') != '/webhooks/whatsapp':
            return 404, None
        if env.get('wsgi.url_scheme') != 'https':
            return 403, None
        method = env.get('REQUEST_METHOD')
        if method not in ('GET', 'POST'):
            return 405, None
        if env.get('HTTP_TRANSFER_ENCODING'):
            return 400, None
        if env.get('HTTP_CONTENT_ENCODING', '').lower() not in ('', 'identity'):
            return 415, None
        if method == 'GET':
            return self._challenge(env)
        if env.get('QUERY_STRING', ''):
            return 400, None
        if not _JSON.fullmatch(env.get('CONTENT_TYPE', '')):
            return 415, None
        length = env.get('CONTENT_LENGTH')
        if length in (None, ''):
            return 411, None
        if not isinstance(length, str) or not re.fullmatch(r'[0-9]+', length):
            return 400, None
        if len(length) > 10 or int(length) > MAX_WEBHOOK_BYTES:
            return 413, None
        size = int(length)
        if size == 0:
            return 400, None
        if not self._capacity.acquire(blocking=False):
            return 503, None
        try:
            return self._receive(env, size)
        finally:
            self._capacity.release()

    def _challenge(self, env) -> tuple[int, bytes | None]:
        if env.get('CONTENT_LENGTH') not in (None, '', '0'):
            return 400, None
        query = env.get('QUERY_STRING', '')
        if (len(query) > 2048 or not query.isascii() or re.search(r'%(?![0-9a-fA-F]{2})', query)):
            return 400, None
        try:
            pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3,
                              encoding='utf-8', errors='strict')
        except ValueError:
            return 400, None
        fields = dict(pairs)
        if len(pairs) != 3 or set(fields) != {'hub.mode', 'hub.verify_token', 'hub.challenge'}:
            return 400, None
        try:
            challenge = self._webhook.challenge(fields['hub.mode'], fields['hub.verify_token'], fields['hub.challenge'])
        except WebhookError:
            return 403, None
        return 200, challenge.encode('ascii')

    def _receive(self, env, size: int) -> tuple[int, None]:
        raw = bytearray()
        try:
            while len(raw) < size:
                requested = min(8192, size - len(raw))
                chunk = env['wsgi.input'].read(requested)
                if type(chunk) is not bytes or not 1 <= len(chunk) <= requested:
                    return 400, None
                raw.extend(chunk)
        except Exception:
            return 503, None
        body = bytes(raw)
        signature = env.get('HTTP_X_HUB_SIGNATURE_256')
        try:
            self._webhook.parse_statuses(body, signature)
        except WebhookError:
            return 403, None
        received = False
        try:
            with self._inbox_factory() as inbox:
                if not isinstance(inbox, WhatsAppInbox) or inbox.webhook_settings != self._webhook.settings:
                    return 503, None
                inbox.receive(body, signature)
                received = True
        except Exception:
            # Includes enter/receive/COMMIT/exit faults; no raw exception in diagnostics.
            return 503, None
        # A context manager may suppress receive's exception on exit.
        return (200 if received else 503), None
