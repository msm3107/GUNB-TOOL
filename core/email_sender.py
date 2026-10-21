"""TLS-only SMTP adapter; no runtime configuration or automatic retry."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime


@dataclass(frozen=True)
class DeliveryResult:
    outcome: str  # accepted | retry | failed | unknown
    provider_message_id: str | None = None
    retry_after_seconds: float | None = None


def normalize_address(channel: str, address: str) -> str:
    if not isinstance(address, str):
        raise ValueError("Niepoprawny adres")
    address = address.strip()
    if not address.isascii() or len(address) > 254 or address.count("@") != 1:
        raise ValueError("Niepoprawny pojedynczy adres e-mail")
    local, domain = address.split("@")
    if not 1 <= len(local) <= 64 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local):
        raise ValueError("Niepoprawna nazwa konta e-mail")
    if not 1 <= len(domain) <= 253 or not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*",
        domain,
    ):
        raise ValueError("Niepoprawna domena e-mail")
    return f"{local}@{domain.lower()}"


def bounded_text(value: str, max_length: int, *, single_line: bool = False) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        raise ValueError("Niepoprawna długość tekstu")
    if single_line and any(c in "\r\n" for c in value):
        raise ValueError("Tekst wielolinijkowy jest niedozwolony")
    return value


def bounded_int(value: int, min_value: int, max_value: int) -> int:
    if type(value) is not int or not min_value <= value <= max_value:
        raise ValueError(f"Wartość poza zakresem {min_value}–{max_value}")
    return value


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    from_address: str = field(repr=False)
    tls: str = "starttls"
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    timeout_seconds: float = 5

    def __post_init__(self) -> None:
        bounded_text(self.host, 253, single_line=True)
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            labels = self.host.split(".")
            if not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels):
                raise ValueError("Niepoprawny host SMTP") from None
        bounded_int(self.port, 1, 65535)
        object.__setattr__(self, "from_address", normalize_address("email", self.from_address))
        if self.tls not in ("starttls", "implicit"):
            raise ValueError("SMTP wymaga TLS")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or not 0.1 <= self.timeout_seconds <= 10
        ):
            raise ValueError("Niepoprawny timeout SMTP")
        if (
            not isinstance(self.username, str)
            or not isinstance(self.password, str)
            or bool(self.username) != bool(self.password)
        ):
            raise ValueError("Niekompletne dane SMTP")
        if self.username:
            for value, size in ((self.username, 256), (self.password, 1024)):
                bounded_text(value, size, single_line=True)
                if not value.isascii():
                    raise ValueError("Dane SMTP wymagają ASCII")


def _rejection(code: int) -> DeliveryResult:
    if type(code) is int and 400 <= code < 500:
        return DeliveryResult("retry")
    if type(code) is int and 500 <= code < 600:
        return DeliveryResult("failed")
    return DeliveryResult("unknown")


class SMTPEmailSender:
    """Wysyłka e-mail z limitem czasu i obsługą TLS."""

    def __init__(self, settings: SMTPSettings) -> None:
        if not isinstance(settings, SMTPSettings):
            raise ValueError("Niepoprawna konfiguracja SMTP")
        self.settings = settings

    def send_email(
        self,
        recipient: str,
        subject: str,
        body: str,
        *,
        idempotency_key: str | None = None,
    ) -> DeliveryResult:
        address = normalize_address("email", recipient)
        key = idempotency_key or hashlib.sha256(f"{address}:{subject}:{time.time()}".encode()).hexdigest()
        message = self._message(address, subject, body, key)
        return self._deliver(address, message)

    def _message(self, address: str, title: str, body: str, key: str) -> EmailMessage:
        bounded_text(key, 256, single_line=True)
        bounded_text(title, 120, single_line=True)
        message = EmailMessage(policy=SMTP)
        message["From"] = self.settings.from_address
        message["To"] = address
        message["Subject"] = title
        message["Date"] = format_datetime(datetime.now(timezone.utc))
        domain = self.settings.from_address.rsplit("@", 1)[1]
        message["Message-ID"] = f"<{hashlib.sha256(key.encode()).hexdigest()}@{domain}>"
        message.set_content(body, charset="utf-8", cte="quoted-printable")
        return message

    def _deliver(self, address: str, message: EmailMessage) -> DeliveryResult:
        client = None
        data_started = False
        deadline = time.monotonic() + 45

        def call(method, *args, **kwargs):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Wyczerpany budżet SMTP")
            if client.sock is not None:
                client.sock.settimeout(min(self.settings.timeout_seconds, remaining))
            return method(*args, **kwargs)

        try:
            payload = message.as_bytes()
            message_id = str(message["Message-ID"])
            receipt_id = (
                message_id if len(message_id) <= 256 else "sha256:" + hashlib.sha256(message_id.encode("ascii")).hexdigest()
            )
            accepted = DeliveryResult("accepted", receipt_id)
            context = ssl.create_default_context()
            if self.settings.tls == "implicit":
                client = smtplib.SMTP_SSL(
                    self.settings.host, self.settings.port, timeout=self.settings.timeout_seconds, context=context
                )
            else:
                client = smtplib.SMTP(self.settings.host, self.settings.port, timeout=self.settings.timeout_seconds)
            client.set_debuglevel(0)
            code, _ = call(client.ehlo)
            if code != 250:
                return _rejection(code)
            if self.settings.tls == "starttls":
                call(client.starttls, context=context)
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
            if time.monotonic() >= deadline:
                return DeliveryResult("retry")
            data_started = True
            code, _ = call(client.data, payload)
            if code == 250:
                return accepted
            return _rejection(code)
        except (ssl.SSLCertVerificationError, smtplib.SMTPNotSupportedError, ValueError):
            return DeliveryResult("failed") if not data_started else DeliveryResult("unknown")
        except smtplib.SMTPResponseException as exc:
            return _rejection(exc.smtp_code)
        except OSError:
            return DeliveryResult("unknown" if data_started else "retry")
        except Exception:
            return DeliveryResult("unknown")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
