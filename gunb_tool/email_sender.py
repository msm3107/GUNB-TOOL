"""TLS-only SMTP adapter; no runtime configuration or automatic retry."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
import hashlib
import ipaddress
import math
import re
import smtplib
import ssl
import time

from .notification_models import DeliveryResult, NotificationEndpoint, ReportPart, bounded_int, bounded_text, utc_iso
from .notification_store import normalize_address


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    from_address: str = field(repr=False)
    tls: str = 'starttls'
    username: str = field(default='', repr=False)
    password: str = field(default='', repr=False)
    timeout_seconds: float = 5

    def __post_init__(self) -> None:
        bounded_text(self.host, 253, single_line=True)
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            labels = self.host.split('.')
            if not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in labels):
                raise ValueError('Niepoprawny host SMTP') from None
        bounded_int(self.port, 1, 65535)
        object.__setattr__(self, 'from_address', normalize_address('email', self.from_address))
        if self.tls not in ('starttls', 'implicit'):
            raise ValueError('SMTP wymaga TLS')
        if (type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds)
                or not 0.1 <= self.timeout_seconds <= 10):
            raise ValueError('Niepoprawny timeout SMTP')
        if not isinstance(self.username, str) or not isinstance(self.password, str) or bool(self.username) != bool(self.password):
            raise ValueError('Niekompletne dane SMTP')
        if self.username:
            for value, size in ((self.username, 256), (self.password, 1024)):
                bounded_text(value, size, single_line=True)
                if not value.isascii():
                    raise ValueError('Dane SMTP wymagają ASCII')


def _rejection(code: int) -> DeliveryResult:
    if type(code) is int and 400 <= code < 500:
        return DeliveryResult('retry')
    if type(code) is int and 500 <= code < 600:
        return DeliveryResult('failed')
    return DeliveryResult('unknown')


class SMTPEmailSender:
    """Socket timeout + cooperative session budget; lease supervision belongs to integration.

    The budget is checked between calls, not a hard DNS/protocol interruption deadline.
    SMTP Message-ID helps correlation; SMTP does not provide exactly-once submission.
    """

    def __init__(self, settings: SMTPSettings) -> None:
        if not isinstance(settings, SMTPSettings):
            raise ValueError('Niepoprawna konfiguracja SMTP')
        self.settings = settings

    @staticmethod
    def _address(endpoint: NotificationEndpoint) -> str:
        if not isinstance(endpoint, NotificationEndpoint) or endpoint.channel != 'email':
            raise ValueError('Niepoprawny odbiorca SMTP')
        address = normalize_address('email', endpoint.address)
        if address != endpoint.address:
            raise ValueError('Niekanoniczny odbiorca SMTP')
        return address

    def send(self, endpoint: NotificationEndpoint, report: ReportPart, *, idempotency_key: str) -> DeliveryResult:
        try:
            address = self._address(endpoint)
            if (not isinstance(report, ReportPart) or not endpoint.enabled or report.chat_id != endpoint.chat_id
                    or report.endpoint_version != endpoint.version):
                raise ValueError('Nieaktualny raport SMTP')
            message = self._message(address, report.title, report.body, idempotency_key)
        except (ValueError, TypeError, AttributeError):
            return DeliveryResult('failed')
        return self._deliver(address, message)

    def send_verification(self, endpoint: NotificationEndpoint, token: str, *, expires_at: datetime,
                          idempotency_key: str) -> DeliveryResult:
        try:
            address = self._address(endpoint)
            if endpoint.enabled or not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
                raise ValueError('Niepoprawna weryfikacja SMTP')
            expiry = utc_iso(expires_at)
            body = ('Żółta Tablica — potwierdzenie adresu e-mail.\n\n'
                    f'Kod jednorazowy:\n{token}\n\nWażny do: {expiry}.\n'
                    'Wpisz kod w zaufanym interfejsie, w którym zlecono weryfikację.\n'
                    'Potwierdzenie skrzynki nie włącza powiadomień ani nie zapisuje zgody.\n'
                    'Jeśli nie zlecono tej operacji, zignoruj wiadomość. Nie przekazuj kodu innym osobom.\n')
            message = self._message(address, 'Żółta Tablica — potwierdź adres e-mail', body, idempotency_key)
        except (ValueError, TypeError, AttributeError):
            return DeliveryResult('failed')
        return self._deliver(address, message)

    def _message(self, address: str, title: str, body: str, key: str) -> EmailMessage:
        bounded_text(key, 256, single_line=True)
        bounded_text(title, 120, single_line=True)
        message = EmailMessage(policy=SMTP)
        message['From'] = self.settings.from_address
        message['To'] = address
        message['Subject'] = title
        message['Date'] = format_datetime(datetime.now(timezone.utc))
        domain = self.settings.from_address.rsplit('@', 1)[1]
        message['Message-ID'] = f'<{hashlib.sha256(key.encode()).hexdigest()}@{domain}>'
        message.set_content(body, charset='utf-8')
        return message

    def _deliver(self, address: str, message: EmailMessage) -> DeliveryResult:
        client = None
        data_started = False
        deadline = time.monotonic() + 45

        def call(method, *args, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Wyczerpany budżet SMTP')
            if client.sock is not None:
                client.sock.settimeout(min(self.settings.timeout_seconds, remaining))
            return method(*args, **kwargs)

        try:
            payload = message.as_bytes()
            context = ssl.create_default_context()
            if self.settings.tls == 'implicit':
                client = smtplib.SMTP_SSL(self.settings.host, self.settings.port,
                                          timeout=self.settings.timeout_seconds, context=context)
            else:
                client = smtplib.SMTP(self.settings.host, self.settings.port, timeout=self.settings.timeout_seconds)
            client.set_debuglevel(0)
            code, _ = call(client.ehlo)
            if code != 250:
                return _rejection(code)
            if self.settings.tls == 'starttls':
                call(client.starttls, context=context)  # smtplib rejects non-220 and missing extension.
                code, _ = call(client.ehlo)
                if code != 250:
                    return _rejection(code)
            if self.settings.username:
                call(client.login, self.settings.username, self.settings.password)
            code, _ = call(client.mail, self.settings.from_address)
            if code != 250:
                return _rejection(code)
            code, _ = call(client.rcpt, address)
            if code not in (250, 251, 252):
                return _rejection(code)
            # Budget exhaustion before calling DATA is a definite non-acceptance.
            if time.monotonic() >= deadline:
                return DeliveryResult('retry')
            data_started = True
            code, _ = call(client.data, payload)
            if code == 250:
                return DeliveryResult('accepted', str(message['Message-ID']))
            return _rejection(code)
        except (ssl.SSLCertVerificationError, smtplib.SMTPNotSupportedError, ValueError):
            return DeliveryResult('failed') if not data_started else DeliveryResult('unknown')
        except smtplib.SMTPResponseException as exc:
            return _rejection(exc.smtp_code)
        except OSError:
            return DeliveryResult('unknown' if data_started else 'retry')
        except Exception:
            return DeliveryResult('unknown')
        finally:
            if client is not None:
                try:
                    client.close()  # Do not let QUIT/cleanup overwrite a received DATA result.
                except Exception:
                    pass
