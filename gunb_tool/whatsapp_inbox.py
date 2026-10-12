"""Bounded durable Meta statuses. No listener, sends or channel activation."""

from __future__ import annotations

from contextlib import contextmanager
import hmac
import json
import re
import sqlite3
from typing import Iterator

from .notification_models import ReportPart, bounded_int, parse_time, utc_iso
from .notification_store import NotificationStore
from .whatsapp_models import message_id, strict_json
from .whatsapp_routing import WhatsAppRouting
from .whatsapp_webhook import WhatsAppWebhook, WhatsAppWebhookSettings


class WebhookInboxFull(RuntimeError):
    """Retryable intake failure: a future HTTP boundary must not ACK this batch."""


_RANK = {'accepted': 1, 'delivered': 2, 'read': 3}


def _decode(row: sqlite3.Row) -> tuple[str, str, str | None]:
    """Validate the durable minimal format; never expose a corrupt payload."""
    message_id(row['provider_message_id'])
    if not re.fullmatch(r'meta:[0-9a-f]{64}', row['event_key']):
        raise ValueError('Niepoprawny klucz')
    data = strict_json(row['payload'].encode('utf-8'), 1024)
    if (not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] not in (1, 2)):
        raise ValueError('Niepoprawna wersja statusu')
    keys = {'version', 'outcome', 'timestamp', 'recipient_digest'}
    if data['version'] == 2:
        keys.add('sender_scope')
    if (set(data) != keys
            or not isinstance(data['outcome'], str) or data['outcome'] not in (*_RANK, 'failed')
            or not isinstance(data['timestamp'], str) or not isinstance(data['recipient_digest'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', data['recipient_digest'])):
        raise ValueError('Niepoprawny status')
    scope = data.get('sender_scope')
    if data['version'] == 2 and (not isinstance(scope, str) or not re.fullmatch(r'[0-9a-f]{64}', scope)):
        raise ValueError('Niepoprawny routing')
    timestamp = data['timestamp']
    if utc_iso(parse_time(timestamp)) != timestamp or timestamp < '1970':
        raise ValueError('Niepoprawny czas')
    return data['outcome'], data['recipient_digest'], scope


class WhatsAppInbox:
    def __init__(self, store: NotificationStore, settings: WhatsAppWebhookSettings, *, max_events: int = 10000) -> None:
        if not isinstance(store, NotificationStore):
            raise ValueError('Niepoprawny magazyn powiadomień')
        self.max_events = bounded_int(max_events, 1, 100000)
        self._webhook = WhatsAppWebhook(settings)
        self._routing = WhatsAppRouting(settings.waba_id, settings.phone_number_id)
        self.store = store
        self.repo = store.repo
        self._conn = self.repo.connection
        self._cursor: tuple[str, int] | None = None

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._conn.in_transaction:
            raise RuntimeError('Inbox wymaga połączenia bez zewnętrznej transakcji')
        try:
            with self.repo.transaction():
                yield
        except BaseException:
            # The repository rolls back statement errors, but COMMIT itself may
            # fail (deferred FK/full disk). This transaction belongs only to us.
            if self._conn.in_transaction:
                self._conn.rollback()
            raise

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
                        'version': 2, 'outcome': event.outcome, 'timestamp': event.timestamp,
                        'recipient_digest': self._routing.recipient_digest(event.recipient_id),
                        'sender_scope': self._routing.scope,
                    }, separators=(',', ':')), stamp) for event in novel],
                )
        return len(novel)

    def _pending(self, limit: int, cursor: tuple[str, int] | None) -> list[sqlite3.Row]:
        after = '' if cursor is None else ' AND (received_at, id) > (?, ?)'
        return self._conn.execute(
            # Otherwise SQLite may choose (channel,event_key) and sort the whole
            # inbox for every batch. This ordered partial index is present since v13.
            "SELECT * FROM notification_webhook_events INDEXED BY ix_notification_webhook_pending"
            " WHERE processed_at IS NULL AND channel = 'whatsapp'"
            + after + ' ORDER BY received_at, id LIMIT ?', (*(() if cursor is None else cursor), limit),
        ).fetchall()

    def _match(self, provider_id: str, digest: str, scope: str | None) -> tuple[sqlite3.Row, ReportPart] | None:
        # v1 has no authenticated sender scope. Never guess from current config.
        if scope is None:
            return None
        # Detect collisions across ALL channels before considering ownership/recipient.
        rows = self._conn.execute(
            'SELECT * FROM notification_outbox WHERE provider_message_id = ? LIMIT 2', (provider_id,),
        ).fetchall()
        if len(rows) != 1:
            return None
        row = rows[0]
        if row['state'] not in (*_RANK, 'failed', 'unknown'):
            return None
        endpoint = self._conn.execute(
            'SELECT chat_id, channel FROM notification_endpoints WHERE id = ?', (row['endpoint_id'],),
        ).fetchone()
        if endpoint is None or endpoint['channel'] != 'whatsapp':
            return None
        route = self._conn.execute('SELECT * FROM whatsapp_dispatch_routes WHERE outbox_id = ?', (row['id'],)).fetchone()
        if route is None:
            return None
        try:
            part = ReportPart.from_json(row['payload'])
            if (part.chat_id != endpoint['chat_id'] or part.chat_id != route['chat_id']
                    or part.endpoint_version != route['endpoint_version']
                    or not hmac.compare_digest(scope, route['sender_scope'])
                    or not hmac.compare_digest(digest, route['recipient_digest'])):
                return None
        except (ValueError, TypeError, OverflowError):
            return None
        return row, part

    def _apply(self, row: sqlite3.Row, part: ReportPart, outcome: str, stamp: str) -> None:
        current = row['state']
        if outcome == 'failed':
            if current not in ('delivered', 'read'):
                self.store._mark(row['id'], 'failed')
                # The old message can fail after address/consent/settings changed.
                # Only quarantine the endpoint version that actually dispatched it.
                endpoint = self._conn.execute('SELECT version FROM notification_endpoints WHERE id = ?',
                                              (row['endpoint_id'],)).fetchone()
                if endpoint is not None and endpoint['version'] == part.endpoint_version:
                    self.store._quarantine(row['endpoint_id'])
            return
        if current == 'failed' and outcome == 'accepted':
            return
        if _RANK[outcome] > _RANK.get(current, 0):
            self.store._mark(row['id'], outcome)
        # ACK already recorded original revisions. Do not create histories or touch
        # another outbox, skipped decision, endpoint, or a newer lead revision.
        self._conn.executemany(
            'UPDATE notification_deliveries SET outcome = ?, processed_at = ?'
            ' WHERE outbox_id = ? AND endpoint_id = ? AND id_sprawy = ? AND revision = ?'
            " AND outcome IN ('accepted', 'delivered') AND (? = 'read' OR (? = 'delivered' AND outcome = 'accepted'))",
            [(outcome, stamp, row['id'], row['endpoint_id'], ref.id_sprawy, ref.revision, outcome, outcome)
             for ref in part.leads],
        )

    def reconcile(self, *, limit: int = 100) -> int:
        """Bounded fair sweep; unmatched events wait for an ACK or retention.

        Delivery bookkeeping may finish after pause/revocation. It never authorizes
        a new send, reenables an endpoint or resolves a lost ACK by phone alone.
        """
        bounded_int(limit, 1, 100)
        with self._transaction():
            rows = self._pending(limit, self._cursor)
            if not rows and self._cursor is not None:
                rows = self._pending(limit, None)
            processed = 0
            stamp = utc_iso(self.repo.now())
            for event in rows:
                try:
                    outcome, digest, scope = _decode(event)
                except (ValueError, TypeError, AttributeError, OverflowError):
                    self._conn.execute('UPDATE notification_webhook_events SET processed_at = ? WHERE id = ?',
                                       (stamp, event['id']))
                else:
                    match = self._match(event['provider_message_id'], digest, scope)
                    if match is None:
                        continue
                    row, part = match
                    self._apply(row, part, outcome, stamp)
                    self._conn.execute(
                        'UPDATE notification_webhook_events SET endpoint_id = ?, processed_at = ? WHERE id = ?',
                        (row['endpoint_id'], stamp, event['id']),
                    )
                processed += 1
            cursor = (rows[-1]['received_at'], rows[-1]['id']) if rows else None
        # Do not skip work if any statement or COMMIT failed.
        self._cursor = cursor
        return processed
