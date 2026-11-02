import sqlite3
from datetime import timedelta

import pytest
from openpyxl import load_workbook

from core.instance import database_replacement_lock
from core.models import Investment
from core.paid_packages import PaidPackagePreparer
from core.storage import LeadRepository
from tests.test_payment_store import NOW, record
from tests.test_payment_store import payment_fixture as _payment_fixture  # noqa: F401


def add_cases(path):
    with LeadRepository(path, existing_only=True) as repo:
        for case, county in (
            ("MATCH", "2814"),
            ("OUTSIDE", "1465"),
            ("OTHERCOUNTY", "2801"),
        ):
            repo.upsert(
                Investment(
                    id_sprawy=case,
                    zrodlo="pozwolenia",
                    status="wniosek",
                    powiat_teryt=county,
                    kategoria="komercyjna",
                    nazwa_zamierzenia="Fikcyjna budowa",
                )
            )


def test_paid_job_builds_real_files_once_without_acknowledging_delivery(
    payment, tmp_path
):
    store, paid = payment
    add_cases(store.path)
    with sqlite3.connect(store.path) as c:
        filters = '{"wojewodztwo":"28","branza":"materialy","powiaty":["2814"]}'
        c.execute("UPDATE checkout_attempts SET filters_json=?", (filters,))
        c.execute("UPDATE b2b_orders SET filtry_json=?", (filters,))
    record(store, paid)
    output = tmp_path / "private"
    builder = PaidPackagePreparer(store.path, output, now=lambda: NOW)
    assert builder.prepare(1) is True
    assert builder.prepare(1) is False
    assert builder.run_pending() == 0
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT state,delivery_id FROM paid_package_jobs"
        ).fetchone() == ("ready", 1)
        delivered, xlsx, csv = c.execute(
            "SELECT doreczono_utc,plik_xlsx,plik_csv FROM b2b_deliveries"
        ).fetchone()
        assert delivered is None
        assert c.execute(
            "SELECT id_sprawy,kategoria_dostawy FROM b2b_delivery_items"
        ).fetchall() == [("MATCH", "new")]
    with open(output / xlsx, "rb") as f:
        book = load_workbook(f, read_only=True)
        assert book.sheetnames == ["1. Nowe", "2. Zmiany", "3. Pełna baza"]
        assert list(book.worksheets[0].values)[1][0] == "MATCH"
        book.close()
    assert (output / csv).read_bytes().startswith(b"\xef\xbb\xbf")
    assert len(list(output.iterdir())) == 2


def test_renderer_has_no_db_writer_or_replacement_lock_and_failure_can_retry(
    payment, tmp_path, monkeypatch
):
    import core.paid_packages as module

    store, paid = payment
    record(store, paid)
    real = module.write_xlsx

    def fail(*args, **kwargs):
        with (
            database_replacement_lock(store.path),
            sqlite3.connect(store.path, timeout=0.1) as c,
        ):
            c.execute("BEGIN IMMEDIATE")
        raise OSError("synthetic-private-detail")

    monkeypatch.setattr(module, "write_xlsx", fail)
    output = tmp_path / "private"
    builder = PaidPackagePreparer(store.path, output, now=lambda: NOW)
    assert builder.prepare(1) is False
    assert not list(output.iterdir())
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT state,error_code FROM paid_package_jobs"
        ).fetchone() == ("pending", "prepare_failed")
        assert c.execute("SELECT count(*) FROM b2b_deliveries").fetchone() == (0,)
        assert c.execute("SELECT status_platnosci FROM b2b_orders").fetchone() == (
            "paid",
        )
    monkeypatch.setattr(module, "write_xlsx", real)
    assert builder.run_pending() == 1


def test_abandoned_claim_recovers_but_fences_previous_worker(payment, tmp_path):
    store, paid = payment
    record(store, paid)
    old = store.claim_package(1, now=NOW)
    assert old is not None
    assert store.claim_package(1, now=NOW) is None
    late = NOW + timedelta(seconds=601)
    builder = PaidPackagePreparer(store.path, tmp_path / "private", now=lambda: late)
    assert builder.run_pending() == 1
    assert store.finish_package(old, [], "old.xlsx", "old.csv", now=late) is False
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM b2b_deliveries").fetchone() == (1,)


def test_final_db_failure_keeps_paid_work_recoverable_and_cleans_files(
    payment, tmp_path
):
    store, paid = payment
    record(store, paid)
    with sqlite3.connect(store.path) as c:
        c.execute(
            "CREATE TRIGGER fail_delivery BEFORE INSERT ON b2b_deliveries BEGIN SELECT RAISE(ABORT,'synthetic'); END"
        )
    output = tmp_path / "private"
    builder = PaidPackagePreparer(store.path, output, now=lambda: NOW)
    assert builder.prepare(1) is False
    assert not list(output.iterdir())
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM b2b_deliveries").fetchone() == (0,)
        c.execute("DROP TRIGGER fail_delivery")
    assert builder.run_pending() == 1


def test_customer_history_is_preserved_across_orders(payment, tmp_path):
    from core.delivery_memory import DeliveryClassifier

    store, paid = payment
    add_cases(store.path)
    with LeadRepository(store.path, existing_only=True) as repo:
        rev = DeliveryClassifier.revision_for(repo.get("MATCH"))
        with repo.transaction():
            repo.connection.execute(
                "INSERT INTO b2b_orders(order_id,customer_id,typ_pakietu) VALUES(2,1,'jednorazowy')"
            )
            repo.connection.execute(
                "INSERT INTO b2b_deliveries(delivery_id,order_id,typ_dostawy,doreczono_utc) VALUES(1,2,'delta','2026-11-01T00:00:00+00:00')"
            )
            repo.connection.execute(
                "INSERT INTO b2b_delivery_items VALUES(1,'MATCH',?,'new')", (rev,)
            )
    record(store, paid)
    assert PaidPackagePreparer(
        store.path, tmp_path / "private", now=lambda: NOW
    ).prepare(1)
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT id_sprawy FROM b2b_delivery_items WHERE delivery_id=2"
        ).fetchall() == [("OTHERCOUNTY",)]


def test_package_scope_matches_estimator_and_bounds_input(payment):
    from core.package_selection import PackageFilters, estimate_package, select_package

    store, _ = payment
    add_cases(store.path)
    with sqlite3.connect(store.path) as c:
        c.row_factory = sqlite3.Row
        filters = PackageFilters("28", "materialy")
        assert (
            len(select_package(c, filters, today=NOW.date(), limit=3))
            == estimate_package(c, filters, today=NOW.date()).count
            == 2
        )
        with pytest.raises(ValueError, match="limit"):
            select_package(c, filters, today=NOW.date(), limit=1)


def test_oversized_job_is_visible_and_cli_returns_failure(
    payment, tmp_path, monkeypatch, capsys
):
    from core.paid_packages import main

    store, paid = payment
    with sqlite3.connect(store.path) as c:
        c.executemany(
            """INSERT INTO investments(id_sprawy,zrodlo,status,kategoria,powiat_teryt,
            utworzono,zmieniono,status_zmieniony,ostatnio_widziany)
            VALUES(?,'pozwolenia','wniosek','komercyjna','2814','2026-11-02','2026-11-02','2026-11-02','2026-11-02')""",
            ((f"SYNTHETIC-{i}",) for i in range(10001)),
        )
    record(store, paid)
    output = tmp_path / "private"
    monkeypatch.setattr(
        "sys.argv",
        ["paid_packages", "--database", str(store.path), "--output", str(output)],
    )
    assert main() == 1
    assert "pending" in capsys.readouterr().out.lower()
    assert not output.exists()
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT state,error_code FROM paid_package_jobs"
        ).fetchone() == ("pending", "too_large")
        assert c.execute("SELECT count(*) FROM b2b_deliveries").fetchone() == (0,)


def test_fsync_failure_never_publishes_unflushed_exports(
    payment, tmp_path, monkeypatch
):
    import os

    store, paid = payment
    record(store, paid)

    def fail(_fd):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(os, "fsync", fail)
    output = tmp_path / "private"
    assert PaidPackagePreparer(store.path, output, now=lambda: NOW).prepare(1) is False
    assert not list(output.iterdir())
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM b2b_deliveries").fetchone() == (0,)
