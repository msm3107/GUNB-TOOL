import json

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

KEY = "zt:filter-draft:v1"
RESULT = {
    "count": 1,
    "with_parcel_coordinates": 0,
    "without_parcel_coordinates": 1,
    "last_seen": None,
}


def ready(page, server):
    page.goto(server.url + "/zamowienie")
    expect(page.locator("#estimate-result")).to_be_visible()


@pytest.mark.parametrize("late_status", [200, 503])
def test_late_result_is_ignored(page, app_server, late_status):
    page.add_init_script(
        "window.AbortController=class extends AbortController { abort() {} };"
    )
    held = []

    def respond(route):
        if route.request.post_data_json["branza"] == "materialy":
            held.append(route)
        else:
            route.fulfill(json={**RESULT, "count": 42})

    page.route("**/api/estimate", respond)
    page.goto(app_server.url + "/zamowienie")
    page.wait_for_function(
        "document.querySelector('#package-form').getAttribute('aria-busy') === 'true'"
    )
    page.select_option("#branza", "dach")
    expect(page.locator("#estimate-count")).to_have_text("42")
    with page.expect_response(
        lambda r: (
            r.request.post_data_json.get("branza") == "materialy"
            if r.request.method == "POST"
            else False
        )
    ):
        held[0].fulfill(
            status=late_status, json={**RESULT, "count": 99, "detail": "Old failure"}
        )
    page.evaluate("() => new Promise(requestAnimationFrame)")
    expect(page.locator("#estimate-count")).to_have_text("42")


@pytest.mark.parametrize("county", ["2814", "2815"])
def test_draft_restore_keeps_exact_scope(page, app_server, county):
    filters = {
        "wojewodztwo": "28",
        "branza": "materialy",
        "powiaty": [county],
        "kategorie": ["komercyjna"],
        "lat": 53.0,
        "lon": 20.0,
        "promien_km": 10.0,
    }
    page.add_init_script(
        "sessionStorage.setItem("
        + json.dumps(KEY)
        + ","
        + json.dumps(json.dumps({"version": 1, "filters": filters}))
        + ");"
    )
    with page.expect_request("**/api/estimate") as request:
        page.goto(app_server.url + "/zamowienie")
    assert request.value.post_data_json == filters
    expect(page.locator("input[name=powiaty]:checked")).to_have_count(1)
    assert page.locator("input[name=powiaty]:checked").input_value() == county
    expect(page.locator("#estimate-result")).to_be_visible()
    with page.expect_request("**/api/estimate") as request:
        page.reload()
    assert request.value.post_data_json == filters


def test_storage_denied_still_estimates(page, app_server):
    page.add_init_script(
        "Object.defineProperty(window,'sessionStorage',{get(){throw new DOMException('denied','SecurityError')}})"
    )
    ready(page, app_server)
    expect(page.locator("#draft-status")).to_contain_text("Obliczenia nadal działają")
    page.locator('input[name=powiaty][value="2814"]').check()
    with page.expect_request("**/api/estimate") as request:
        page.get_by_role("button", name="Policz inwestycje").click()
    assert request.value.post_data_json["powiaty"] == ["2814"]
    expect(page.locator("#estimate-result")).to_be_visible()


def test_timeout_keeps_filters_and_hides_stale_result(page, app_server):
    page.add_init_script(
        "window.AbortController=class extends AbortController { abort() {} };"
    )
    page.clock.install()
    held = []
    calls = []

    def respond(route):
        calls.append(route)
        if len(calls) == 2:
            held.append(route)
        else:
            route.fulfill(json=RESULT)

    page.route("**/api/estimate", respond)
    ready(page, app_server)
    page.select_option("#branza", "dach")
    page.clock.fast_forward(401)
    expect(page.locator("#package-form")).to_have_attribute("aria-busy", "true")
    page.clock.fast_forward(10001)
    expect(page.locator("#estimate-status")).to_contain_text("Przekroczono czas")
    expect(page.locator("#estimate-result")).to_be_hidden()
    expect(page.locator("#branza")).to_have_value("dach")
    held[0].fulfill(json={**RESULT, "count": 999})
    page.evaluate("() => new Promise(requestAnimationFrame)")
    expect(page.locator("#estimate-result")).to_be_hidden()
    page.get_by_role("button", name="Przelicz ponownie").click()
    expect(page.locator("#estimate-result")).to_be_visible()
    assert len(calls) == 3


def test_invalid_draft_never_estimates_default_scope_automatically(page, app_server):
    page.add_init_script(
        "sessionStorage.setItem("
        + json.dumps(KEY)
        + ',\'{"version":99,"filters":{}}\');'
    )
    requests = []
    page.on(
        "request",
        lambda request: (
            requests.append(request) if "/api/estimate" in request.url else None
        ),
    )
    page.goto(app_server.url + "/zamowienie")
    expect(page.locator("#draft-status")).to_contain_text("Nie przywrócono")
    assert not requests
    expect(page.locator("#estimate-result")).to_be_hidden()


def test_server_field_errors_preserve_choice_and_are_accessible(page, app_server):
    ready(page, app_server)
    page.locator('input[name=powiaty][value="2814"]').check()
    page.route(
        "**/api/estimate",
        lambda route: route.fulfill(
            status=422,
            json={
                "detail": "Popraw zakres",
                "field_errors": {"powiaty": "Zachowaj właściwy powiat"},
            },
        ),
    )
    page.get_by_role("button", name="Policz inwestycje").click()
    expect(page.locator("#filter-errors")).to_contain_text("Popraw zakres")
    expect(page.locator("#powiaty")).to_have_attribute("aria-invalid", "true")
    expect(page.locator('input[name=powiaty][value="2814"]')).to_be_checked()
    expect(page.locator("#estimate-result")).to_be_hidden()


def test_county_search_does_not_change_selected_scope(page, app_server):
    ready(page, app_server)
    page.locator('input[name=powiaty][value="2814"]').check()
    page.get_by_label("Znajdź powiat").fill("2801")
    expect(page.locator('input[name=powiaty][value="2814"]')).to_be_checked()
    expect(page.locator("#county-count")).to_contain_text("1 / 20")
    with page.expect_request("**/api/estimate") as request:
        page.get_by_role("button", name="Policz inwestycje").click()
    assert request.value.post_data_json["powiaty"] == ["2814"]


def test_mobile_zero_keeps_scope_and_keyboard_controls(page, app_server):
    page.route(
        "**/api/estimate",
        lambda route: route.fulfill(
            json={**RESULT, "count": 0, "without_parcel_coordinates": 0}
        ),
    )
    ready(page, app_server)
    county = page.locator('input[name=powiaty][value="2814"]')
    county.focus()
    page.keyboard.press("Space")
    expect(county).to_be_checked()
    page.get_by_role("button", name="Policz inwestycje").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#estimate-mobile")).to_be_visible()
    expect(page.locator("#estimate-mobile")).to_contain_text("Brak pasujących")
    expect(page.locator("#estimate-scope")).to_contain_text("2814")
    expect(page.locator("#estimate-zero")).to_contain_text("kompletności importu")
    page.get_by_role("link", name="Zmień obszar", exact=True).click()
    expect(page.locator("#powiaty")).to_be_focused()
    expect(county).to_be_checked()


def test_region_change_requires_explicit_county_clear(page, app_server):
    ready(page, app_server)
    page.locator('input[name=powiaty][value="2814"]').check()
    page.select_option("#wojewodztwo", "14")
    page.get_by_role("button", name="Policz inwestycje").click()
    expect(page.locator("#error-powiaty")).to_contain_text("innego województwa")
    expect(page.locator('input[name=powiaty][value="2814"]')).to_be_checked()
    expect(page.locator("#estimate-result")).to_be_hidden()
    page.get_by_role("button", name="Całe województwo", exact=True).click()
    expect(page.locator("#estimate-result")).to_be_visible()
    expect(page.locator("#estimate-scope")).to_contain_text("całe województwo")


def test_native_estimate_preserves_scope(browser, app_server):
    with browser.new_context(
        java_script_enabled=False, viewport={"width": 390, "height": 844}
    ) as context:
        page = context.new_page()
        page.goto(app_server.url + "/zamowienie")
        page.locator('input[name=powiaty][value="2814"]').check()
        page.get_by_role("button", name="Policz inwestycje").click()
        expect(page.locator("#estimate-count")).to_have_text("1")
        expect(page.locator('input[name=powiaty][value="2814"]')).to_be_checked()


def test_logout_clears_draft_only_after_confirmed_logout(page, app_server):
    ready(page, app_server)
    assert page.evaluate("(key)=>sessionStorage.getItem(key)", KEY)
    page.goto(app_server.url + "/logowanie")
    page.get_by_label("Adres e-mail", exact=True).fill("person@example.test")
    page.get_by_role("button", name="Wyślij link").click()
    expect(page.get_by_role("status")).to_contain_text("Jeśli możemy")
    token = app_server.sender.messages[-1][2].split("/auth/verify#")[1].split()[0]
    page.goto(app_server.url + "/auth/verify#" + token)
    page.get_by_role("button", name="Potwierdź logowanie").click()
    expect(page).to_have_url(app_server.url + "/panel")
    page.route(
        "**/auth/logout",
        lambda route: route.fulfill(status=503, body="synthetic failure"),
    )
    page.get_by_role("button", name="Wyloguj się").click()
    assert page.evaluate("(key)=>sessionStorage.getItem(key)", KEY)
    page.unroute("**/auth/logout")
    page.goto(app_server.url + "/panel")
    page.get_by_role("button", name="Wyloguj się").click()
    expect(page).to_have_url(app_server.url + "/logowanie")
    assert page.evaluate("(key)=>sessionStorage.getItem(key)", KEY) is None
