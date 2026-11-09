"""Real native downloads and renewal with synthetic files and authenticated sessions."""

import hashlib
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from openpyxl import load_workbook
from playwright.sync_api import expect

from core.demo import DEMO_PACKAGE_DATE, demo_package_items
from core.export_package import write_csv, write_xlsx
from core.package_access import PackageAccessStore


@pytest.mark.parametrize("javascript", [True, False])
def test_owner_native_downloads_and_expired_renewal(browser, app_server, javascript):
    root = app_server.path.parent / "private"
    root.mkdir(mode=0o700)
    for fmt, writer in (("xlsx", write_xlsx), ("csv", write_csv)):
        with (root / ("sample." + fmt)).open("wb") as target:
            writer(
                demo_package_items(), target, trade_key="dach", as_of=DEMO_PACKAGE_DATE
            )
    with sqlite3.connect(app_server.path) as c:
        c.execute(
            "INSERT INTO b2b_orders(order_id,customer_id,typ_pakietu,status_platnosci) VALUES(1,1,'jednorazowy','paid')"
        )
        c.execute(
            "INSERT INTO b2b_deliveries VALUES(1,1,'delta','2026-11-01','sample.xlsx','sample.csv')"
        )
    PackageAccessStore(app_server.path).backfill(1, root)
    with browser.new_context() as login:
        page = login.new_page()
        page.goto(app_server.url + "/logowanie")
        page.get_by_label("Adres e-mail", exact=True).fill("person@example.test")
        page.get_by_role("button", name="Wyślij link").click()
        expect(page.get_by_role("status")).to_contain_text("Jeśli możemy")
        assert app_server.delivery_done.wait(5)
        token = app_server.sender.messages[-1][2].split("/auth/verify#")[1].split()[0]
        page.goto(app_server.url + "/auth/verify#" + token)
        page.get_by_role("button", name="Potwierdź logowanie").click()
        expect(page).to_have_url(app_server.url + "/panel")
        cookies = login.cookies()
    with browser.new_context(
        java_script_enabled=javascript,
        accept_downloads=True,
        viewport={"width": 390, "height": 844},
    ) as context:
        context.add_cookies(cookies)
        page = context.new_page()
        page.goto(app_server.url + "/panel")
        for width in (320, 390, 768, 1280):
            page.set_viewport_size({"width": width, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.set_viewport_size({"width": 390, "height": 844})
        for fmt in ("xlsx", "csv"):
            button = page.get_by_role(
                "button", name="Pobierz " + fmt.upper(), exact=True
            )
            button.focus()
            with (
                page.expect_response(
                    lambda r: r.request.method == "POST"
                    and r.url.endswith("/panel/paczki/1/link")
                ) as link,
                page.expect_download() as transfer,
            ):
                button.press("Enter")
            assert link.value.status == 303
            assert transfer.value.failure() is None
            assert transfer.value.suggested_filename == "zolta-tablica-paczka-1." + fmt
            payload = Path(transfer.value.path()).read_bytes()
            if fmt == "csv":
                assert payload.startswith(
                    b"\xef\xbb\xbf"
                ) and "DANE FIKCYJNE" in payload.decode("utf-8-sig")
            else:
                book = load_workbook(BytesIO(payload), read_only=True)
                try:
                    assert book.sheetnames == ["1. Nowe", "2. Zmiany", "3. Pełna baza"]
                finally:
                    book.close()
        old_url = link.value.headers["location"]
        digest = hashlib.sha256(old_url.rsplit("/", 1)[1].encode()).hexdigest()
        with sqlite3.connect(app_server.path) as c:
            c.execute(
                "UPDATE download_grants SET created_at=created_at-901,expires_at=expires_at-901 WHERE token_hash=?",
                (digest,),
            )
        response = page.goto(app_server.url + old_url)
        assert response.status == 410
        assert "/pobierz/" not in page.url
        expect(page.get_by_role("status")).to_contain_text("Link wygasł")
        with page.expect_download() as renewed:
            page.get_by_role("button", name="Nowy link CSV", exact=True).click()
        assert renewed.value.failure() is None
        with sqlite3.connect(app_server.path) as c:
            assert c.execute("SELECT doreczono_utc FROM b2b_deliveries").fetchone() == (
                "2026-11-01",
            )
            assert c.execute(
                "SELECT method,outcome FROM download_audit ORDER BY audit_id"
            ).fetchall() == [
                ("GET", "requested"),
                ("GET", "requested"),
                ("GET", "expired"),
                ("GET", "requested"),
            ]
