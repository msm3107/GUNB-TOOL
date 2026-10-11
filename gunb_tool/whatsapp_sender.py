"""Inactive-by-default Meta template Sender. One request, conservative uncertainty."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import re
import time
import unicodedata

import requests

from .notification_models import DeliveryResult, NotificationEndpoint, ReportPart, bounded_text
from .notification_store import normalize_address
from .whatsapp_models import message_id, numeric_id, strict_json

MAX_RESPONSE_BYTES = 16384
MAX_TEMPLATE_TEXT_CHARS = 900


@dataclass(frozen=True)
class WhatsAppSettings:
    api_version: str
    phone_number_id: str = field(repr=False)
    access_token: str = field(repr=False)
    template_name: str
    language: str = 'pl'
    timeout_seconds: float = 5

    def __post_init__(self) -> None:
        try:
            if not isinstance(self.api_version, str) or not re.fullmatch(r'v[1-9][0-9]{0,2}\.0', self.api_version):
                raise ValueError
            numeric_id(self.phone_number_id)
            if (not isinstance(self.access_token, str) or not 1 <= len(self.access_token) <= 4096
                    or not re.fullmatch(r'[!-~]+', self.access_token)):
                raise ValueError
            if (not isinstance(self.template_name, str) or not 1 <= len(self.template_name) <= 512
                    or not re.fullmatch(r'[a-z0-9_]+', self.template_name)):
                raise ValueError
            if not isinstance(self.language, str) or not re.fullmatch(r'[a-z]{2,3}(?:_[A-Z]{2})?', self.language):
                raise ValueError
            if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                    or not 0.1 <= self.timeout_seconds <= 10):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError('Niepoprawna konfiguracja WhatsApp') from None


def _template_text(value: str) -> str:
    if any(unicodedata.category(ch) in ('Cc', 'Cf') and ch not in '\n\r\t' for ch in value):
        raise ValueError('Znaki sterujące')
    return ' | '.join(' '.join(line.split()) for line in value.splitlines() if line.strip())


class WhatsAppSender:
    """No hard DNS deadline: future runtime must supervise calls within its lease.

    Meta idempotency is not assumed. The worker key is accepted as a protocol
    argument, never turned into an undocumented header or an automatic retry.
    """

    def __init__(self, settings: WhatsAppSettings) -> None:
        if not isinstance(settings, WhatsAppSettings):
            raise ValueError('Niepoprawna konfiguracja WhatsApp')
        self.settings = settings

    def send(self, endpoint: NotificationEndpoint, report: ReportPart, *, idempotency_key: str) -> DeliveryResult:
        try:
            if (not isinstance(endpoint, NotificationEndpoint) or endpoint.channel != 'whatsapp'
                    or not endpoint.enabled or not endpoint.verified_at or not endpoint.consent_at
                    or endpoint.consent_revoked_at is not None or not isinstance(report, ReportPart)
                    or report.chat_id != endpoint.chat_id or report.endpoint_version != endpoint.version):
                raise ValueError('Nieaktualny odbiorca')
            address = normalize_address('whatsapp', endpoint.address)
            if address != endpoint.address:
                raise ValueError('Niekanoniczny numer')
            bounded_text(idempotency_key, 256, single_line=True)
            title, body = _template_text(report.title), _template_text(report.body)
            if not title or not body or len(title) + len(body) > MAX_TEMPLATE_TEXT_CHARS:
                raise ValueError('Limit szablonu')
            payload = json.dumps({
                'messaging_product': 'whatsapp', 'recipient_type': 'individual', 'to': address[1:],
                'type': 'template', 'template': {
                    'name': self.settings.template_name, 'language': {'code': self.settings.language},
                    'components': [{'type': 'body', 'parameters': [
                        {'type': 'text', 'text': title}, {'type': 'text', 'text': body},
                    ]}],
                },
            }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        except (ValueError, TypeError, AttributeError):
            return DeliveryResult('failed')
        return self._deliver(address[1:], payload)

    def _deliver(self, recipient: str, payload: bytes) -> DeliveryResult:
        session = response = None
        deadline = time.monotonic() + 30
        try:
            session = requests.Session()
            session.trust_env = False
            session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return DeliveryResult('retry')  # HTTP has not started.
            timeout = min(self.settings.timeout_seconds, remaining)
            response = session.post(
                f'https://graph.facebook.com/{self.settings.api_version}/{self.settings.phone_number_id}/messages',
                data=payload, headers={'Authorization': 'Bearer ' + self.settings.access_token,
                                       'Content-Type': 'application/json', 'Accept-Encoding': 'identity'},
                timeout=(timeout, timeout), allow_redirects=False, stream=True, verify=True,
            )
            if response.headers.get('Content-Encoding', '').lower() not in ('', 'identity'):
                return DeliveryResult('unknown')
            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=1024):
                if time.monotonic() >= deadline or len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                    return DeliveryResult('unknown')
                chunks.extend(chunk)
            raw = strict_json(bytes(chunks), MAX_RESPONSE_BYTES)
            if not isinstance(raw, dict):
                return DeliveryResult('unknown')
            status = response.status_code
            if status == 200 and 'error' not in raw:
                contacts, messages = raw.get('contacts'), raw.get('messages')
                if (raw.get('messaging_product') == 'whatsapp' and isinstance(contacts, list) and len(contacts) == 1
                        and isinstance(contacts[0], dict) and contacts[0].get('wa_id') == recipient
                        and isinstance(messages, list) and len(messages) == 1 and isinstance(messages[0], dict)):
                    return DeliveryResult('accepted', message_id(messages[0].get('id')))
            error = raw.get('error')
            if (400 <= status < 500 and status not in (408, 409) and 'messages' not in raw
                    and isinstance(error, dict) and type(error.get('code')) is int and 0 < error['code'] < 2**31):
                if status == 429:
                    value = response.headers.get('Retry-After', '')
                    retry_after = (min(int(value), 86400) if isinstance(value, str)
                                   and re.fullmatch(r'[0-9]{1,10}', value) else None)
                    return DeliveryResult('retry', retry_after_seconds=retry_after)
                return DeliveryResult('failed')
            return DeliveryResult('unknown')
        except Exception:
            # HTTP/DNS errors can include tokens, URLs, recipients or provider text.
            # Once a request might have started, a second send is unsafe.
            return DeliveryResult('unknown')
        finally:
            for resource in (response, session):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
