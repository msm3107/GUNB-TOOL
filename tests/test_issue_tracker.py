"""Customer issue reporting uses synthetic leads and temporary SQLite only."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from core.issue_tracker import (
    IssueNotAllowed,
    IssueRateLimitExceeded,
    IssueTracker,
    IssueType,
)
from core.models import Investment
from core.storage import LeadRepository


def deliver_case(repo, case_id="CASE/1", *, customer_id=1, delivered=True):
    repo.connection.execute(
        "INSERT OR IGNORE INTO b2b_customers (customer_id, email) VALUES (?, ?)",
        (customer_id, f"customer-{customer_id}@example.test"),
    )
    repo.upsert(Investment(id_sprawy=case_id, zrodlo="pozwolenia", status="decyzja"))
    order = repo.connection.execute(
        "INSERT INTO b2b_orders (customer_id, typ_pakietu) VALUES (?, 'jednorazowy')", (customer_id,)
    )
    delivery = repo.connection.execute(
        "INSERT INTO b2b_deliveries (order_id, typ_dostawy, doreczono_utc) VALUES (?, 'pelna', ?)",
        (order.lastrowid, "2026-10-01T00:00:00+00:00" if delivered else None),
    )
    repo.connection.execute(
        "INSERT INTO b2b_delivery_items (delivery_id, id_sprawy, rewizja, kategoria_dostawy) "
        "VALUES (?, ?, 'revision', 'full')", (delivery.lastrowid, case_id)
    )


@pytest.fixture
def tracker(repo):
    deliver_case(repo)
    return IssueTracker(repo)


@pytest.mark.parametrize("kind", (
    "zly_telefon", "to_nie_inwestor_tylko_projektant", "inwestycja_juz_stoi", "inna_uwaga"
))
def test_report_persists_each_issue_type_and_sets_unverified_label(tracker, repo, clock, kind):
    result = tracker.report_issue(1, "CASE/1", kind, "  Błędny\x00 telefon\n proszę\t sprawdzić  ")

    assert result.issue_id > 0
    assert result.customer_id == 1 and result.id_sprawy == "CASE/1"
    assert result.issue_type == IssueType(kind)
    assert result.description == "Błędny telefon proszę sprawdzić"
    assert result.created_at == clock.now_utc().isoformat(timespec="seconds")
    row = repo.connection.execute("SELECT * FROM issue_reports").fetchone()
    assert row["opis"] == result.description and row["typ"] == kind
    stored = repo.get("CASE/1")
    assert stored.wymaga_weryfikacji is True
    assert stored.data_quality_label == "Zgłoszona uwaga — wymaga weryfikacji"
    assert stored.status == "decyzja"
    assert len(repo.status_history("CASE/1")) == 1
    assert not repo.connection.in_transaction


@pytest.mark.parametrize("description", (None, 123, "x" * 1001, " \t\x00\n", "\ud800\ufffe"))
def test_invalid_description_leaves_no_report_or_flag(tracker, repo, description):
    with pytest.raises(ValueError):
        tracker.report_issue(1, "CASE/1", "inna_uwaga", description)
    assert repo.connection.execute("SELECT COUNT(*) FROM issue_reports").fetchone()[0] == 0
    assert repo.get("CASE/1").wymaga_weryfikacji is False


def test_description_boundary_and_plain_text_are_preserved(tracker, repo):
    description = "<script>literal text</script>'; DROP TABLE investments; --"
    result = tracker.report_issue(1, "CASE/1", IssueType.OTHER, description)
    assert result.description == description
    assert repo.get("CASE/1") is not None
    result = tracker.report_issue(1, "CASE/1", IssueType.WRONG_PHONE, "Ł" * 1000)
    assert result.description == "Ł" * 1000


@pytest.mark.parametrize("customer_id,case_id,kind", (
    (True, "CASE/1", "zly_telefon"), (0, "CASE/1", "zly_telefon"),
    (-1, "CASE/1", "zly_telefon"), ("1", "CASE/1", "zly_telefon"),
    (2**63, "CASE/1", "zly_telefon"), (1, None, "zly_telefon"),
    (1, " ", "zly_telefon"), (1, "x" * 513, "zly_telefon"),
    (1, "\ud800", "zly_telefon"),
    (1, "CASE/1", "unknown"), (1, "CASE/1", None),
))
def test_invalid_identifiers_or_type_are_rejected(tracker, repo, customer_id, case_id, kind):
    with pytest.raises(ValueError):
        tracker.report_issue(customer_id, case_id, kind)
    assert repo.connection.execute("SELECT COUNT(*) FROM issue_reports").fetchone()[0] == 0
    assert repo.get("CASE/1").wymaga_weryfikacji is False


@pytest.mark.parametrize("customer_id,case_id", (
    (1, "MISSING"), (1, "CASE/PENDING"), (1, "CASE/OTHER"), (2, "CASE/1"), (999, "CASE/1")
))
def test_only_cases_delivered_to_this_customer_can_be_reported(tracker, repo, customer_id, case_id):
    deliver_case(repo, "CASE/PENDING", delivered=False)
    deliver_case(repo, "CASE/OTHER", customer_id=2)
    with pytest.raises(IssueNotAllowed):
        tracker.report_issue(customer_id, case_id, "zly_telefon")
    assert repo.connection.execute("SELECT COUNT(*) FROM issue_reports").fetchone()[0] == 0
    assert repo.connection.execute("SELECT SUM(wymaga_weryfikacji) FROM investments").fetchone()[0] == 0


def test_customer_can_report_a_case_from_any_of_their_orders(tracker, repo):
    deliver_case(repo, "CASE/SECOND-ORDER")
    report = tracker.report_issue(1, "CASE/SECOND-ORDER", "inwestycja_juz_stoi")
    assert report.id_sprawy == "CASE/SECOND-ORDER"


def test_duplicate_retry_returns_original_without_editing_description(tracker, repo, clock):
    first = tracker.report_issue(1, "CASE/1", "zly_telefon", "Pierwsza uwaga")
    clock.advance(hours=2)
    retry = tracker.report_issue(1, "CASE/1", "zly_telefon", "Inny opis")
    assert retry == first
    assert repo.connection.execute("SELECT COUNT(*) FROM issue_reports").fetchone()[0] == 1


def test_different_customers_keep_separate_report_bodies(tracker, repo):
    deliver_case(repo, "CASE/1", customer_id=2)
    first = tracker.report_issue(1, "CASE/1", "zly_telefon", "Prywatna uwaga A")
    second = tracker.report_issue(2, "CASE/1", "zly_telefon", "Prywatna uwaga B")
    assert first.issue_id != second.issue_id
    assert second.customer_id == 2 and second.description == "Prywatna uwaga B"
    assert repo.get("CASE/1").data_quality_label == "Zgłoszona uwaga — wymaga weryfikacji"


def test_hourly_limit_is_per_customer_and_duplicates_do_not_consume_it(tracker, repo, clock):
    for index in range(20):
        deliver_case(repo, f"CASE/LIMIT/{index}")
        tracker.report_issue(1, f"CASE/LIMIT/{index}", "zly_telefon")
    deliver_case(repo, "CASE/NEXT")
    with pytest.raises(IssueRateLimitExceeded):
        tracker.report_issue(1, "CASE/NEXT", "zly_telefon")
    retry = tracker.report_issue(1, "CASE/LIMIT/0", "zly_telefon")
    assert retry.id_sprawy == "CASE/LIMIT/0"
    assert repo.get("CASE/NEXT").wymaga_weryfikacji is False
    deliver_case(repo, "CASE/NEXT", customer_id=2)
    assert tracker.report_issue(2, "CASE/NEXT", "zly_telefon").customer_id == 2
    clock.advance(minutes=59, seconds=59)
    with pytest.raises(IssueRateLimitExceeded):
        tracker.report_issue(1, "CASE/NEXT", "zly_telefon")
    clock.advance(seconds=1)
    assert tracker.report_issue(1, "CASE/NEXT", "zly_telefon").customer_id == 1


def test_failed_flag_write_rolls_back_report(tracker, repo):
    repo.connection.execute(
        "CREATE TRIGGER fail_quality BEFORE UPDATE OF wymaga_weryfikacji ON investments "
        "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        tracker.report_issue(1, "CASE/1", "zly_telefon")
    assert repo.connection.execute("SELECT COUNT(*) FROM issue_reports").fetchone()[0] == 0
    assert repo.get("CASE/1").wymaga_weryfikacji is False
    assert not repo.connection.in_transaction
    repo.connection.execute("DROP TRIGGER fail_quality")
    assert tracker.report_issue(1, "CASE/1", "zly_telefon").issue_id > 0


def test_failed_report_is_atomic_when_caller_catches_error_in_outer_transaction(tracker, repo):
    repo.connection.execute(
        "CREATE TRIGGER fail_quality BEFORE UPDATE OF wymaga_weryfikacji ON investments "
        "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
    )
    with repo.transaction():
        with pytest.raises(sqlite3.IntegrityError):
            tracker.report_issue(1, "CASE/1", "zly_telefon")
        assert repo.connection.execute("SELECT COUNT(*) FROM issue_reports").fetchone()[0] == 0
    assert repo.get("CASE/1").wymaga_weryfikacji is False


def submit_concurrently(db_path, clock, case_ids):
    barrier = Barrier(2)

    def submit(case_id):
        with LeadRepository(db_path, now=clock.now_utc) as repository:
            barrier.wait(timeout=10)
            try:
                return IssueTracker(repository).report_issue(1, case_id, "zly_telefon")
            except IssueRateLimitExceeded:
                return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(submit, case_id) for case_id in case_ids]
        return [future.result(timeout=15) for future in futures]


def test_concurrent_connections_do_not_exceed_hourly_limit(tmp_path, clock):
    path = tmp_path / "concurrent.sqlite"
    with LeadRepository(path, now=clock.now_utc) as repository:
        tracker = IssueTracker(repository)
        for index in range(21):
            deliver_case(repository, f"CASE/{index}")
            if index < 19:
                tracker.report_issue(1, f"CASE/{index}", "zly_telefon")
    results = submit_concurrently(path, clock, ["CASE/19", "CASE/20"])
    assert sum(result is not None for result in results) == 1
    with LeadRepository(path) as repository:
        assert repository.connection.execute("SELECT COUNT(*) FROM issue_reports").fetchone()[0] == 20
        assert repository.connection.execute("SELECT SUM(wymaga_weryfikacji) FROM investments").fetchone()[0] == 20


def test_concurrent_duplicate_reports_are_idempotent(tmp_path, clock):
    path = tmp_path / "duplicate.sqlite"
    with LeadRepository(path, now=clock.now_utc) as repository:
        deliver_case(repository)
    results = submit_concurrently(path, clock, ["CASE/1", "CASE/1"])
    assert results[0] == results[1]
    with LeadRepository(path) as repository:
        assert repository.connection.execute("SELECT COUNT(*) FROM issue_reports").fetchone()[0] == 1
