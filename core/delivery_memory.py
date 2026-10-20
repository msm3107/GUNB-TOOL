"""Klasyfikacja inwestycji względem historii dostaw dla zamówienia B2B."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from .models import DeliveryCategory, Investment
from .storage import LeadRepository
from .text import normalize_text


@dataclass(frozen=True)
class ClassifiedDeliveryItem:
    """Inwestycja zaklasyfikowana do bieżącej paczki wraz z zapisywaną rewizją."""

    investment: Investment
    revision: str
    category: DeliveryCategory


class DeliveryClassifier:
    """Porównuje filtrowane inwestycje z ostatnimi doręczonymi stanami zamówienia.

    Klasyfikator nie zapisuje historii. Warstwa realizująca dostawę powinna utrwalić
    ``revision`` dopiero po jej doręczeniu, w ``b2b_delivery_items``.
    """

    _HISTORY_SQL = """
        SELECT item.id_sprawy, item.rewizja
        FROM b2b_deliveries AS delivery
        JOIN b2b_delivery_items AS item USING (delivery_id)
        WHERE delivery.order_id = ?
          AND delivery.doreczono_utc IS NOT NULL
        ORDER BY delivery.doreczono_utc DESC, delivery.delivery_id DESC
    """

    def __init__(self, repository: LeadRepository) -> None:
        self._repository = repository

    @staticmethod
    def revision_for(investment: Investment) -> str:
        """Wyznacza rewizję ze statusu, daty zdarzenia i opisu nieznanego statusu."""
        unknown_status_description = (
            normalize_text(investment.status_opis) if investment.status == "inny" else None
        )
        state = json.dumps(
            [investment.status, unknown_status_description, investment.data_aktualizacji],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(state.encode("utf-8")).hexdigest()

    def classify(
        self,
        order_id: int,
        investments: Iterable[Investment],
        *,
        full: bool = False,
    ) -> tuple[ClassifiedDeliveryItem, ...]:
        """Zwraca nowe/zmienione sprawy albo wszystkie sprawy dla pełnej paczki.

        Zwykła dostawa pomija niezmienione sprawy, które klient już otrzymał.
        Historia jest czytana jednym zapytaniem dla całego zamówienia.
        """
        candidates = tuple(investments)
        rows = self._repository.connection.execute(self._HISTORY_SQL, (order_id,)).fetchall()

        latest_revisions: dict[str, str] = {}
        for row in rows:
            id_sprawy, revision = row[0], row[1]
            if id_sprawy not in latest_revisions:
                latest_revisions[id_sprawy] = revision

        result: list[ClassifiedDeliveryItem] = []
        for investment in candidates:
            revision = self.revision_for(investment)
            previous_revision = latest_revisions.get(investment.id_sprawy)

            if full:
                category = DeliveryCategory.FULL
            elif previous_revision is None:
                category = DeliveryCategory.NEW
            elif previous_revision != revision:
                category = DeliveryCategory.CHANGE
            else:
                continue

            result.append(ClassifiedDeliveryItem(investment, revision, category))

        return tuple(result)
