import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import expect

from core.places import import_catalog
from tests.test_places import DAY

KEY = "zt:filter-draft:v1"


def choose(page, server):
    page.goto(server.url + "/zamowienie")
    expect(page.locator("#estimate-result")).to_be_visible()
    page.locator("#place_query").fill("lodz")
    page.get_by_role("button", name="Znajdź miejscowość", exact=True).click()
    expect(page.locator("#place_id option")).to_have_count(3)
    expect(page.locator("#place_id")).to_have_value("")
    expect(page.locator("#place_id")).to_contain_text("Gmina A")
    expect(page.locator("#place_id")).to_contain_text("Gmina B")
    page.select_option("#place_id", "2")
    page.locator("#promien_km").fill("10")
    with page.expect_request("**/api/estimate") as request:
        page.get_by_role("button", name="Policz inwestycje").click()
    expect(page.locator("#estimate-result")).to_be_visible()
    return request.value.post_data_json


def test_place_picker_uses_server_point_and_restores_versioned_draft(page, app_server):
    filters = choose(page, app_server)
    assert (
        filters["place_id"] == "2"
        and filters["place_version"] == app_server.place_version
    )
    assert filters["lat"] is None and filters["lon"] is None
    stored = page.evaluate("key => JSON.parse(sessionStorage.getItem(key))", KEY)
    assert stored == {"version": 2, "filters": filters}
    with page.expect_request("**/api/estimate") as restored:
        page.reload()
    assert restored.value.post_data_json == filters
    expect(page.locator("#place_id")).to_have_value("2")
    expect(page.locator("#promien_km")).to_have_value("10")
    expect(page.locator("#estimate-result")).to_be_visible()


def test_catalog_change_requires_new_choice_without_filter_loss(page, app_server):
    filters = choose(page, app_server)
    source = app_server.place_source
    source.write_text(
        source.read_text(encoding="utf-8").replace("Gmina A", "Gmina C"),
        encoding="utf-8",
    )
    import_catalog(source, app_server.places_path, source_date=DAY)
    page.reload()
    expect(page.locator("#error-place_id")).to_contain_text("aktualnego katalogu")
    expect(page.locator("#place_id")).to_have_value("2")
    expect(page.locator("#estimate-result")).to_be_hidden()
    assert (
        page.evaluate("key => JSON.parse(sessionStorage.getItem(key)).filters", KEY)
        == filters
    )
    page.get_by_role("button", name="Przelicz ponownie").click()
    expect(page.locator("#estimate-result")).to_be_hidden()
    expect(page.locator("#promien_km")).to_have_value("10")


def test_missing_catalog_is_an_error_not_an_empty_place_list(page, app_server):
    page.goto(app_server.url + "/zamowienie")
    app_server.places_path.unlink()
    page.locator("#place_query").fill("lodz")
    page.get_by_role("button", name="Znajdź miejscowość", exact=True).click()
    expect(page.locator("#place-status")).to_contain_text("niedostępny")
    expect(page.locator("#place_query")).to_have_value("lodz")
    expect(page.locator("#place_id")).to_have_value("")


def test_place_search_timeout_keeps_filters_and_ignores_late_reply(page, app_server):
    page.add_init_script(
        "window.AbortController=class extends AbortController { abort() {} };"
    )
    page.goto(app_server.url + "/zamowienie")
    page.clock.install()
    held = []
    page.route("**/api/places?*", lambda route: held.append(route))
    page.locator("#place_query").fill("lodz")
    page.locator("#promien_km").fill("10")
    page.get_by_role("button", name="Znajdź miejscowość", exact=True).click()
    expect(page.locator("#place-fields")).to_have_attribute("aria-busy", "true")
    page.clock.fast_forward(10001)
    expect(page.locator("#place-status")).to_contain_text("Przekroczono czas")
    held[0].fulfill(json={"places": []})
    page.evaluate("() => new Promise(requestAnimationFrame)")
    expect(page.locator("#place-status")).to_contain_text("Przekroczono czas")
    expect(page.locator("#place_query")).to_have_value("lodz")
    expect(page.locator("#promien_km")).to_have_value("10")
    expect(page.locator("#place_id")).to_have_value("")


def test_place_draft_keeps_county_conflict_and_explicit_clear_keeps_radius(
    page, app_server
):
    choose(page, app_server)
    page.locator('input[name=powiaty][value="2814"]').check()
    page.select_option("#wojewodztwo", "14")
    saved = page.evaluate("key => JSON.parse(sessionStorage.getItem(key)).filters", KEY)
    page.reload()
    expect(page.locator("#error-powiaty")).to_contain_text("innego województwa")
    assert (
        page.evaluate("key => JSON.parse(sessionStorage.getItem(key)).filters", KEY)
        == saved
    )
    expect(page.locator("#place_id")).to_have_value("2")
    page.get_by_role("button", name="Usuń wybór miejscowości", exact=True).click()
    expect(page.locator("#place_id")).to_have_value("")
    expect(page.locator("#place_version")).to_have_value("")
    expect(page.locator("#promien_km")).to_have_value("10")
    expect(page.locator('input[name=powiaty][value="2814"]')).to_be_checked()


def test_late_place_search_cannot_replace_new_results(page, app_server):
    page.add_init_script(
        "window.AbortController=class extends AbortController { abort() {} };"
    )
    held = []

    def respond(route):
        if "q=Old" in route.request.url:
            held.append(route)
        else:
            route.fulfill(
                json={
                    "places": [
                        {
                            "id": "3",
                            "name": "New",
                            "municipality": "Gmina B",
                            "county": "Powiat B",
                            "region": "28",
                            "source_version": app_server.place_version,
                            "lat": 53,
                            "lon": 20,
                        }
                    ]
                }
            )

    page.route("**/api/places?*", respond)
    page.goto(app_server.url + "/zamowienie")
    page.locator("#place_query").fill("Old")
    page.get_by_role("button", name="Znajdź miejscowość", exact=True).click()
    expect(page.locator("#place-status")).to_contain_text("Szukanie")
    page.locator("#place_query").fill("New")
    page.get_by_role("button", name="Znajdź miejscowość", exact=True).click()
    expect(page.locator("#place_id option[value='3']")).to_have_text(
        "New — Gmina B, Powiat B (woj. 28)"
    )
    held[0].fulfill(json={"places": []})
    page.evaluate("() => new Promise(requestAnimationFrame)")
    expect(page.locator("#place_id option[value='3']")).to_be_attached()
    expect(page.locator("#place_id")).to_have_value("")


def test_native_place_search_and_selection_without_javascript(browser, app_server):
    with browser.new_context(java_script_enabled=False) as context:
        page = context.new_page()
        page.goto(app_server.url + "/zamowienie")
        page.locator('input[name=powiaty][value="2814"]').check()
        page.locator("#place_query").fill("lodz")
        page.get_by_role("button", name="Znajdź miejscowość", exact=True).click()
        expect(page.locator("#place_id option")).to_have_count(3)
        page.select_option("#place_id", "2")
        page.locator("#promien_km").fill("10")
        page.get_by_role("button", name="Policz inwestycje").click()
        expect(page.locator("#estimate-result")).to_be_visible()
        expect(page.locator("#place_id")).to_have_value("2")
        expect(page.locator('input[name=powiaty][value="2814"]')).to_be_checked()
        expect(
            page.get_by_role("button", name="Usuń wybór miejscowości", exact=True)
        ).to_be_visible()
        page.get_by_role("button", name="Usuń wybór miejscowości", exact=True).click()
        expect(page.locator("#place_id")).to_have_value("")
        expect(page.locator("#place_version")).to_have_value("")
        expect(page.locator("#promien_km")).to_have_value("10.0")
        expect(page.locator('input[name=powiaty][value="2814"]')).to_be_checked()
        page.locator(".radius-options summary").click()
        page.locator("#lat").fill("53")
        page.locator("#lon").fill("20")
        page.get_by_role("button", name="Policz inwestycje").click()
        expect(page.locator("#estimate-result")).to_be_visible()
