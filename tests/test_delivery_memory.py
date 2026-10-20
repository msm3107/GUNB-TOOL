from __future__ import annotations

import pytest

from core.delivery_memory import DeliveryClassifier
from core.models import DeliveryCategory, Investment


def investment(
    id_sprawy: str = "CASE/1",
    *,
    status: str = "wniosek",
    status_opis: str | None = None,
    data_aktualizacji: str | None = "2026-09-01",
    nazwa_zamierzenia: str = "Budowa domu",
) -> Investment:
    return Investment(
        id_sprawy=id_sprawy,
        zrodlo="pozwolenia",
        status=status,
        status_opis=status_opis,
        data_aktualizacji=data_aktualizacji,
        nazwa_zamierzenia=nazwa_zamierzenia,
    )


def add_order(repo, order_id: int = 1) -> None:
    repo.connection.execute(
        "INSERT OR IGNORE INTO b2b_customers (customer_id, email) VALUES (1, 'firma@example.pl')"
    )
    repo.connection.execute(
        "INSERT INTO b2b_orders (order_id, customer_id, typ_pakietu) VALUES (?, 1, 'abonament')",
        (order_id,),
    )


def record_delivery(repo, order_id: int, item: Investment, *, delivered: bool = True) -> None:
    repo.upsert(item)
    cursor = repo.connection.execute(
        "INSERT INTO b2b_deliveries (order_id, typ_dostawy, doreczono_utc) VALUES (?, 'delta', ?)",
        (order_id, "2026-10-01T10:00:00Z" if delivered else None),
    )
    repo.connection.execute(
        "INSERT INTO b2b_delivery_items (delivery_id, id_sprawy, rewizja, kategoria_dostawy) "
        "VALUES (?, ?, ?, 'new')",
        (cursor.lastrowid, item.id_sprawy, DeliveryClassifier.revision_for(item)),
    )


def test_unseen_investment_is_classified_as_new(repo):
    classifier = DeliveryClassifier(repo)

    result = classifier.classify(1, [investment()])

    assert [(item.investment.id_sprawy, item.category) for item in result] == [
        ("CASE/1", DeliveryCategory.NEW),
    ]
    assert result[0].revision == DeliveryClassifier.revision_for(investment())


def test_delivered_investment_is_not_returned_again_for_same_order(repo):
    add_order(repo)
    already_delivered = investment()
    record_delivery(repo, 1, already_delivered)
    classifier = DeliveryClassifier(repo)

    result = classifier.classify(1, [already_delivered, investment("CASE/2")])

    assert [(item.investment.id_sprawy, item.category) for item in result] == [
        ("CASE/2", DeliveryCategory.NEW),
    ]


@pytest.mark.parametrize(
    "changed",
    [
        investment(status="decyzja"),
        investment(data_aktualizacji="2026-09-02"),
    ],
    ids=["status", "event-date"],
)
def test_status_or_event_date_change_is_classified_as_change(repo, changed):
    add_order(repo)
    record_delivery(repo, 1, investment())

    result = DeliveryClassifier(repo).classify(1, [changed])

    assert [(item.investment.id_sprawy, item.category) for item in result] == [
        ("CASE/1", DeliveryCategory.CHANGE),
    ]


def test_changed_unknown_status_description_is_classified_as_change(repo):
    add_order(repo)
    record_delivery(repo, 1, investment(status="inny", status_opis="Nieznany etap A"))

    changed = investment(status="inny", status_opis="Nieznany etap B")
    result = DeliveryClassifier(repo).classify(1, [changed])

    assert [(item.investment.id_sprawy, item.category) for item in result] == [
        ("CASE/1", DeliveryCategory.CHANGE),
    ]


def test_formatting_only_change_in_unknown_status_description_is_ignored(repo):
    add_order(repo)
    record_delivery(repo, 1, investment(status="inny", status_opis="Nieznany etap A"))

    reformatted = investment(status="inny", status_opis=" NIEZNANY   ETAP A ")

    assert DeliveryClassifier(repo).classify(1, [reformatted]) == ()


def test_unrelated_field_change_does_not_create_delivery_revision(repo):
    add_order(repo)
    record_delivery(repo, 1, investment())
    updated_description = investment(nazwa_zamierzenia="Inny opis inwestycji")

    result = DeliveryClassifier(repo).classify(1, [updated_description])

    assert result == ()


def test_full_delivery_includes_previously_delivered_investments(repo):
    add_order(repo)
    already_delivered = investment()
    record_delivery(repo, 1, already_delivered)

    result = DeliveryClassifier(repo).classify(1, [already_delivered], full=True)

    assert [(item.investment.id_sprawy, item.category) for item in result] == [
        ("CASE/1", DeliveryCategory.FULL),
    ]


def test_undelivered_items_do_not_count_as_delivery_history(repo):
    add_order(repo)
    candidate = investment()
    record_delivery(repo, 1, candidate, delivered=False)

    result = DeliveryClassifier(repo).classify(1, [candidate])

    assert [(item.investment.id_sprawy, item.category) for item in result] == [
        ("CASE/1", DeliveryCategory.NEW),
    ]


def test_history_lookup_uses_one_query_for_many_investments(repo):
    investments = [investment(f"CASE/{index}") for index in range(25)]
    statements: list[str] = []
    repo.connection.set_trace_callback(statements.append)

    DeliveryClassifier(repo).classify(1, investments)

    history_queries = [
        statement
        for statement in statements
        if "B2B_DELIVERY_ITEMS" in statement.upper() and "SELECT" in statement.upper()
    ]
    assert len(history_queries) == 1
