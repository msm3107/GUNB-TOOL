"""Internal verification API: no HTTP/bot endpoint and no automatic activation."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import hmac
import re
import secrets
from typing import Iterator, Protocol

from .notification_models import DeliveryResult, NotificationEndpoint, parse_time, utc_iso
from .notification_store import NotificationStore, normalize_address


class VerificationSender(Protocol):
    def send_verification(self, endpoint: NotificationEndpoint, token: str, *, expires_at: datetime,
                          idempotency_key: str) -> DeliveryResult: ...


class EmailVerification:
    def __init__(self, store: NotificationStore, sender: VerificationSender) -> None:
        if not callable(getattr(sender, 'send_verification', None)):
            raise ValueError('Niepoprawny nadawca weryfikacji')
        self.store, self.sender = store, sender
        self.repo = store.repo
        self._conn = self.repo.connection

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self._conn.in_transaction:
            raise RuntimeError('Weryfikacja wymaga połączenia bez zewnętrznej transakcji')
        with self.repo.transaction():
            yield

    def _eligible(self, chat_id: int, endpoint_id: int) -> NotificationEndpoint | None:
        try:
            endpoint = self.store.get_endpoint(chat_id, endpoint_id)
            if (endpoint.channel != 'email' or endpoint.enabled or endpoint.verified_at is not None
                    or normalize_address('email', endpoint.address) != endpoint.address):
                return None
            user = self.store._bot.get_user(chat_id)
            if user is None or user.status != 'aktywny' or user.wstrzymane:
                return None
            if self.store.access == 'open' or chat_id in self.store.admins or user.bez_limitu:
                return endpoint
            if user.is_active and parse_time(user.subscription_ends or '') > self.repo.now():
                return endpoint
        except (ValueError, TypeError):
            pass
        return None

    @staticmethod
    def _address_digest(endpoint: NotificationEndpoint) -> str:
        # Case folding for rate limits only; never change actual local-part delivery.
        return hashlib.sha256(endpoint.address.lower().encode('ascii')).hexdigest()

    def _limited(self, chat_id: int, address_digest: str) -> bool:
        now = self.repo.now()
        since = utc_iso(now - timedelta(hours=1))
        for field, value in (('chat_id', chat_id), ('address_digest', address_digest)):
            # Column names are a closed internal list, not caller input.
            row = self._conn.execute(
                f'SELECT COUNT(*) AS count, MAX(issued_at) AS last FROM email_verifications WHERE {field} = ? AND issued_at > ?',
                (value, since),
            ).fetchone()
            if row['count'] >= 3:
                return True
            if row['last']:
                try:
                    if now < parse_time(row['last']) + timedelta(seconds=60):
                        return True
                except (ValueError, TypeError):
                    return True
        return self._conn.execute('SELECT COUNT(*) FROM email_verifications WHERE issued_at > ?', (since,)).fetchone()[0] >= 100

    def request(self, chat_id: int, endpoint_id: int) -> DeliveryResult | None:
        """Trusted caller only. Future UI must not expose SMTP/existence diagnostics."""
        with self._transaction():
            endpoint = self._eligible(chat_id, endpoint_id)
            if endpoint is None:
                return None
            now = self.repo.now()
            stamp = utc_iso(now)
            self._conn.execute(
                'DELETE FROM email_verifications WHERE id IN'
                ' (SELECT id FROM email_verifications WHERE issued_at < ? ORDER BY issued_at, id LIMIT 200)',
                (utc_iso(now - timedelta(days=7)),),
            )
            address_digest = self._address_digest(endpoint)
            if self._limited(chat_id, address_digest):
                return None
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode('ascii')).hexdigest()
            expiry = now + timedelta(seconds=900)
            self._conn.execute(
                'UPDATE email_verifications SET invalidated_at = ?'
                ' WHERE endpoint_id = ? AND consumed_at IS NULL AND invalidated_at IS NULL', (stamp, endpoint.id),
            )
            ident = self._conn.execute(
                'INSERT INTO email_verifications (chat_id, endpoint_id, endpoint_version, address_digest, token_digest, issued_at, expires_at)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?)',
                (chat_id, endpoint.id, endpoint.version, address_digest, digest, stamp, utc_iso(expiry)),
            ).lastrowid
        try:
            result = self.sender.send_verification(endpoint, token, expires_at=expiry,
                                                   idempotency_key=f'email-verification:{ident}:{digest}')
            if not isinstance(result, DeliveryResult):
                result = DeliveryResult('unknown')
        except Exception:
            result = DeliveryResult('unknown')  # Never log raw provider exception or the token.
        if result.outcome in ('retry', 'failed'):
            with self._transaction():
                self._conn.execute(
                    'UPDATE email_verifications SET invalidated_at = ? WHERE id = ? AND consumed_at IS NULL',
                    (utc_iso(self.repo.now()), ident),
                )
        return result

    def consume(self, chat_id: int, endpoint_id: int, token: str) -> bool:
        with self._transaction():
            endpoint = self._eligible(chat_id, endpoint_id)
            if endpoint is None or not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
                return False
            row = self._conn.execute(
                'SELECT * FROM email_verifications WHERE endpoint_id = ? ORDER BY id DESC LIMIT 1', (endpoint.id,),
            ).fetchone()
            if (row is None or row['chat_id'] != chat_id or row['endpoint_version'] != endpoint.version
                    or row['address_digest'] != self._address_digest(endpoint) or row['consumed_at'] is not None
                    or row['invalidated_at'] is not None or row['attempts'] >= 5):
                return False
            now = self.repo.now()
            try:
                if not parse_time(row['issued_at']) <= now < parse_time(row['expires_at']):
                    return False
            except (ValueError, TypeError):
                return False
            digest = hashlib.sha256(token.encode('ascii')).hexdigest()
            stamp = utc_iso(now)
            if not hmac.compare_digest(row['token_digest'], digest):
                self._conn.execute(
                    'UPDATE email_verifications SET attempts = attempts + 1,'
                    ' invalidated_at = CASE WHEN attempts >= 4 THEN ? ELSE invalidated_at END WHERE id = ?', (stamp, row['id']),
                )
                return False
            self._conn.execute('UPDATE email_verifications SET consumed_at = ? WHERE id = ?', (stamp, row['id']))
            self._conn.execute('UPDATE notification_endpoints SET verified_at = ?, updated_at = ? WHERE id = ?',
                               (stamp, stamp, endpoint.id))
            return True
