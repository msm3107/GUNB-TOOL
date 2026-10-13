"""Request-owned file-backed WAL/FULL inbox; no listener or database initialization."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .notification_models import bounded_int
from .notification_store import NotificationStore
from .storage import LeadRepository
from .whatsapp_inbox import WhatsAppInbox
from .whatsapp_webhook import WhatsAppWebhookSettings


class WhatsAppInboxFactory:
    def __init__(self, db_path: str | Path, settings: WhatsAppWebhookSettings, *, max_events: int = 10000) -> None:
        try:
            if (not isinstance(db_path, (str, Path)) or not str(db_path)
                    or str(db_path) == ':memory:' or str(db_path).lower().startswith('file:')
                    or str(db_path).startswith(('\\\\', '//')) or not isinstance(settings, WhatsAppWebhookSettings)):
                raise ValueError
            self._path = Path(db_path).absolute()
            self._max_events = bounded_int(max_events, 1, 100000)
        except (ValueError, TypeError, OSError):
            raise ValueError('Niepoprawna konfiguracja fabryki inboxa WhatsApp') from None
        self._settings = settings

    @contextmanager
    def __call__(self) -> Iterator[WhatsAppInbox]:
        # Fresh connection in the calling thread; no schema setup or shared state.
        with LeadRepository(self._path, existing_only=True) as repo:
            conn = repo.connection
            if conn.in_transaction:
                raise RuntimeError('Niepoprawny stan połączenia inboxa')
            conn.execute('PRAGMA synchronous = FULL')
            if conn.execute('PRAGMA synchronous').fetchone()[0] != 2 or repo.journal_mode != 'wal':
                raise RuntimeError('Wymagany trwały zapis inboxa')
            yield WhatsAppInbox(NotificationStore(repo), self._settings, max_events=self._max_events)
