from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect


def test_sample_retains_native_filters_and_downloads_both_formats(browser, app_server):
    with browser.new_context(
        java_script_enabled=False,
        viewport={"width": 390, "height": 844},
        accept_downloads=True,
    ) as context:
        page = context.new_page()
        page.goto(app_server.url + "/zamowienie")
        page.select_option("#branza", "dach")
        page.locator('input[name=powiaty][value="2814"]').check()
        for category in page.locator("input[name=kategorie]").all():
            category.uncheck()
        page.locator('input[name=kategorie][value="komercyjna"]').check()
        page.locator("#promien_km").fill("10")
        with page.expect_popup() as popup:
            page.get_by_role(
                "link", name="Fikcyjna próbka XLSX/CSV (nowa karta)", exact=True
            ).click()
        sample = popup.value
        expect(sample.locator("h1")).to_contain_text("Dane fikcyjne")
        expect(sample.locator("details.lead-example")).to_have_count(3)
        expect(page.locator("#branza")).to_have_value("dach")
        expect(page.locator('input[name=powiaty][value="2814"]')).to_be_checked()
        expect(page.locator("input[name=kategorie]:checked")).to_have_count(1)
        expect(page.locator("#promien_km")).to_have_value("10")
        for extension in ("xlsx", "csv"):
            with sample.expect_download() as transfer:
                sample.get_by_role(
                    "link", name="Fikcyjny " + extension.upper(), exact=True
                ).click()
            download = transfer.value
            assert (
                download.suggested_filename
                == "zolta-tablica-przyklad-dane-fikcyjne." + extension
            )
            assert download.failure() is None
            payload = Path(download.path()).read_bytes()
            assert payload.startswith(b"PK" if extension == "xlsx" else b"\xef\xbb\xbf")
        expect(sample.locator("h1")).to_contain_text("Dane fikcyjne")
