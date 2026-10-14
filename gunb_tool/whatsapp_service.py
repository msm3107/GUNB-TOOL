"""Opt-in private webhook process; never loads bot configuration or starts Telegram."""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys
from typing import Mapping, Sequence

from .whatsapp_ingress import WhatsAppWebhookApplication
from .whatsapp_inbox_factory import WhatsAppInboxFactory
from .whatsapp_webhook import WhatsAppWebhookSettings


class WebhookServiceError(ValueError):
    """Fixed public startup error without configuration or storage details."""


def build_application(environ: Mapping[str, str] | None = None) -> WhatsAppWebhookApplication:
    env = os.environ if environ is None else environ
    try:
        if env.get('WHATSAPP_WEBHOOK_ENABLED') != 'true':
            raise ValueError
        path = env.get('WHATSAPP_WEBHOOK_DB_PATH', '')
        if not path or not Path(path).is_absolute():
            raise ValueError
        settings = WhatsAppWebhookSettings(env.get('WHATSAPP_WABA_ID', ''), env.get('WHATSAPP_PHONE_NUMBER_ID', ''),
                                           env.get('WHATSAPP_APP_SECRET', ''), env.get('WHATSAPP_VERIFY_TOKEN', ''))
        factory = WhatsAppInboxFactory(path, settings)
        # Existing schema only. Exit before the server can fork: no inherited DB handle.
        with factory() as inbox:
            inbox.repo.connection.execute(
                'SELECT id,channel,event_key,endpoint_id,provider_message_id,payload,received_at,processed_at'
                ' FROM notification_webhook_events LIMIT 0',
            ).fetchall()
        return WhatsAppWebhookApplication(settings, factory)
    except Exception:
        raise WebhookServiceError('Webhook WhatsApp: konfiguracja lub baza wymaga sprawdzenia') from None


def server_options() -> dict:
    """Fixed private Linux profile; no operator-supplied Gunicorn configuration."""
    return {'bind': '127.0.0.1:8765', 'workers': 2, 'worker_class': 'sync', 'threads': 1,
            'timeout': 30, 'graceful_timeout': 10, 'backlog': 32, 'max_requests': 1000,
            'max_requests_jitter': 100, 'keepalive': 0, 'limit_request_line': 4094,
            'limit_request_fields': 32, 'limit_request_field_size': 4096,
            'forwarded_allow_ips': '127.0.0.1', 'secure_scheme_headers': {'X-FORWARDED-PROTO': 'https'},
            'forwarder_headers': '', 'header_map': 'refuse', 'proxy_protocol': False,
            'http_protocols': 'h1', 'http_parser': 'python', 'control_socket_disable': True,
            'preload_app': True, 'reload': False, 'spew': False, 'daemon': False,
            'accesslog': None, 'capture_output': False, 'worker_tmp_dir': '/dev/shm',
            'errorlog': '-', 'loglevel': 'info', 'logger_class': 'gunb_tool.whatsapp_server.PrivateLogger'}


class PrivateLogMixin:
    """Keep server event levels, discard raw request/config/exception data."""

    def log(self, level, message, *args, **kwargs):
        if not isinstance(level, int) or level < logging.INFO:
            return
        self.error_log.log(level, 'Webhook HTTP: zdarzenie serwera')

    def critical(self, *args, **kwargs):
        self.log(logging.CRITICAL, '')

    def error(self, *args, **kwargs):
        self.log(logging.ERROR, '')

    def warning(self, *args, **kwargs):
        self.log(logging.WARNING, '')

    def info(self, *args, **kwargs):
        self.log(logging.INFO, '')

    def debug(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        self.log(logging.ERROR, '')

    def access(self, *args, **kwargs):
        pass


def _serve(app) -> None:
    # Isolate Gunicorn's process/FD activation and import-time defaults as well as CLI options.
    for name in ('GUNICORN_CMD_ARGS', 'GUNICORN_FD', 'GUNICORN_PID', 'LISTEN_FDS', 'LISTEN_PID',
                 'LISTEN_FDNAMES', 'WEB_CONCURRENCY', 'FORWARDED_ALLOW_IPS', 'PORT', 'NOTIFY_SOCKET'):
        os.environ.pop(name, None)
    from .whatsapp_server import serve
    serve(app)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise WebhookServiceError('Niepoprawne argumenty usługi webhooka')


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description='Prywatna usługa webhooka WhatsApp (Linux, osobny proces).')
    parser.add_argument('--check', action='store_true', help='sprawdź konfigurację i istniejącą bazę bez nasłuchu')
    try:
        args = parser.parse_args(argv)
        if not args.check and sys.platform != 'linux':
            raise WebhookServiceError('Nasłuch webhooka wymaga Linuxa')
        app = build_application()
    except WebhookServiceError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.check:
        print('Webhook WhatsApp: kontrola konfiguracji i bazy OK (bez nasłuchu)')
        return 0
    try:
        _serve(app)
    except ImportError:
        print('Webhook WhatsApp: zainstaluj opcjonalne zależności usługi', file=sys.stderr)
        return 2
    except SystemExit as exc:
        if exc.code in (None, 0):
            return 0
        print('Webhook WhatsApp: serwer zakończył się błędem', file=sys.stderr)
        return 1
    except Exception:
        print('Webhook WhatsApp: błąd serwera, sprawdź stan usługi', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
