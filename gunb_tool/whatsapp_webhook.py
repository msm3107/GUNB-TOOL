"""Pure authenticated Meta status boundary. No listener, writes or incoming commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re

from .notification_models import utc_iso
from .whatsapp_models import WhatsAppStatus, message_id, numeric_id, strict_json

MAX_WEBHOOK_BYTES = 65536
_OUTCOMES = {'sent': 'accepted', 'delivered': 'delivered', 'read': 'read', 'failed': 'failed'}


class WebhookError(ValueError):
    """Generic public rejection; never attach the raw payload or provider details."""


@dataclass(frozen=True)
class WhatsAppWebhookSettings:
    waba_id: str = field(repr=False)
    phone_number_id: str = field(repr=False)
    app_secret: str = field(repr=False)
    verify_token: str = field(repr=False)

    def __post_init__(self) -> None:
        try:
            numeric_id(self.waba_id)
            numeric_id(self.phone_number_id)
            for secret, minimum in ((self.app_secret, 1), (self.verify_token, 16)):
                if (not isinstance(secret, str) or not minimum <= len(secret) <= 256
                        or not re.fullmatch(r'[!-~]+', secret)):
                    raise ValueError
            if hmac.compare_digest(self.app_secret, self.verify_token):
                raise ValueError('Oddzielne sekrety')
        except (ValueError, TypeError):
            raise ValueError('Niepoprawna konfiguracja webhooka WhatsApp') from None


def _items(value, maximum: int):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError('Limit listy')
    return value


class WhatsAppWebhook:
    def __init__(self, settings: WhatsAppWebhookSettings) -> None:
        if not isinstance(settings, WhatsAppWebhookSettings):
            raise ValueError('Niepoprawna konfiguracja webhooka WhatsApp')
        self.settings = settings

    def challenge(self, mode: str, token: str, challenge: str) -> str:
        if (mode != 'subscribe' or not isinstance(token, str) or not token.isascii()
                or len(token) > 256 or not hmac.compare_digest(token, self.settings.verify_token)
                or not isinstance(challenge, str) or not re.fullmatch(r'[0-9]{1,256}', challenge)):
            raise WebhookError('Niepoprawny webhook WhatsApp') from None
        return challenge

    def parse_statuses(self, raw_body: bytes, signature: str | None) -> tuple[WhatsAppStatus, ...]:
        try:
            if (type(raw_body) is not bytes or not 1 <= len(raw_body) <= MAX_WEBHOOK_BYTES
                    or not isinstance(signature, str) or not re.fullmatch(r'sha256=[0-9a-f]{64}', signature)):
                raise ValueError('Brak uwierzytelnienia')
            expected = hmac.new(self.settings.app_secret.encode('ascii'), raw_body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature[7:], expected):
                raise ValueError('Brak uwierzytelnienia')
            raw = strict_json(raw_body, MAX_WEBHOOK_BYTES)
            if not isinstance(raw, dict) or raw.get('object') != 'whatsapp_business_account':
                raise ValueError('Nieobsługiwany obiekt')
            events = {}
            count = 0
            for entry in _items(raw.get('entry'), 20):
                if not isinstance(entry, dict):
                    raise ValueError('Niepoprawny entry')
                for change in _items(entry.get('changes'), 20):
                    if not isinstance(change, dict):
                        raise ValueError('Niepoprawny change')
                    if change.get('field') != 'messages':
                        continue
                    value = change.get('value')
                    if not isinstance(value, dict):
                        raise ValueError('Niepoprawny value')
                    statuses = _items(value.get('statuses', []), 100)
                    count += len(statuses)
                    if count > 100:
                        raise ValueError('Limit zdarzeń')
                    if (entry.get('id') != self.settings.waba_id or value.get('messaging_product') != 'whatsapp'):
                        continue
                    metadata = value.get('metadata')
                    if not isinstance(metadata, dict):
                        raise ValueError('Brak metadanych')
                    if metadata.get('phone_number_id') != self.settings.phone_number_id:
                        continue
                    for status in statuses:
                        if not isinstance(status, dict) or not isinstance(status.get('status'), str):
                            raise ValueError('Niepoprawny status')
                        outcome = _OUTCOMES.get(status['status'])
                        if outcome is None:
                            continue
                        ident = message_id(status.get('id'))
                        recipient = numeric_id(status.get('recipient_id'), minimum=8, maximum=15)
                        stamp = status.get('timestamp')
                        if not isinstance(stamp, str) or not re.fullmatch(r'[0-9]{1,12}', stamp):
                            raise ValueError('Niepoprawny czas')
                        seconds = int(stamp)
                        if seconds > 253402300799:
                            raise ValueError('Niepoprawny czas')
                        timestamp = utc_iso(datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds))
                        identity = json.dumps([self.settings.waba_id, self.settings.phone_number_id,
                                               ident, recipient, outcome, timestamp], separators=(',', ':'))
                        key = 'meta:' + hashlib.sha256(identity.encode('ascii')).hexdigest()
                        events[key] = WhatsAppStatus(ident, recipient, outcome, timestamp, key)
            return tuple(events.values())
        except (ValueError, TypeError, AttributeError, OverflowError):
            raise WebhookError('Niepoprawny webhook WhatsApp') from None
