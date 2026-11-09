"""Private, immutable download artifacts; synthetic files/DB only."""

import hashlib
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from core.package_access import PackageAccessStore
from core.package_files import (
    ArtifactUnavailable,
    inspect_artifact,
    open_verified,
)
from core.storage import LeadRepository

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def package(tmp_path):
    path, root = tmp_path / "downloads.sqlite", tmp_path / "private"
    root.mkdir(mode=0o700)
    with LeadRepository(path) as repo:
        c = repo.connection
        c.executemany(
            "INSERT INTO b2b_customers(customer_id,email) VALUES(?,?)",
            [(1, "owner@example.test"), (2, "other@example.test")],
        )
        c.execute(
            "INSERT INTO b2b_orders(order_id,customer_id,typ_pakietu,status_platnosci) VALUES(1,1,'jednorazowy','paid')"
        )
        c.execute(
            "INSERT INTO b2b_deliveries(delivery_id,order_id,typ_dostawy,doreczono_utc,plik_xlsx,plik_csv) VALUES(1,1,'delta','2026-11-01','sample.xlsx','sample.csv')"
        )
    for suffix in ("xlsx", "csv"):
        (root / ("sample." + suffix)).write_bytes(b"SYNTHETIC-" + suffix.encode())
    return PackageAccessStore(path), root


def test_manifest_is_two_immutable_formats_and_recording_never_publishes(package):
    store, root = package
    artifacts = tuple(
        inspect_artifact(root, "sample." + fmt, fmt) for fmt in ("xlsx", "csv")
    )
    store.record_artifacts(1, artifacts)
    store.record_artifacts(1, artifacts)  # safe backfill retry
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT format,size,sha256 FROM package_artifacts ORDER BY format"
        ).fetchall() == [(a.format, a.size, a.sha256) for a in reversed(artifacts)]
        assert c.execute("SELECT doreczono_utc FROM b2b_deliveries").fetchone() == (
            "2026-11-01",
        )
        assert c.execute("SELECT count(*) FROM package_publications").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE package_artifacts SET sha256=?", ("0" * 64,))
    with pytest.raises(ValueError):
        store.record_artifacts(
            1, (replace(artifacts[0], relative_name="other.xlsx"), artifacts[1])
        )


def test_real_file_is_hashed_and_streamed_through_one_handle(package):
    _, root = package
    artifact = inspect_artifact(root, "sample.csv", "csv")
    assert (
        artifact.size == 13
        and artifact.sha256 == hashlib.sha256(b"SYNTHETIC-csv").hexdigest()
    )
    with open_verified(root, artifact) as opened:
        assert opened.stream.read() == b"SYNTHETIC-csv"
    assert opened.stream.closed


@pytest.mark.parametrize(
    "name",
    [
        "../sample.csv",
        "/sample.csv",
        "sub/sample.csv",
        "sub\\sample.csv",
        "C:sample.csv",
        "sample.csv:stream",
        "..csv",
        "sample.xlsx",
    ],
)
def test_arbitrary_or_mismatched_paths_are_rejected(package, name):
    _, root = package
    with pytest.raises(ArtifactUnavailable):
        inspect_artifact(root, name, "csv")


def test_missing_corrupt_and_oversized_manifest_are_rejected(package):
    _, root = package
    good = inspect_artifact(root, "sample.csv", "csv")
    (root / "sample.csv").write_bytes(b"CORRUPT")
    with pytest.raises(ArtifactUnavailable):
        open_verified(root, good)
    with pytest.raises(ArtifactUnavailable):
        open_verified(root, replace(good, size=65 * 1024 * 1024))
    (root / "sample.csv").unlink()
    with pytest.raises(ArtifactUnavailable):
        open_verified(root, good)


@pytest.mark.parametrize("parent", [False, True])
def test_file_and_parent_symlinks_are_rejected(package, tmp_path, parent):
    _, root = package
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sample.csv").write_bytes(b"PRIVATE-OTHER")
    alias = tmp_path / "alias" if parent else root / "alias.csv"
    try:
        alias.symlink_to(
            outside if parent else outside / "sample.csv", target_is_directory=parent
        )
    except OSError:
        pytest.skip("OS does not permit synthetic symlinks")
    with pytest.raises(ArtifactUnavailable):
        inspect_artifact(
            alias if parent else root, "sample.csv" if parent else "alias.csv", "csv"
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO")
def test_fifo_never_blocks_download(package):
    _, root = package
    os.mkfifo(root / "fifo.csv")
    with pytest.raises(ArtifactUnavailable):
        inspect_artifact(root, "fifo.csv", "csv")


def test_path_replacement_after_verification_cannot_change_stream(package):
    _, root = package
    good = inspect_artifact(root, "sample.csv", "csv")
    with open_verified(root, good) as opened:
        alternate = root / "alternate.csv"
        alternate.write_bytes(b"OTHER-SECRET")
        try:
            os.replace(alternate, root / "sample.csv")
        except PermissionError:
            assert os.name == "nt"  # native handle denies replacement while open
        assert opened.stream.read() == b"SYNTHETIC-csv"


@pytest.fixture
def download_http(package, monkeypatch):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app.config import WebConfig
    from app.identity import AuthenticatedCustomer
    from app.main import create_app
    from app.routes import downloads
    from tests.test_auth_service import settings

    store, root = package
    store.record_artifacts(
        1,
        tuple(inspect_artifact(root, "sample." + fmt, fmt) for fmt in ("xlsx", "csv")),
    )
    clock = [NOW]
    monkeypatch.setattr(downloads, "utc_now", lambda: clock[0])
    config = WebConfig(database_path=store.path, package_dir=root, auth=settings())

    def identity(request):
        value = request.cookies.get("owner")
        return AuthenticatedCustomer(int(value)) if value in {"1", "2"} else None

    with TestClient(
        create_app(config, identity_resolver=identity),
        base_url=config.auth.origin,
        raise_server_exceptions=False,
    ) as client:
        client.cookies.set("owner", "1")
        yield SimpleNamespace(
            store=store,
            root=root,
            client=client,
            clock=clock,
            origin=config.auth.origin,
        )


def issue(http, fmt="csv", delivery=1):
    return http.client.post(
        f"/panel/paczki/{delivery}/link",
        data={"format": fmt},
        headers={"Origin": http.origin},
        follow_redirects=False,
    )


@pytest.mark.parametrize("fmt", ["xlsx", "csv"])
def test_owner_can_retry_get_and_head_without_ack_or_token_consumption(
    download_http, fmt
):
    h = download_http
    grant = issue(h, fmt)
    assert grant.status_code == 303
    url = grant.headers["location"]
    first, second, head = h.client.get(url), h.client.get(url), h.client.head(url)
    assert first.status_code == second.status_code == head.status_code == 200
    assert first.content == second.content == b"SYNTHETIC-" + fmt.encode()
    assert head.content == b"" and int(head.headers["content-length"]) == len(
        first.content
    )
    assert (
        first.headers["content-disposition"]
        == f'attachment; filename="zolta-tablica-paczka-1.{fmt}"'
    )
    assert (
        first.headers["cache-control"] == "no-store"
        and first.headers["referrer-policy"] == "no-referrer"
    )
    token = url.rsplit("/", 1)[1]
    with sqlite3.connect(h.store.path) as c:
        assert c.execute(
            "SELECT expires_at-created_at FROM download_grants"
        ).fetchone() == (900,)
        rows = c.execute(
            "SELECT token_hash,method,outcome FROM download_audit ORDER BY audit_id"
        ).fetchall()
        assert rows == [
            (hashlib.sha256(token.encode()).hexdigest(), "GET", "requested")
        ] * 2 + [(hashlib.sha256(token.encode()).hexdigest(), "HEAD", "head")]
        assert token not in repr(c.execute("SELECT * FROM download_audit").fetchall())
        assert c.execute("SELECT doreczono_utc FROM b2b_deliveries").fetchone() == (
            "2026-11-01",
        )
        assert c.execute("SELECT count(*) FROM package_publications").fetchone() == (0,)


def test_foreign_tokens_ids_signature_and_no_session_never_leak(download_http):
    h = download_http
    url = issue(h).headers["location"]
    h.client.cookies.set("owner", "2")
    for response in (issue(h), h.client.get(url), h.client.get("/pobierz/not-a-token")):
        assert response.status_code == 404 and "sample.csv" not in response.text
    h.client.cookies.clear()
    response = h.client.get(url)
    assert response.status_code == 401 and "/logowanie" in response.text
    assert url not in response.text


def test_expired_link_after_login_offers_owner_renewal(download_http):
    from datetime import timedelta

    h = download_http
    url = issue(h).headers["location"]
    h.clock[0] += timedelta(seconds=900)
    h.client.cookies.clear()
    assert h.client.get(url).status_code == 401
    h.client.cookies.set("owner", "1")
    expired = h.client.get(url)
    assert (
        expired.status_code == 410 and 'action="/panel/paczki/1/link"' in expired.text
    )
    fresh = issue(h)
    assert fresh.status_code == 303 and fresh.headers["location"] != url
    assert h.client.get(fresh.headers["location"]).status_code == 200


@pytest.mark.parametrize(
    "change", ["prepared", "unpaid", "different_owner", "missing_manifest"]
)
def test_grant_rechecks_access_and_never_treats_preparation_as_publication(
    download_http, change
):
    h = download_http
    url = issue(h).headers["location"]
    with sqlite3.connect(h.store.path) as c:
        if change == "prepared":
            c.execute("UPDATE b2b_deliveries SET doreczono_utc=NULL")
        elif change == "unpaid":
            c.execute("UPDATE b2b_orders SET status_platnosci='failed'")
        elif change == "different_owner":
            c.execute("UPDATE b2b_orders SET customer_id=2")
        else:
            c.execute("DELETE FROM package_artifacts")
    assert h.client.get(url).status_code == 404
    assert issue(h).status_code == 404


def test_separate_publication_grants_archive_access_without_marking_delivery(
    download_http,
):
    h = download_http
    with sqlite3.connect(h.store.path) as c:
        c.execute("UPDATE b2b_deliveries SET doreczono_utc=NULL")
        c.execute("INSERT INTO package_publications VALUES(1,'2026-11-08T12:00:00Z')")
    url = issue(h).headers["location"]
    assert h.client.get(url).status_code == 200
    with sqlite3.connect(h.store.path) as c:
        assert c.execute("SELECT doreczono_utc FROM b2b_deliveries").fetchone() == (
            None,
        )


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_bad_file_allows_safe_retry_after_repair(download_http, damage):
    h = download_http
    url = issue(h).headers["location"]
    target = h.root / "sample.csv"
    target.unlink() if damage == "missing" else target.write_bytes(b"BROKEN")
    bad = h.client.get(url)
    assert (
        bad.status_code == 503
        and str(h.root) not in bad.text
        and "sample.csv" not in bad.text
    )
    assert bad.headers["retry-after"]
    target.write_bytes(b"SYNTHETIC-csv")
    assert h.client.get(url).content == b"SYNTHETIC-csv"


def test_link_post_enforces_origin_and_bounded_exact_format(download_http):
    h = download_http
    assert (
        h.client.post("/panel/paczki/1/link", data={"format": "csv"}).status_code == 403
    )
    assert (
        h.client.post(
            "/panel/paczki/1/link",
            data={"format": "csv"},
            headers={"Origin": "https://evil.example.test"},
        ).status_code
        == 403
    )
    for fmt in ("../sample.csv", "CSV", "", "xlsx\x00"):
        assert issue(h, fmt).status_code == 422
    assert (
        h.client.post(
            "/panel/paczki/1/link",
            content=b"x" * 5000,
            headers={
                "Origin": h.origin,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        ).status_code
        == 413
    )


def test_manifest_backfill_is_explicit_atomic_and_file_io_has_no_db_lock(
    package, monkeypatch
):
    import core.package_access as module
    from core.instance import database_replacement_lock

    store, root = package
    original = module.inspect_artifact

    def inspect(*args):
        with (
            database_replacement_lock(store.path),
            sqlite3.connect(store.path, timeout=0.05) as c,
        ):
            c.execute("BEGIN IMMEDIATE")
        return original(*args)

    monkeypatch.setattr(module, "inspect_artifact", inspect)
    (root / "sample.csv").unlink()
    with pytest.raises(ArtifactUnavailable):
        store.backfill(1, root)
    with sqlite3.connect(store.path) as c:
        assert c.execute("SELECT count(*) FROM package_artifacts").fetchone() == (0,)
    (root / "sample.csv").write_bytes(b"SYNTHETIC-csv")
    assert store.backfill(1, root) == 2
    assert store.backfill(1, root) == 2


def test_unexpected_verification_error_also_closes_handle(package, monkeypatch):
    import core.package_files as module

    _, root = package
    good = inspect_artifact(root, "sample.csv", "csv")
    opened = module._private_open(root, "sample.csv", "csv")
    monkeypatch.setattr(module, "_private_open", lambda *args: opened)

    def fail(*args):
        raise RuntimeError("synthetic verification failure")

    monkeypatch.setattr(module, "_inspect", fail)
    with pytest.raises(RuntimeError):
        module.open_verified(root, good)
    assert opened.stream.closed


@pytest.mark.parametrize("prefix", ["/pobierz/", "/%70obierz/", "/pobierz%2F"])
def test_access_log_redacts_real_token_and_query(download_http, caplog, prefix):
    import logging

    h = download_http
    url = issue(h).headers["location"]
    token = url.rsplit("/", 1)[1]
    with caplog.at_level(logging.INFO, logger="uvicorn.access"):
        logging.getLogger("uvicorn.access").info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1",
            "GET",
            prefix + token + "?copy=" + token,
            "1.1",
            200,
        )
    assert token not in caplog.text and "[REDACTED]" in caplog.text


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
def test_stream_disconnect_and_send_failure_close_handle_and_release_slot(
    package, spec
):
    import asyncio

    from starlette.requests import ClientDisconnect

    from app.routes.downloads import ArtifactResponse

    _, root = package
    opened = open_verified(root, inspect_artifact(root, "sample.csv", "csv"))
    released = []
    response = ArtifactResponse(opened, lambda: released.append(True))

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if spec == "2.4" and message["type"] == "http.response.body":
            raise OSError("synthetic closed connection")

    async def run():
        await response({"type": "http", "asgi": {"spec_version": spec}}, receive, send)

    if spec == "2.4":
        with pytest.raises(ClientDisconnect):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert opened.stream.closed and released == [True]


def test_four_active_streams_reject_fifth_then_cancellation_frees_every_slot(
    download_http, monkeypatch
):
    import asyncio

    from app.routes import downloads

    h = download_http
    url = issue(h).headers["location"]
    opened = []
    original = downloads.open_verified

    def capture(*args):
        result = original(*args)
        opened.append(result)
        return result

    monkeypatch.setattr(downloads, "open_verified", capture)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "scheme": "https",
        "path": url,
        "raw_path": url.encode(),
        "query_string": b"",
        "headers": [(b"cookie", b"owner=1")],
        "client": ("127.0.0.1", 1234),
        "server": ("example.test", 443),
        "root_path": "",
        "http_version": "1.1",
    }

    async def run():
        holding, entered = asyncio.Event(), asyncio.Queue()
        initial_statuses = []

        async def receive():
            await holding.wait()
            return {"type": "http.disconnect"}

        async def hold(message):
            if message["type"] == "http.response.start":
                initial_statuses.append(message["status"])
            if message["type"] == "http.response.body" and message.get("body"):
                entered.put_nowait(True)
                await holding.wait()

        tasks = []
        statuses = []

        async def capture_status(message):
            if message["type"] == "http.response.start":
                statuses.append(message["status"])

        try:
            # Start each stream after its short metadata transaction finishes.
            # A held503 error body is not an active file stream.
            for _ in range(4):
                tasks.append(
                    asyncio.create_task(h.client.app(dict(scope), receive, hold))
                )
                await asyncio.wait_for(entered.get(), 5)
                assert initial_statuses[-1] == 200
            await h.client.app(dict(scope), receive, capture_status)
            assert statuses == [429], (statuses, initial_statuses)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert len(opened) == 4 and all(value.stream.closed for value in opened)
        await h.client.app(dict(scope), receive, capture_status)
        assert statuses[-1] == 200 and opened[-1].stream.closed

    asyncio.run(run())


def test_verification_has_no_db_transaction_or_replacement_guard(
    download_http, monkeypatch
):
    from app.routes import downloads
    from core.instance import database_replacement_lock

    h = download_http
    original = downloads.open_verified

    def check(*args):
        with (
            database_replacement_lock(h.store.path),
            sqlite3.connect(h.store.path, timeout=0.05) as conn,
        ):
            conn.execute("BEGIN IMMEDIATE")
        return original(*args)

    monkeypatch.setattr(downloads, "open_verified", check)
    assert h.client.get(issue(h).headers["location"]).status_code == 200


def test_grant_rate_limit_and_head_do_not_leak_slots(download_http):
    from datetime import timedelta

    h = download_http
    url = issue(h).headers["location"]
    for _ in range(6):
        assert h.client.head(url).status_code == 200
    for _ in range(9):
        assert issue(h).status_code == 303
    assert issue(h).status_code == 429
    h.clock[0] += timedelta(seconds=61)
    assert issue(h).status_code == 303


def test_metadata_lock_returns_safe_503_without_file_access(download_http, monkeypatch):
    from app.routes import downloads
    from core.instance import database_replacement_lock

    h = download_http
    url = issue(h).headers["location"]

    def forbidden(*args):
        raise AssertionError("File must not open after metadata failure")

    monkeypatch.setattr(downloads, "open_verified", forbidden)
    with database_replacement_lock(h.store.path):
        assert h.client.get(url).status_code == 503
        # Malformed token audit is also unavailable; return safe503, not500.
        assert h.client.get("/pobierz/bad-token").status_code == 503


def test_dashboard_shows_only_owned_available_formats(download_http):
    h = download_http
    page = h.client.get("/panel")
    assert page.status_code == 200
    assert 'action="/panel/paczki/1/link"' in page.text
    assert "Pobierz XLSX" in page.text and "Pobierz CSV" in page.text
    assert "sample.csv" not in page.text and str(h.root) not in page.text
    with sqlite3.connect(h.store.path) as c:
        c.execute("UPDATE b2b_deliveries SET doreczono_utc=NULL")
    assert 'action="/panel/paczki/1/link"' not in h.client.get("/panel").text
    with sqlite3.connect(h.store.path) as c:
        c.execute("INSERT INTO package_publications VALUES(1,'2026-11-08')")
    published = h.client.get("/panel")
    assert "Udostępniona w panelu" in published.text
    assert 'action="/panel/paczki/1/link"' in published.text
    h.client.cookies.set("owner", "2")
    assert 'action="/panel/paczki/1/link"' not in h.client.get("/panel").text


def test_www_with_old_schema_does_not_migrate_or_backfill(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import WebConfig
    from app.identity import AuthenticatedCustomer
    from app.main import create_app
    from tests.test_auth_service import settings
    from tests.test_storage import legacy_database

    path = tmp_path / "legacy.sqlite"
    legacy_database(path, 22).close()
    with TestClient(
        create_app(
            WebConfig(database_path=path, auth=settings()),
            identity_resolver=lambda r: AuthenticatedCustomer(1),
        ),
        base_url=settings().origin,
    ) as client:
        response = client.post(
            "/panel/paczki/1/link",
            data={"format": "csv"},
            headers={"Origin": settings().origin},
        )
        assert response.status_code == 503
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone() == (22,)
        assert (
            c.execute(
                "SELECT name FROM sqlite_master WHERE name='package_artifacts'"
            ).fetchone()
            is None
        )
    assert not (tmp_path / "backups").exists()


def test_hard_link_does_not_alias_a_private_artifact(package, tmp_path):
    _, root = package
    try:
        os.link(root / "sample.csv", tmp_path / "outside.csv")
    except OSError:
        pytest.skip("OS does not support synthetic hard links")
    with pytest.raises(ArtifactUnavailable):
        inspect_artifact(root, "sample.csv", "csv")


@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction")
def test_windows_real_junction_is_rejected_without_symlink_privilege(package, tmp_path):
    import subprocess

    _, root = package
    alias = tmp_path / "junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(alias), str(root)],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.skip("OS does not permit synthetic junctions")
    with pytest.raises(ArtifactUnavailable):
        inspect_artifact(alias, "sample.csv", "csv")


def test_cancellation_during_verification_closes_worker_result(
    download_http, monkeypatch
):
    import asyncio
    import threading
    import time

    from app.routes import downloads

    h = download_http
    url = issue(h).headers["location"]
    original = downloads.open_verified
    opened = []
    entered, finish, returned = threading.Event(), threading.Event(), threading.Event()
    returned_workers = []

    def blocked(*args):
        result = original(*args)
        opened.append(result)
        entered.set()
        assert finish.wait(5), "Synthetic verification was not released"
        returned_workers.append(True)
        if len(returned_workers) == 4:
            returned.set()
        return result

    monkeypatch.setattr(downloads, "open_verified", blocked)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "scheme": "https",
        "path": url,
        "raw_path": url.encode(),
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
            raise AssertionError("Cancelled verification must not start a response")

        tasks = []
        try:
            for _ in range(4):
                entered.clear()
                task = asyncio.create_task(h.client.app(dict(scope), receive, send))
                tasks.append(task)
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                (result,) = await asyncio.gather(task, return_exceptions=True)
                assert isinstance(result, asyncio.CancelledError)
            statuses = []

            async def busy_send(message):
                if message["type"] == "http.response.start":
                    statuses.append(message["status"])

            await asyncio.wait_for(h.client.app(dict(scope), receive, busy_send), 1)
            assert statuses == [429]  # cancelled workers still own their four slots
            finish.set()
            assert await asyncio.to_thread(returned.wait, 5)
            deadline = time.monotonic() + 1
            while (
                not all(value.stream.closed for value in opened)
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.01)
            assert len(opened) == 4 and all(value.stream.closed for value in opened)
        finally:
            finish.set()
            await asyncio.to_thread(returned.wait, 5)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for value in opened:
                value.close()

    asyncio.run(run())
    monkeypatch.setattr(downloads, "open_verified", original)
    assert h.client.get(url).status_code == 200
