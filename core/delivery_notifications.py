"""Bounded test-mode publication emails through the existing SMTP adapter."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from .b2b_outbox import MAX_ATTEMPTS, B2BOutbox, ReadyDelivery, _limit
from .checkout_store import CheckoutUnavailable
from .email_sender import DeliveryResult, SMTPSettings
from .smtp_process import SupervisedSMTP

log = logging.getLogger(__name__)


def _valid_origin(origin: str) -> bool:
    if (
        not isinstance(origin, str)
        or not origin.isascii()
        or not 1 <= len(origin) <= 2048
        or any(ord(c) <= 32 for c in origin)
    ):
        return False
    try:
        value = urlsplit(origin)
        host = value.hostname
        valid_host = host == "::1" or bool(
            host and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
        )
        return bool(
            valid_host
            and origin == value.scheme + "://" + value.netloc
            and value.username is None
            and value.password is None
            and not (
                value.path
                or value.query
                or value.fragment
                or value.username
                or value.password
            )
            and (value.port is None or 1 <= value.port <= 65535)
            and (
                value.scheme == "https"
                or value.scheme == "http"
                and host in {"127.0.0.1", "localhost", "::1"}
            )
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class DeliveryNotificationSettings:
    enabled: bool = False
    origin: str = ""
    smtp: SMTPSettings | None = field(default=None, repr=False)

    def __post_init__(self):
        if (
            type(self.enabled) is not bool
            or self.enabled
            and (
                not _valid_origin(self.origin)
                or not isinstance(self.smtp, SMTPSettings)
            )
        ):
            raise ValueError("Invalid notification settings")

    @classmethod
    def from_env(cls, source: Mapping[str, str]) -> DeliveryNotificationSettings:
        mode = source.get("GUNB_B2B_NOTIFY_MODE", "disabled")
        if mode == "disabled":
            return cls()
        if mode != "test":
            raise ValueError("Notifications support disabled or test mode")
        return cls(
            True,
            source.get("GUNB_B2B_NOTIFY_ORIGIN", ""),
            SMTPSettings(
                source.get("GUNB_B2B_NOTIFY_SMTP_HOST", ""),
                int(source.get("GUNB_B2B_NOTIFY_SMTP_PORT", "587")),
                source.get("GUNB_B2B_NOTIFY_SMTP_FROM", ""),
                source.get("GUNB_B2B_NOTIFY_SMTP_TLS", "starttls"),
                source.get("GUNB_B2B_NOTIFY_SMTP_USERNAME", ""),
                source.get("GUNB_B2B_NOTIFY_SMTP_PASSWORD", ""),
            ),
        )


class EmailSender(Protocol):
    def send_email(
        self,
        recipient: str,
        subject: str,
        body: str,
        *,
        idempotency_key: str,
        html_body: str | None = None,
    ) -> DeliveryResult: ...


def render_ready(delivery: ReadyDelivery, origin: str) -> tuple[str, str, str]:
    if (
        not _valid_origin(origin)
        or type(delivery.delivery_id) is not int
        or not 1 <= delivery.delivery_id < 2**63
    ):
        raise ValueError("Invalid notification destination")
    counts = (delivery.new_count, delivery.change_count, delivery.full_count)
    if (
        any(type(n) is not int or not 0 <= n <= 10000 for n in counts)
        or sum(counts) > 10000
    ):
        raise ValueError("Invalid notification counts")
    url = origin + "/panel/paczki/" + str(delivery.delivery_id)
    subject = "Żółta Tablica — Twoja paczka jest gotowa"
    text = (
        "Twoja paczka jest dostępna w panelu Żółtej Tablicy.\n\n"
        f"Nowe: {delivery.new_count}\nZmiany: {delivery.change_count}\nPełna baza: {delivery.full_count}\n\n"
        f"Otwórz własną paczkę: {url}\n\n"
        "Zaloguj się na swoje konto, aby przejrzeć lub pobrać pliki.\n"
        "To usługowe powiadomienie o opublikowanej paczce.\n"
    )
    markup = (
        '<!doctype html><html lang="pl"><body>'
        "<h1>Żółta Tablica</h1><p>Twoja paczka jest dostępna w panelu.</p>"
        f"<p>Nowe: {delivery.new_count}<br>Zmiany: {delivery.change_count}<br>Pełna baza: {delivery.full_count}</p>"
        f'<p><a href="{html.escape(url, quote=True)}">Otwórz własną paczkę</a></p>'
        "<p>Zaloguj się na swoje konto, aby przejrzeć lub pobrać pliki.</p>"
        "<p>Usługowe powiadomienie o opublikowanej paczce.</p></body></html>"
    )
    return subject, text, markup


class DeliveryNotificationWorker:
    def __init__(
        self,
        store: B2BOutbox,
        settings: DeliveryNotificationSettings,
        *,
        sender: EmailSender | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.store, self.settings = store, settings
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sender = sender or (
            SupervisedSMTP(settings.smtp) if settings.enabled else None
        )
        self._slots = threading.BoundedSemaphore(2)

    def run_pending(self, limit: int = 10) -> dict[str, int]:
        _limit(limit)
        counts = dict.fromkeys(
            ("accepted", "rejected", "unknown", "retry", "cancelled", "skipped"), 0
        )
        if not self.settings.enabled:
            return counts
        if not self._slots.acquire(blocking=False):
            raise CheckoutUnavailable
        try:
            counts["unknown"] = self.store.recover_stale(now=self._now(), limit=limit)
            for _ in range(limit):
                ticket = self.store.claim(now=self._now())
                if ticket is None:
                    break
                ready = self.store.begin_send(ticket, now=self._now())
                if ready is None:
                    counts["cancelled"] += 1
                    continue
                try:
                    subject, text, markup = render_ready(ready, self.settings.origin)
                except ValueError:
                    result = DeliveryResult("failed")
                else:
                    key = hashlib.sha256(
                        f"{ticket.customer_id}:{ticket.delivery_id}:{ticket.published_at}".encode()
                    ).hexdigest()
                    try:
                        result = self._sender.send_email(
                            ticket.recipient,
                            subject,
                            text,
                            idempotency_key="zt-package-ready-" + key,
                            html_body=markup,
                        )
                        if not isinstance(result, DeliveryResult):
                            result = DeliveryResult("unknown")
                    except Exception:  # Provider IO may already have accepted DATA.
                        result = DeliveryResult("unknown")
                try:
                    saved = self.store.finish(ticket, result, now=self._now())
                except CheckoutUnavailable:
                    saved = False
                if not saved:
                    counts["skipped"] += 1
                    log.warning(
                        "Notification outcome not persisted; delivery_id=%s",
                        ticket.delivery_id,
                    )
                else:
                    outcome = {
                        "accepted": "accepted",
                        "failed": "rejected",
                        "retry": "retry",
                    }.get(result.outcome, "unknown")
                    if outcome == "retry" and ticket.attempts + 1 >= MAX_ATTEMPTS:
                        outcome = "rejected"
                    counts[outcome] += 1
            return counts
        finally:
            self._slots.release()


def main(
    argv: list[str] | None = None, *, source: Mapping[str, str] | None = None
) -> int:
    parser = argparse.ArgumentParser(
        description="Process bounded test publication notifications"
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--enqueue-ready", type=int)
    args = parser.parse_args(argv)
    try:
        _limit(args.limit)
        settings = DeliveryNotificationSettings.from_env(
            os.environ if source is None else source
        )
        if not settings.enabled:
            print("Publication notifications disabled")
            return 0
        store = B2BOutbox(args.database)
        if args.enqueue_ready is not None:
            store.enqueue_ready(args.enqueue_ready)
        result = DeliveryNotificationWorker(store, settings).run_pending(args.limit)
    except (ValueError, TypeError, CheckoutUnavailable):
        print(
            "Notifications unavailable; verify test configuration and existing WAL schema"
        )
        return 1
    print(json.dumps(result, sort_keys=True))
    return int(any(result[k] for k in ("rejected", "unknown", "retry", "skipped")))


if __name__ == "__main__":
    raise SystemExit(main())
