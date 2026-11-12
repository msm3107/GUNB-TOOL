"""Immutable authorized package views; only synthetic records/files and loopback."""

import csv
import hashlib
import sqlite3
from dataclasses import replace
from io import BytesIO, StringIO

import pytest
from openpyxl import load_workbook

from core.demo import DEMO_PACKAGE_DATE, demo_package_items
from core.export_package import EXPORT_COLUMNS, write_csv, write_xlsx
from core.package_access import PackageAccessStore, PackageNotFound
from core.package_preview import PackagePreview, PreviewUnavailable
from core.storage import LeadRepository


@pytest.fixture
def archive(tmp_path):
    from types import SimpleNamespace

    path, root = tmp_path / "preview.sqlite", tmp_path / "private"
    root.mkdir(mode=0o700)
    base = demo_package_items()
    items = tuple(
        replace(
            base[i % 3],
            investment=replace(
                base[i % 3].investment,
                id_sprawy=f"CASE/{i:03}",
                nazwa_zamierzenia=f"FIKCYJNA inwestycja {i}",
            ),
        )
        for i in range(123)
    )
    with LeadRepository(path) as repo:
        c = repo.connection
        c.executemany(
            "INSERT INTO b2b_customers(customer_id,email) VALUES(?,?)",
            [(1, "owner@example.test"), (2, "other@example.test")],
        )
        c.execute("INSERT INTO b2b_orders VALUES(1,1,'{}','jednorazowy','paid')")
        c.execute(
            "INSERT INTO b2b_deliveries VALUES(1,1,'delta','2026-11-01','snapshot.xlsx','snapshot.csv')"
        )
        for item in items:
            repo.upsert(item.investment)
            c.execute(
                "INSERT INTO b2b_delivery_items VALUES(?,?,?,?)",
                (1, item.investment.id_sprawy, item.revision, item.category.value),
            )
    for fmt, writer in (("csv", write_csv), ("xlsx", write_xlsx)):
        with (root / ("snapshot." + fmt)).open("wb") as target:
            writer(items, target, trade_key="dach", as_of=DEMO_PACKAGE_DATE)
    store = PackageAccessStore(path)
    store.backfill(1, root)
    return SimpleNamespace(
        path=path,
        root=root,
        items=items,
        store=store,
        service=PackagePreview(store, root),
    )


def rewrite_csv(a, rows):
    """Trusted test fixture replaces both bytes and expected digest before request."""
    stream = StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\r\n")
    writer.writerows(rows)
    content = b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")
    (a.root / "snapshot.csv").write_bytes(content)
    with sqlite3.connect(a.path) as c:
        c.execute("DELETE FROM package_artifacts WHERE format='csv'")
        c.execute(
            "INSERT INTO package_artifacts VALUES(1,'csv','snapshot.csv',?,?)",
            (len(content), hashlib.sha256(content).hexdigest()),
        )


def rows(a):
    return list(
        csv.reader(
            StringIO(
                (a.root / "snapshot.csv").read_text(encoding="utf-8-sig"), newline=""
            )
        )
    )


def test_preview_uses_saved_csv_and_counts_match_real_xlsx_after_new_import(archive):
    a = archive
    first = a.service.read(1, 1)
    assert len(first.rows) == 50 and first.total == 123 and first.pages == 3
    assert first.counts == {"new": 41, "change": 41, "full": 41}
    assert first.rows[0].values["Nazwa zamierzenia"] == "FIKCYJNA inwestycja 0"
    with LeadRepository(a.path, existing_only=True) as repo:
        repo.upsert(
            replace(
                a.items[0].investment,
                nazwa_zamierzenia="CURRENT-SECRET",
                status="odmowa",
                wymaga_weryfikacji=True,
            )
        )
    assert a.service.read(1, 1) == first
    book = load_workbook(
        BytesIO((a.root / "snapshot.xlsx").read_bytes()), read_only=True
    )
    try:
        assert [sum(1 for _ in sheet.values) - 1 for sheet in book.worksheets] == [
            41,
            41,
            41,
        ]
    finally:
        book.close()
    assert a.service.read(1, 1, page=3).rows[-1].case_id == "CASE/122"
    selected = a.service.read(1, 1, category="change", case_id="CASE/100")
    assert (
        selected.total == 41
        and selected.pages == 1
        and selected.selected.case_id == "CASE/100"
    )
    assert all(row.category == "change" for row in selected.rows)


@pytest.mark.parametrize(
    "page,category",
    [(0, "all"), (201, "all"), (True, "all"), (1, "unknown"), (4, "all")],
)
def test_invalid_view_never_silently_broadens_scope(archive, page, category):
    with pytest.raises(ValueError):
        archive.service.read(1, 1, page=page, category=category)


def test_foreign_prepared_missing_or_unlisted_case_are_not_found(archive):
    a = archive
    for customer, delivery, case in (
        (2, 1, None),
        (1, 2, None),
        (1, 1, "CASE/FOREIGN"),
    ):
        with pytest.raises(PackageNotFound):
            a.service.read(customer, delivery, case_id=case)
    with sqlite3.connect(a.path) as c:
        c.execute("UPDATE b2b_deliveries SET doreczono_utc=NULL")
    with pytest.raises(PackageNotFound):
        a.service.read(1, 1)


@pytest.mark.parametrize(
    "damage",
    ["header", "columns", "category", "duplicate", "missing", "unknown", "many"],
)
def test_malformed_csv_never_yields_partial_rows_even_with_matching_digest(
    archive, damage
):
    a = archive
    content = rows(a)
    if damage == "header":
        content[0][0] = "OTHER"
    elif damage == "columns":
        content[1].pop()
    elif damage == "category":
        content[1][1] = "unknown"
    elif damage == "duplicate":
        content.append(content[1])
    elif damage == "missing":
        content.pop()
    elif damage == "unknown":
        content[1][0] = "UNKNOWN/CASE"
    else:
        content.extend([content[1]] * 10001)
    rewrite_csv(a, content)
    with pytest.raises(PreviewUnavailable):
        a.service.read(1, 1)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "utf8", "field"])
def test_unavailable_artifact_is_not_rebuilt_from_investments(archive, damage):
    a = archive
    target = a.root / "snapshot.csv"
    if damage == "missing":
        target.unlink()
    elif damage == "corrupt":
        target.write_bytes(b"CORRUPT")
    elif damage == "utf8":
        bad = b"\xef\xbb\xbf\xff"
        target.write_bytes(bad)
        with sqlite3.connect(a.path) as c:
            c.execute("DELETE FROM package_artifacts WHERE format='csv'")
            c.execute(
                "INSERT INTO package_artifacts VALUES(1,'csv','snapshot.csv',?,?)",
                (len(bad), hashlib.sha256(bad).hexdigest()),
            )
    else:
        content = rows(a)
        content[1][12] = "A" * 131073
        rewrite_csv(a, content)
    with pytest.raises(PreviewUnavailable):
        a.service.read(1, 1)


def test_read_and_parse_do_not_hold_db_write_or_replacement_guard(archive, monkeypatch):
    import core.package_preview as module
    from core.instance import database_replacement_lock

    original = module.open_verified

    def check(*args):
        with (
            database_replacement_lock(archive.path),
            sqlite3.connect(archive.path, timeout=0.05) as c,
        ):
            c.execute("BEGIN IMMEDIATE")
        return original(*args)

    monkeypatch.setattr(module, "open_verified", check)
    assert archive.service.read(1, 1).total == 123


def test_private_case_context_is_not_written_to_access_log(preview_http, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1",
            "GET",
            "/panel/paczki/1?case_id=PRIVATE-CASE",
            "1.1",
            200,
        )
    assert "PRIVATE-CASE" not in caplog.text and "[PRIVATE]" in caplog.text


def test_more_than_10000_items_is_rejected_before_file_io(archive, monkeypatch):
    import core.package_preview as module

    with sqlite3.connect(archive.path) as c:
        c.executemany(
            "INSERT INTO investments(id_sprawy,zrodlo,status,utworzono,zmieniono,status_zmieniony,ostatnio_widziany) VALUES(?,'pozwolenia','wniosek','x','x','x','x')",
            ((f"OVER/{i}",) for i in range(10001)),
        )
        c.execute(
            "INSERT INTO b2b_delivery_items SELECT 1,id_sprawy,'revision','new' FROM investments WHERE id_sprawy LIKE 'OVER/%'"
        )

    def forbidden(*args):
        raise AssertionError("Oversized ledger must fail before file IO")

    monkeypatch.setattr(module, "open_verified", forbidden)
    with pytest.raises(PreviewUnavailable):
        archive.service.read(1, 1)


def test_preview_and_download_share_four_slots_even_after_cancel(
    preview_http, monkeypatch
):
    import asyncio
    import threading

    import core.package_preview as module

    h = preview_http
    entered, finish, done = threading.Event(), threading.Event(), threading.Event()
    opened, finished = [], []
    real_open, real_read = module.open_verified, PackagePreview.read

    def blocked(*args):
        value = real_open(*args)
        opened.append(value)
        entered.set()
        assert finish.wait(5)
        return value

    def tracked(self, *args, **kwargs):
        try:
            return real_read(self, *args, **kwargs)
        finally:
            finished.append(True)
            if len(finished) == 4:
                done.set()

    monkeypatch.setattr(module, "open_verified", blocked)
    monkeypatch.setattr(PackagePreview, "read", tracked)
    grant = h.client.post(
        "/panel/paczki/1/link",
        data={"format": "csv"},
        headers={"Origin": h.origin},
        follow_redirects=False,
    )
    assert grant.status_code == 303
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "scheme": "https",
        "path": "/panel/paczki/1",
        "raw_path": b"/panel/paczki/1",
        "query_string": b"",
        "headers": [(b"cookie", b"owner=1")],
        "client": ("127.0.0.1", 1234),
        "server": ("example.test", 443),
        "root_path": "",
        "http_version": "1.1",
    }

    async def run():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            raise AssertionError("Cancelled preview must not send a response")

        tasks = []
        try:
            for _ in range(4):
                entered.clear()
                task = asyncio.create_task(h.client.app(dict(scope), receive, send))
                tasks.append(task)
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert h.client.get(grant.headers["location"]).status_code == 429
        finally:
            finish.set()
            await asyncio.to_thread(done.wait, 5)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for value in opened:
                value.close()

    asyncio.run(run())
    assert all(value.stream.closed for value in opened)
    assert h.client.get(grant.headers["location"]).status_code == 200


def test_disk_error_during_csv_parse_retains_form_and_closes_handle(
    preview_http, monkeypatch
):
    import core.package_preview as module
    from tests.test_customer_dashboard import _IssueForm

    h = preview_http
    original_reader, original_open = module.csv.reader, module.open_verified
    opened = []

    def fail_reader(*args, **kwargs):
        reader = original_reader(*args, **kwargs)
        yield next(reader)
        raise OSError("SYNTHETIC-DISK-DETAIL")

    def capture(*args):
        value = original_open(*args)
        opened.append(value)
        return value

    with monkeypatch.context() as patch:
        patch.setattr(module.csv, "reader", fail_reader)
        patch.setattr(module, "open_verified", capture)
        failed = post_issue(h)
        assert failed.status_code == 503
        assert "SYNTHETIC-DISK-DETAIL" not in failed.text
        assert (
            _IssueForm(failed.text, "/panel/paczki/1/uwagi").values == issue_payload()
        )
        assert opened and all(value.stream.closed for value in opened)
    assert post_issue(h).status_code == 303


def test_maps_are_existing_validated_snapshot_links(archive):
    a = archive
    content = rows(a)
    content[1][EXPORT_COLUMNS.index("Szerokość geogr.")] = "53.5"
    content[1][EXPORT_COLUMNS.index("Długość geogr.")] = "20.25"
    content[1][EXPORT_COLUMNS.index("Google Maps")] = (
        "https://www.google.com/maps?q=53.500000,20.250000"
    )
    content[1][EXPORT_COLUMNS.index("Geoportal")] = (
        "https://mapy.geoportal.gov.pl/imap/Imgp_2.html?identifyParcel=281404_2.0001.12"
    )
    content[2][EXPORT_COLUMNS.index("Google Maps")] = "javascript:alert(1)"
    content[2][EXPORT_COLUMNS.index("Geoportal")] = (
        "https://mapy.geoportal.gov.pl.evil.test/imap/Imgp_2.html?identifyParcel=123"
    )
    rewrite_csv(a, content)
    page = a.service.read(1, 1)
    assert (
        page.rows[0].google_maps == content[1][-1]
        and page.rows[0].geoportal == content[1][-2]
    )
    assert page.rows[1].google_maps is None and page.rows[1].geoportal is None


@pytest.mark.parametrize("raw_id", ["=LEAD", "'=LEAD"])
def test_formula_safe_csv_id_uses_immutable_ledger_identity(archive, raw_id):
    a = archive
    with LeadRepository(a.path, existing_only=True) as repo:
        repo.upsert(replace(a.items[0].investment, id_sprawy=raw_id))
        repo.connection.execute(
            "UPDATE b2b_delivery_items SET id_sprawy=? WHERE id_sprawy=?",
            (raw_id, "CASE/000"),
        )
    content = rows(a)
    content[1][0] = "'=LEAD"
    rewrite_csv(a, content)
    assert a.service.read(1, 1, case_id=raw_id).selected.case_id == raw_id


def test_ambiguous_encoded_ids_are_rejected_instead_of_guessing(archive):
    a = archive
    content = rows(a)
    with LeadRepository(a.path, existing_only=True) as repo:
        for index, raw_id in ((0, "=LEAD"), (3, "'=LEAD")):
            repo.upsert(replace(a.items[index].investment, id_sprawy=raw_id))
            repo.connection.execute(
                "UPDATE b2b_delivery_items SET id_sprawy=? WHERE id_sprawy=?",
                (raw_id, f"CASE/{index:03}"),
            )
            content[index + 1][0] = "'=LEAD"
    rewrite_csv(a, content)
    with pytest.raises(PreviewUnavailable):
        a.service.read(1, 1)


def test_long_values_are_bounded_in_preview_but_original_file_is_unchanged(archive):
    a = archive
    content = rows(a)
    content[1][12] = "Ł" * 5000
    rewrite_csv(a, content)
    page = a.service.read(1, 1)
    assert page.rows[0].values["Nazwa zamierzenia"] == "Ł" * 2000
    assert page.rows[0].truncated is True
    assert rows(a)[1][12] == "Ł" * 5000


@pytest.fixture
def preview_http(archive, monkeypatch):
    from datetime import datetime, timezone

    from fastapi.testclient import TestClient

    from app.config import WebConfig
    from app.identity import AuthenticatedCustomer
    from app.main import create_app
    from app.routes import packages
    from tests.test_auth_service import settings

    config = WebConfig(
        database_path=archive.path, package_dir=archive.root, auth=settings()
    )
    archive.now = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(packages, "utc_now", lambda: archive.now)

    def identity(request):
        if getattr(archive, "auth_unavailable", False):
            from core.checkout_store import CheckoutUnavailable

            raise CheckoutUnavailable
        value = request.cookies.get("owner")
        return AuthenticatedCustomer(int(value)) if value in ("1", "2") else None

    with TestClient(
        create_app(config, identity_resolver=identity),
        base_url=config.auth.origin,
        raise_server_exceptions=False,
    ) as client:
        client.cookies.set("owner", "1")
        archive.client, archive.origin = client, config.auth.origin
        yield archive


def issue_payload():
    return {
        "case_id": "CASE/075",
        "issue_type": "inna_uwaga",
        "description": "<script>opis & test</script>",
        "page": "2",
        "category": "all",
    }


def post_issue(h, values=None):
    return h.client.post(
        "/panel/paczki/1/uwagi",
        data=values or issue_payload(),
        headers={"Origin": h.origin},
        follow_redirects=False,
    )


def test_owner_get_is_private_paginated_and_selected_row_is_escaped(preview_http):
    h = preview_http
    content = rows(h)
    content[76][12] = "<img src=x onerror=alert(1)>"
    rewrite_csv(h, content)
    response = h.client.get("/panel/paczki/1?page=2&category=all&case_id=CASE%2F075")
    assert (
        response.status_code == 200 and response.headers["cache-control"] == "no-store"
    )
    from tests.test_landing_page import PageParser

    assert (
        sum(
            "package-row" in attrs.get("class", "").split()
            for tag, attrs in PageParser(response.text).elements
        )
        == 50
    )
    assert "<img src=x" not in response.text and "&lt;img" in response.text
    assert 'action="/panel/paczki/1/uwagi"' in response.text
    from tests.test_customer_dashboard import _IssueForm

    fields = _IssueForm(response.text, "/panel/paczki/1/uwagi").values
    assert (
        fields["case_id"] == "CASE/075"
        and fields["page"] == "2"
        and fields["category"] == "all"
    )
    h.client.cookies.set("owner", "2")
    foreign = h.client.get("/panel/paczki/1")
    assert foreign.status_code == 404 and "FIKCYJNA" not in foreign.text
    h.client.cookies.clear()
    assert (
        h.client.get("/panel/paczki/1", follow_redirects=False).headers["location"]
        == "/logowanie?next=%2Fpanel%2Fpaczki%2F1"
    )


def test_contextual_issue_creates_once_and_shows_only_own_acceptance_history(
    preview_http,
):
    h = preview_http
    for _ in range(2):
        result = post_issue(h)
        assert (
            result.status_code == 303
            and "page=2" in result.headers["location"]
            and "category=all" in result.headers["location"]
        )
    page = h.client.get(result.headers["location"])
    assert page.status_code == 200 and "Przyjęto do weryfikacji" in page.text
    assert "&lt;script&gt;opis &amp; test&lt;/script&gt;" in page.text
    with sqlite3.connect(h.path) as c:
        assert c.execute(
            "SELECT customer_id,id_sprawy,opis FROM issue_reports"
        ).fetchall() == [(1, "CASE/075", "<script>opis & test</script>")]
        assert c.execute("SELECT doreczono_utc FROM b2b_deliveries").fetchone() == (
            "2026-11-01",
        )
        c.execute(
            "INSERT INTO issue_reports(customer_id,id_sprawy,typ,opis,zgloszono_utc) VALUES(2,'CASE/075','inna_uwaga','FOREIGN-SECRET',?)",
            (h.now.isoformat(),),
        )
    assert "FOREIGN-SECRET" not in h.client.get(result.headers["location"]).text


def test_locked_write_and_failed_reload_keep_all_fields_then_retry_once(
    preview_http, monkeypatch
):
    from contextlib import ExitStack

    from core.customer_dashboard import CustomerDashboardStore
    from core.instance import database_replacement_lock
    from tests.test_customer_dashboard import _IssueForm

    h = preview_http
    original = CustomerDashboardStore.report_issue
    with ExitStack() as locks, monkeypatch.context() as patch:

        def locked(self, *args, **kwargs):
            locks.enter_context(database_replacement_lock(self.path))
            return original(self, *args, **kwargs)

        patch.setattr(CustomerDashboardStore, "report_issue", locked)
        failed = post_issue(h)
        assert failed.status_code == 503
        assert (
            _IssueForm(failed.text, "/panel/paczki/1/uwagi").values == issue_payload()
        )
        assert "<script>opis" not in failed.text
    assert post_issue(h).status_code == 303
    with sqlite3.connect(h.path) as c:
        assert c.execute("SELECT count(*) FROM issue_reports").fetchone() == (1,)


def test_rate_limit_retains_description_and_context_then_retry(preview_http):
    from tests.test_customer_dashboard import _IssueForm

    h = preview_http
    with sqlite3.connect(h.path) as c:
        c.executemany(
            "INSERT INTO issue_reports(customer_id,id_sprawy,typ,opis,zgloszono_utc) VALUES(1,?,'inna_uwaga','SYNTHETIC',?)",
            [(f"CASE/{i:03}", h.now.isoformat(timespec="seconds")) for i in range(20)],
        )
    denied = post_issue(h)
    assert denied.status_code == 429 and denied.headers.get("retry-after")
    assert _IssueForm(denied.text, "/panel/paczki/1/uwagi").values == issue_payload()
    with sqlite3.connect(h.path) as c:
        c.execute("DELETE FROM issue_reports WHERE id_sprawy='CASE/019'")
    assert post_issue(h).status_code == 303
    assert post_issue(h).status_code == 303
    with sqlite3.connect(h.path) as c:
        assert c.execute(
            "SELECT count(*) FROM issue_reports WHERE id_sprawy='CASE/075'"
        ).fetchone() == (1,)


def test_wrong_case_and_csrf_are_rejected_and_publication_has_same_rights(preview_http):
    h = preview_http
    body = {**issue_payload(), "case_id": "OTHER/CASE"}
    assert post_issue(h, body).status_code == 404
    assert (
        h.client.post(
            "/panel/paczki/1/uwagi",
            data=issue_payload(),
            headers={"Origin": "https://evil.example.test"},
        ).status_code
        == 403
    )
    h.client.cookies.set("owner", "2")
    assert post_issue(h).status_code == 404
    h.client.cookies.set("owner", "1")
    with sqlite3.connect(h.path) as c:
        c.execute("UPDATE b2b_deliveries SET doreczono_utc=NULL")
        c.execute("INSERT INTO package_publications VALUES(1,'2026-11-09')")
    assert post_issue(h).status_code == 303


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE b2b_orders SET customer_id=2",
        "UPDATE b2b_orders SET status_platnosci='failed'",
        "DELETE FROM package_artifacts",
        "UPDATE b2b_deliveries SET doreczono_utc=NULL",
    ],
)
def test_write_rechecks_rights_and_manifest_after_csv_read(
    preview_http, monkeypatch, change
):
    from core.customer_dashboard import CustomerDashboardStore

    h = preview_http
    original = CustomerDashboardStore.report_issue

    def changed(self, *args, **kwargs):
        with sqlite3.connect(h.path) as c:
            c.execute(change)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(CustomerDashboardStore, "report_issue", changed)
    assert post_issue(h).status_code in (403, 404)
    with sqlite3.connect(h.path) as c:
        assert c.execute("SELECT count(*) FROM issue_reports").fetchone() == (0,)


def test_oversized_case_identity_is_rejected_with_bounded_metadata(archive):
    a = archive
    raw_id = "A" * 5000
    with LeadRepository(a.path, existing_only=True) as repo:
        repo.upsert(replace(a.items[0].investment, id_sprawy=raw_id))
        repo.connection.execute(
            "UPDATE b2b_delivery_items SET id_sprawy=? WHERE id_sprawy=?",
            (raw_id, "CASE/000"),
        )
    content = rows(a)
    content[1][0] = raw_id
    rewrite_csv(a, content)
    with pytest.raises(PreviewUnavailable):
        a.service.read(1, 1)
    _, identities = a.store.preview_metadata(1, 1)
    assert all(len(case) <= 513 for case, _ in identities)


def test_auth_failure_echoes_only_own_posted_fields_without_package_data(preview_http):
    from tests.test_customer_dashboard import _IssueForm

    h = preview_http
    h.auth_unavailable = True
    unavailable = post_issue(h)
    assert unavailable.status_code == 503
    assert (
        _IssueForm(unavailable.text, "/panel/paczki/1/uwagi").values == issue_payload()
    )
    assert "FIKCYJNA inwestycja" not in unavailable.text
    h.auth_unavailable = False
    h.client.cookies.clear()
    anonymous = post_issue(h)
    assert anonymous.status_code == 401
    assert _IssueForm(anonymous.text, "/panel/paczki/1/uwagi").values == issue_payload()
    assert (
        "/logowanie" in anonymous.text and "FIKCYJNA inwestycja" not in anonymous.text
    )


def test_cancelled_read_keeps_capacity_until_worker_closes_file(archive, monkeypatch):
    import asyncio
    import threading

    from starlette.concurrency import run_in_threadpool

    import core.package_preview as module
    from core.package_access import DownloadBusy

    original = module.open_verified
    entered, finish, done = threading.Event(), threading.Event(), threading.Event()
    opened, finished = [], []

    def blocked(*args):
        result = original(*args)
        opened.append(result)
        entered.set()
        assert finish.wait(5)
        return result

    def worker():
        try:
            return archive.service.read(1, 1)
        finally:
            finished.append(True)
            if len(finished) == 4:
                done.set()

    monkeypatch.setattr(module, "open_verified", blocked)

    async def run():
        tasks = []
        try:
            for _ in range(4):
                entered.clear()
                task = asyncio.create_task(run_in_threadpool(worker))
                tasks.append(task)
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                (result,) = await asyncio.gather(task, return_exceptions=True)
                assert isinstance(result, asyncio.CancelledError)
            with pytest.raises(DownloadBusy):
                archive.service.read(1, 1)
            finish.set()
            assert await asyncio.to_thread(done.wait, 5)
            assert len(opened) == 4 and all(value.stream.closed for value in opened)
        finally:
            finish.set()
            await asyncio.to_thread(done.wait, 5)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for value in opened:
                value.close()

    asyncio.run(run())
    monkeypatch.setattr(module, "open_verified", original)
    assert archive.service.read(1, 1).total == 123
