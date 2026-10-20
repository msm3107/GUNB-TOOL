"""Klasyfikacja inwestycji względem historii dostaw dla zamówienia B2B."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

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
        SELECT id_sprawy, rewizja
        FROM (
            SELECT item.id_sprawy, item.rewizja,
                   ROW_NUMBER() OVER (
                       PARTITION BY item.id_sprawy
                       ORDER BY delivery.doreczono_utc DESC, delivery.delivery_id DESC
                   ) AS revision_rank
            FROM b2b_orders AS current_order
            JOIN b2b_orders AS customer_order
              ON customer_order.customer_id = current_order.customer_id
            JOIN b2b_deliveries AS delivery ON delivery.order_id = customer_order.order_id
            JOIN b2b_delivery_items AS item USING (delivery_id)
            WHERE current_order.order_id = ?
              AND delivery.doreczono_utc IS NOT NULL
              AND item.id_sprawy IN ({case_placeholders})
        )
        WHERE revision_rank = 1
    """
    _HISTORY_CASE_CHUNK_SIZE = 500

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
        latest_revisions = (
            self._latest_revisions(order_id, candidates) if candidates and not full else {}
        )

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

    def _latest_revisions(
        self,
        order_id: int,
        candidates: tuple[Investment, ...],
    ) -> dict[str, str]:
        case_ids = tuple(dict.fromkeys(item.id_sprawy for item in candidates))
        latest_revisions: dict[str, str] = {}
        connection = self._repository.connection

        for start in range(0, len(case_ids), self._HISTORY_CASE_CHUNK_SIZE):
            chunk = case_ids[start : start + self._HISTORY_CASE_CHUNK_SIZE]
            placeholders = ", ".join("?" for _ in chunk)
            sql = self._HISTORY_SQL.format(case_placeholders=placeholders)
            rows = connection.execute(sql, (order_id, *chunk)).fetchall()
            latest_revisions.update({row[0]: row[1] for row in rows})

        return latest_revisions
