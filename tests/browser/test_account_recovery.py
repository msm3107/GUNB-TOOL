"""Actual browser account navigation/native Checkout with local provider stubs."""
import json
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from core.payment_store import PaidCheckout, PaymentStore
from tests.browser.test_package_preview import package_server as _build_package
from tests.test_auth_service import NOW


@pytest.mark.parametrize("app_server", [True], indirect=True)
@pytest.mark.parametrize("javascript", [True, False])
@pytest.mark.parametrize("width", [390, 1280])
def test_package_login_native_retry_and_owned_billing(browser, app_server, javascript, width, browser_name):
    server = _build_package.__wrapped__(app_server)
    target = "/panel/paczki/1?page=2&category=all&case_id=CASE%2F051"
    with browser.new_context(java_script_enabled=javascript, viewport={"width": width, "height": 844}) as context:
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(server.url + "/") else route.abort())
        page = context.new_page()
        captured = []
        def checkout_transport(route):
            # Playwright routes only the first URL in a redirected request.
            # Stop at the original POST; never follow the external Location.
            response = route.fetch(max_redirects=0, headers={**route.request.headers, "connection": "close"})
            try:
                if response.status == 303:
                    captured.append(response.headers["location"])
                    route.fulfill(status=200, body="<h1>Checkout redirect captured</h1>", content_type="text/html; charset=utf-8")
                else:
                    route.fulfill(response=response)
            finally:
                response.dispose()
        page.route("**/zamowienie/checkout", checkout_transport)
        page.goto(server.url + target)
        expect(page.get_by_role("heading", name="Zaloguj się bez hasła.")).to_be_visible()
        page.get_by_label("Adres e-mail", exact=True).fill("person@example.test")
        page.get_by_role("button", name="Wyślij link").click()
        expect(page.get_by_role("status")).to_contain_text("Jeśli możemy")
        assert server.delivery_done.wait(5)
        link = next(line for line in server.sender.messages[-1][2].splitlines() if line.startswith(server.url + "/auth/verify"))
        page.goto(link)
        if not javascript:
            page.get_by_label("Kod z końca linku", exact=False).fill(link.split("#")[1])
        page.get_by_role("button", name="Potwierdź logowanie", exact=True).click()
        expect(page).to_have_url(server.url + target)
        expect(page.locator(".package-row[open]")).to_contain_text("CASE/051")

        page.goto(server.url + "/zamowienie")
        page.select_option("#branza", "dach")
        page.locator('input[name=powiaty][value="2814"]').check()
        for category in page.locator("input[name=kategorie]").all():
            category.uncheck()
        page.locator('input[name=kategorie][value="komercyjna"]').check()
        page.get_by_role("button", name="Przejdź do testowego zamówienia").click()
        expect(page.get_by_role("heading", name="Sprawdź testowe zamówienie.")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        shots = Path(__file__).resolve().parents[2] / ".local-review"
        if browser_name == "chromium" and javascript:
            shots.mkdir(exist_ok=True)
            page.screenshot(path=str(shots / f"PR37-summary-{width}.png"), full_page=True)
        page.get_by_label("Rodzaj testowej paczki").select_option("abonament")
        token = page.locator('input[name=request_token]').input_value()
        filters = json.loads(page.locator('input[name=filters]').input_value())
        assert filters["branza"] == "dach" and filters["powiaty"] == ["2814"] and filters["kategorie"] == ["komercyjna"]
        page.get_by_role("button", name="Przejdź do płatności testowej").click()
        expect(page.get_by_role("status")).to_contain_text("Nie znamy wyniku")
        expect(page.locator('input[name=request_token]')).to_have_value(token)
        expect(page.get_by_label("Rodzaj testowej paczki")).to_have_value("abonament")
        page.get_by_role("button", name="Ponów tę samą próbę").click()
        expect(page.get_by_role("heading", name="Checkout redirect captured")).to_be_visible()
        assert captured == ["https://checkout.stripe.com/test-owned"]
        assert len(server.checkout_api.calls) == 2
        assert server.checkout_api.calls[0][1] == server.checkout_api.calls[1][1]

        params = server.checkout_api.calls[-1][0]
        metadata = params["metadata"]
        paid = PaidCheckout(metadata["checkout_key"], "cs_test_native", metadata["order_id"], metadata["customer_id"], "subscription", "pln", 23456, int(NOW.timestamp()), "cus_native", "sub_native")
        PaymentStore(server.path).record_event(event_id="evt_native", event_type="checkout.session.completed", completion=paid, now=NOW)
        page.goto(server.url + "/panel")
        page.get_by_role("button", name="Odśwież status abonamentu", exact=True).click()
        expect(page.locator(".dashboard-subscription")).to_contain_text("Aktywny")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if browser_name == "chromium" and javascript:
            page.screenshot(path=str(shots / f"PR37-billing-{width}.png"), full_page=True)
