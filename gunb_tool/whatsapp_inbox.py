"""Bounded durable Meta statuses. No listener, sends or channel activation."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from typing import Iterator

from .notification_models import bounded_int, utc_iso
from .notification_store import NotificationStore
from .whatsapp_webhook import WhatsAppWebhook, WhatsAppWebhookSettings


class WebhookInboxFull(RuntimeError):
    """Retryable intake failure: a future HTTP boundary must not ACK this batch."""


def _recipient_digest(ident: str, recipient: str) -> str:
    return hashlib.sha256((ident + '\0' + recipient).encode('ascii')).hexdigest()


class WhatsAppInbox:
    def __init__(self, store: NotificationStore, settings: WhatsAppWebhookSettings, *, max_events: int = 10000) -> None:
        if not isinstance(store, NotificationStore):
            raise ValueError('Niepoprawny magazyn powiadomień')
        self.max_events = bounded_int(max_events, 1, 100000)
        self._webhook = WhatsAppWebhook(settings)
        self.store = store
        self.repo = store.repo
        self._conn = self.repo.connection

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._conn.in_transaction:
            raise RuntimeError('Inbox wymaga połączenia bez zewnętrznej transakcji')
        with self.repo.transaction():
            yield

    def receive(self, raw_body: bytes, signature: str | None) -> int:
        """Authenticate first; return only after the complete batch commits.

        A successful zero is also durable (duplicate/irrelevant authenticated input).
        Never translate authentication, capacity or database failures into an ACK.
        """
        statuses = self._webhook.parse_statuses(raw_body, signature)
        with self._transaction():
            novel = [event for event in statuses if self._conn.execute(
                "SELECT 1 FROM notification_webhook_events WHERE channel = 'whatsapp' AND event_key = ?",
                (event.event_key,),
            ).fetchone() is None]
            if novel:
                count = self._conn.execute(
                    'SELECT COUNT(*) FROM (SELECT id FROM notification_webhook_events LIMIT ?)',
                    (self.max_events + 1,),
                ).fetchone()[0]
                if count + len(novel) > self.max_events:
                    raise WebhookInboxFull('Brak miejsca w inboxie webhooków')
                stamp = utc_iso(self.repo.now())
                self._conn.executemany(
                    'INSERT INTO notification_webhook_events'
                    ' (channel, event_key, provider_message_id, payload, received_at)'
                    " VALUES ('whatsapp', ?, ?, ?, ?)",
                    [(event.event_key, event.provider_message_id, json.dumps({
                        'version': 1, 'outcome': event.outcome, 'timestamp': event.timestamp,
                        'recipient_digest': _recipient_digest(event.provider_message_id, event.recipient_id),
                    }, separators=(',', ':')), stamp) for event in novel],
                )
        return len(novel)
