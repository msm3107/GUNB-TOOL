"""Ograniczony cykl workera z wstrzykniętym nadawcą; bez produkcyjnego adaptera i wątku."""

from __future__ import annotations

import logging
from typing import Mapping, Protocol

from .notification_models import CHANNELS, DeliveryResult, NotificationEndpoint, ReportPart, bounded_int
from .notification_store import NotificationStore

log = logging.getLogger(__name__)


class Sender(Protocol):
    def send(self, endpoint: NotificationEndpoint, report: ReportPart, *, idempotency_key: str) -> DeliveryResult:
        """Timeout < lease; retry tylko jeśli wiadomość na pewno nie została przyjęta.

        Klucz przekazujemy stabilnie; faktyczne wsparcie idempotencji zależy od dostawcy.
        Adapter nie może zapisywać adresów, body, sekretów ani surowych błędów w logach.
        """
        ...


class NotificationWorker:
    def __init__(self, store: NotificationStore, senders: Mapping[str, Sender], *, max_attempts: int = 5) -> None:
        if any(channel not in CHANNELS or not callable(getattr(sender, "send", None)) for channel, sender in senders.items()):
            raise ValueError("Niepoprawny rejestr nadawców")
        self.store = store
        self.senders = dict(senders)
        self.max_attempts = bounded_int(max_attempts, 1, 10)

    def run_once(self, *, limit: int = 25) -> int:
        """Do 100 przejęć; repozytorium musi należeć do wywołującego wątku."""
        bounded_int(limit, 1, 100)
        if self.store.repo.connection.in_transaction:
            raise RuntimeError("Worker wymaga połączenia bez zewnętrznej transakcji")
        handled = 0
        for _ in range(limit):
            claim = self.store.claim(tuple(self.senders))
            if claim is None:
                break
            handled += 1
            prepared = self.store.prepare_send(claim)
            if prepared is None:
                continue
            endpoint, report = prepared
            try:
                result = self.senders[endpoint.channel].send(endpoint, report, idempotency_key=claim.idempotency_key)
                if not isinstance(result, DeliveryResult):
                    result = DeliveryResult("unknown")
            except Exception:
                # Timeout mógł wystąpić po przyjęciu. Bez tracebacku/surowej treści błędu dostawcy.
                log.warning("Niepewny wynik nadawcy: zadanie %s", claim.id)
                result = DeliveryResult("unknown")
            self.store.complete(claim, result, max_attempts=self.max_attempts)
        return handled
