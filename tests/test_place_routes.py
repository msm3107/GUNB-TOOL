from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import WebConfig
from app.main import create_app
from core.models import Investment
from core.places import import_catalog
from core.storage import LeadRepository
from tests.test_places import DAY, gml


@pytest.fixture
def place_app(tmp_path):
    source, catalog, database = (
        tmp_path / name for name in ("names.gml", "places.sqlite", "leads.sqlite")
    )
    source.write_text(
        gml(
            [
                ("2", "Łódź", "Gmina A", "Powiat A", "2814011", "53 20"),
                ("3", "Łódź", "Gmina B", "Powiat B", "2801011", "53.5 20.5"),
            ]
        ),
        encoding="utf-8",
    )
    version = import_catalog(source, catalog, source_date=DAY)
    with LeadRepository(database) as repo:
        for key, county, precision in (
            ("near", "2814", "dzialka"),
            ("other", "1401", "dzialka"),
            ("imprecise", "2814", "gmina"),
        ):
            repo.upsert(
                Investment(
                    id_sprawy=key,
                    zrodlo="pozwolenia",
                    status="decyzja",
                    powiat_teryt=county,
                    kategoria="komercyjna",
                    lat=53,
                    lon=20,
                    precyzja_geo=precision,
                    gmina_teryt=county + "011",
                    teryt_dzialki=county + "01_1.0001.1",
                )
            )
    with TestClient(
        create_app(WebConfig(database_path=database, places_path=catalog))
    ) as client:
        yield SimpleNamespace(
            client=client,
            version=version,
            catalog=catalog,
            database=database,
            source=source,
        )


def chosen(app, **changes):
    return {
        "wojewodztwo": "28",
        "branza": "materialy",
        "kategorie": ["komercyjna"],
        "place_query": "Łódź",
        "place_id": "2",
        "place_version": app.version,
        "promien_km": 10,
        **changes,
    }


def test_place_api_disambiguates_names_without_writes(place_app):
    app = place_app
    before = app.catalog.read_bytes(), app.database.read_bytes()
    response = app.client.get("/api/places", params={"q": "lodz", "region": "28"})
    assert response.status_code == 200
    places = response.json()["places"]
    assert [p["id"] for p in places] == ["2", "3"]
    assert places[0]["municipality"] != places[1]["municipality"]
    assert places[0]["source_version"] == app.version
    assert app.client.get("/api/places?q=nieistnieje").json() == {"places": []}
    assert response.headers["cache-control"] == "no-store"
    assert before == (app.catalog.read_bytes(), app.database.read_bytes())


@pytest.mark.parametrize(
    "query", ["q=x", "q=" + "a" * 81, "q=lodz&region=99", "q=lodz&q=inne"]
)
def test_place_api_validates_bounded_search(place_app, query):
    assert place_app.client.get("/api/places?" + query).status_code == 422


def test_absent_catalog_is_503_and_gps_still_works(place_app):
    with TestClient(create_app(WebConfig(database_path=place_app.database))) as client:
        assert client.get("/api/places?q=lodz").status_code == 503
        assert client.post("/api/estimate", json=chosen(place_app)).status_code == 503
        assert (
            client.post(
                "/api/estimate",
                json={
                    "wojewodztwo": "28",
                    "branza": "materialy",
                    "lat": 53,
                    "lon": 20,
                    "promien_km": 10,
                },
            ).status_code
            == 200
        )


def test_place_resolves_on_server_and_retains_radius_quality_rules(place_app):
    response = place_app.client.post("/api/estimate", json=chosen(place_app))
    assert response.status_code == 200
    assert response.json()["count"] == response.json()["with_parcel_coordinates"] == 1
    assert len(response.json()) == 4
    assert (
        place_app.client.post(
            "/api/estimate", json=chosen(place_app, place_id="3")
        ).json()["count"]
        == 0
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"lat": 53, "lon": 20},
        {"place_version": "old"},
        {"place_id": "missing"},
        {"place_id": None},
        {"promien_km": None},
    ],
)
def test_place_conflicts_and_missing_choices_fail_closed(place_app, changes):
    response = place_app.client.post("/api/estimate", json=chosen(place_app, **changes))
    assert response.status_code == 422
    assert response.json()["detail"] and response.json()["field_errors"]


def test_native_place_search_and_selection_preserve_scope(place_app):
    app = place_app
    values = {
        "wojewodztwo": "28",
        "branza": "materialy",
        "powiaty": "2814",
        "kategorie": "komercyjna",
        "place_query": "Łódź",
        "place_action": "search",
        "promien_km": "10",
    }
    response = app.client.post("/zamowienie", data=values)
    assert response.status_code == 200
    assert "Gmina A" in response.text and "Gmina B" in response.text
    assert 'name="place_id"' in response.text and app.version in response.text
    values.pop("place_action")
    values.update(place_id="2", place_version=app.version)
    response = app.client.post("/zamowienie", data=values)
    assert response.status_code == 200
    assert 'id="estimate-count">1<' in response.text
    assert 'value="2814"' in response.text and 'value="10.0"' in response.text


def test_old_catalog_version_never_uses_cached_coordinates(place_app):
    app = place_app
    app.source.write_text(
        app.source.read_text(encoding="utf-8").replace("Gmina A", "Gmina C"),
        encoding="utf-8",
    )
    import_catalog(app.source, app.catalog, source_date=DAY)
    response = app.client.post("/api/estimate", json=chosen(app))
    assert response.status_code == 422 and "place_id" in response.json()["field_errors"]


def test_test_checkout_resolves_the_same_place_without_provider_io(place_app):
    calls = []

    class FakeCheckout:
        settings = SimpleNamespace(test_mode=True, origin="https://example.test")

        def start(self, **values):
            calls.append(values)
            return "https://checkout.stripe.com/c/pay/synthetic"

    with TestClient(
        create_app(
            WebConfig(database_path=place_app.database, places_path=place_app.catalog),
            checkout_service=FakeCheckout(),
        )
    ) as client:
        body = {
            "email": "person@example.test",
            "kind": "paczka",
            "request_token": "11111111-1111-4111-8111-111111111111",
            "filters": chosen(place_app),
        }
        response = client.post(
            "/zamowienie/checkout",
            json=body,
            headers={"Origin": "https://example.test"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert (calls[0]["filters"].lat, calls[0]["filters"].lon) == (53, 20)
        assert calls[0]["filters"].promien_km == 10
        body["filters"]["lat"] = 54
        assert (
            client.post(
                "/zamowienie/checkout",
                json=body,
                headers={"Origin": "https://example.test"},
                follow_redirects=False,
            ).status_code
            == 422
        )
        assert len(calls) == 1
