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


def add_order(repo, order_id: int = 1, *, customer_id: int = 1) -> None:
    repo.connection.execute(
        "INSERT OR IGNORE INTO b2b_customers (customer_id, email) VALUES (?, ?)",
        (customer_id, f"firma{customer_id}@example.pl"),
    )
    repo.connection.execute(
        "INSERT INTO b2b_orders (order_id, customer_id, typ_pakietu) VALUES (?, ?, 'abonament')",
        (order_id, customer_id),
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


def test_delivery_history_is_shared_across_orders_for_the_same_customer(repo):
    add_order(repo, 1, customer_id=1)
    add_order(repo, 2, customer_id=1)
    add_order(repo, 3, customer_id=2)
    unchanged = investment("CASE/UNCHANGED")
    changed_before = investment("CASE/CHANGED")
    record_delivery(repo, 1, unchanged)
    record_delivery(repo, 1, changed_before)

    second_order = DeliveryClassifier(repo).classify(
        2,
        [
            unchanged,
            investment("CASE/CHANGED", status="decyzja"),
            investment("CASE/NEW"),
        ],
    )

    assert [(item.investment.id_sprawy, item.category) for item in second_order] == [
        ("CASE/CHANGED", DeliveryCategory.CHANGE),
        ("CASE/NEW", DeliveryCategory.NEW),
    ]
    other_customer = DeliveryClassifier(repo).classify(3, [unchanged])
    assert [(item.investment.id_sprawy, item.category) for item in other_customer] == [
        ("CASE/UNCHANGED", DeliveryCategory.NEW),
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


def test_full_delivery_carries_batched_status_history(repo):
    candidates = []
    for index in range(25):
        case_id = f"CASE/HISTORY/{index}"
        repo.upsert(investment(case_id, status="wniosek"))
        latest_status = investment(case_id, status="decyzja")
        repo.upsert(latest_status)
        candidates.append(latest_status)
    statements: list[str] = []
    repo.connection.set_trace_callback(statements.append)

    result = DeliveryClassifier(repo).classify(1, candidates, full=True)

    assert len(result) == len(candidates)
    assert all(
        [(row.stary_status, row.nowy_status) for row in item.status_history]
        == [(None, "wniosek"), ("wniosek", "decyzja")]
        for item in result
    )
    assert sum("FROM STATUS_HISTORY" in statement.upper() for statement in statements) == 1


@pytest.mark.parametrize("full, investments", [(False, []), (True, [investment()])])
def test_empty_delta_and_full_delivery_skip_history_lookup(repo, full, investments):
    statements: list[str] = []
    repo.connection.set_trace_callback(statements.append)

    DeliveryClassifier(repo).classify(1, investments, full=full)

    assert not any(
        "B2B_DELIVERY_ITEMS" in statement.upper() and "SELECT" in statement.upper()
        for statement in statements
    )


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
    assert "ID_SPRAWY IN" in history_queries[0].upper()
