"""Real private package navigation and contextual feedback, with native forms."""

import re
import sqlite3
from dataclasses import replace

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from core.demo import DEMO_PACKAGE_DATE, demo_package_items
from core.export_package import write_csv, write_xlsx
from core.instance import database_replacement_lock
from core.package_access import PackageAccessStore
from core.storage import LeadRepository


@pytest.fixture
def package_server(app_server):
    root = app_server.path.parent / "private"
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
    with LeadRepository(app_server.path, existing_only=True) as repo:
        c = repo.connection
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
        with (root / ("snapshot." + fmt)).open("wb") as f:
            writer(items, f, trade_key="dach", as_of=DEMO_PACKAGE_DATE)
    PackageAccessStore(app_server.path).backfill(1, root)
    return app_server


def login_cookies(browser, server):
    with browser.new_context() as context:
        page = context.new_page()
        page.goto(server.url + "/logowanie")
        page.get_by_label("Adres e-mail", exact=True).fill("person@example.test")
        page.get_by_role("button", name="Wyślij link").click()
        expect(page.get_by_role("status")).to_contain_text("Jeśli możemy")
        assert server.delivery_done.wait(5)
        token = server.sender.messages[-1][2].split("/auth/verify#")[1].split()[0]
        page.goto(server.url + "/auth/verify#" + token)
        page.get_by_role("button", name="Potwierdź logowanie").click()
        expect(page).to_have_url(server.url + "/panel")
        return context.cookies()


@pytest.mark.parametrize("javascript", [True, False])
def test_native_preview_preserves_context_on_503_then_stores_one_issue(
    browser, package_server, javascript
):
    server = package_server
    cookies = login_cookies(browser, server)
    with browser.new_context(
        java_script_enabled=javascript, viewport={"width": 390, "height": 844}
    ) as context:
        context.add_cookies(cookies)
        page = context.new_page()
        page.goto(server.url + "/panel")
        page.get_by_role("link", name="Przejrzyj paczkę →", exact=True).click()
        expect(page.locator(".package-row")).to_have_count(50)
        page.get_by_role("link", name="Następna strona →", exact=True).first.click()
        expect(page.locator(".package-row").first.locator("summary")).to_contain_text(
            "CASE/050"
        )
        page.get_by_label("Typ pozycji").select_option("change")
        page.get_by_role("button", name="Pokaż pozycje", exact=True).click()
        expect(page.locator(".package-row")).to_have_count(41)
        first = page.locator(".package-row").first
        first.locator("summary").click()
        first.get_by_role(
            "link", name="Zgłoś uwagę przy tej inwestycji →", exact=True
        ).click()
        expect(page.locator("#issue-case")).to_have_value("CASE/001")
        page.get_by_label("Co wymaga sprawdzenia?").select_option("inna_uwaga")
        description = "<b>Syntetyczny opis & sprawdzenie</b>"
        page.get_by_label("Opis (do 1000 znaków)").fill(description)
        with database_replacement_lock(server.path):
            with page.expect_response(
                lambda r: (
                    r.request.method == "POST"
                    and r.url.endswith("/panel/paczki/1/uwagi")
                )
            ) as failed:
                page.get_by_role("button", name="Wyślij uwagę", exact=True).click()
            assert failed.value.status == 503
            expect(page.locator("#issue-description")).to_have_value(description)
            expect(page.locator("input[name=page]")).to_have_value("1")
            expect(page.locator("input[name=category]")).to_have_value("change")
        page.get_by_role("button", name="Wyślij uwagę", exact=True).click()
        expect(page).to_have_url(
            re.compile(
                r".*category=change&case_id=CASE%2F001&notice=issue#issue-heading$"
            )
        )
        expect(page.get_by_text("Przyjęto do weryfikacji", exact=False)).to_be_visible()
        expect(page.get_by_text(description, exact=True)).to_be_visible()
        with sqlite3.connect(server.path) as c:
            assert c.execute(
                "SELECT customer_id,id_sprawy,opis FROM issue_reports"
            ).fetchall() == [(1, "CASE/001", description)]
            c.execute(
                "UPDATE investments SET nazwa_zamierzenia='CURRENT-SECRET',status='odmowa' WHERE id_sprawy='CASE/001'"
            )
        page.reload()
        expect(page.locator("body")).not_to_contain_text("CURRENT-SECRET")
        expect(page.locator("#issue-case")).to_have_value("CASE/001")
        for width in (320, 390, 768, 1280):
            page.set_viewport_size({"width": width, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
