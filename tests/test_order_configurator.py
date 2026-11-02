import sqlite3
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import WebConfig
from app.main import create_app
from core.instance import database_replacement_lock, engine_process_lock
from core.models import Investment
from core.storage import LeadRepository


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synthetic.sqlite"
    with LeadRepository(path) as repo:
        repo.upsert(Investment(id_sprawy="DEMO", zrodlo="pozwolenia", status="decyzja",
            powiat_teryt="2814", powiat="Powiat fikcyjny", kategoria="komercyjna",
            nazwa_zamierzenia="Fikcyjna budowa"))
    return path


@pytest.fixture
def client(database):
    with TestClient(create_app(WebConfig(database_path=database))) as http:
        yield http


def test_form_renders_current_brand_and_controls(client):
    response = client.get("/zamowienie")
    assert response.status_code == 200
    assert "Żółta Tablica" in response.text and "Powiat fikcyjny" in response.text
    assert '<form' in response.text and 'name="wojewodztwo"' in response.text
    assert 'name="branza"' in response.text and 'name="promien_km"' in response.text
    assert "/static/order-configurator.js" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_json_estimate_returns_only_aggregate_information_and_writes_nothing(client, database):
    before = database.read_bytes()
    response = client.post("/api/estimate", json={"wojewodztwo": "28", "branza": "materialy"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1 and data["with_parcel_coordinates"] == 0
    assert set(data) == {"count", "with_parcel_coordinates", "without_parcel_coordinates", "last_seen"}
    assert "DEMO" not in response.text and "Fikcyjna budowa" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert database.read_bytes() == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM b2b_orders").fetchone()[0] == 0


def test_native_post_estimates_without_creating_order(client):
    response = client.post("/zamowienie", data={"wojewodztwo": "28", "branza": "materialy", "kategorie": "komercyjna"})
    assert response.status_code == 200
    assert "Pasujące inwestycje" in response.text and 'id="estimate-count">1<' in response.text


def test_native_form_preserves_county_when_metadata_read_loses_guard(client, database, monkeypatch):
    from contextlib import ExitStack
    from html.parser import HTMLParser

    from app.estimator import Estimator

    class SelectedCounties(HTMLParser):
        values = None

        def handle_starttag(self, tag, attrs):
            fields = dict(attrs)
            if tag == "option" and "selected" in fields and "data-region" in fields:
                self.values.append(fields["value"])

    with LeadRepository(database) as repo:
        repo.upsert(Investment(id_sprawy="OTHER", zrodlo="pozwolenia", status="decyzja",
            powiat_teryt="2801", kategoria="komercyjna"))
    payload = {"wojewodztwo": "28", "branza": "materialy", "kategorie": "komercyjna", "powiaty": "2814"}
    original = Estimator.estimate
    with ExitStack() as guards, monkeypatch.context() as patch:
        def estimate_then_lock(self, filters):
            result = original(self, filters)
            guards.enter_context(database_replacement_lock(database))
            return result

        patch.setattr(Estimator, "estimate", estimate_then_lock)
        response = client.post("/zamowienie", data=payload)
    assert response.status_code == 200
    assert 'id="estimate-count">1<' in response.text
    assert 'data-ready="true"' in response.text
    parser = SelectedCounties()
    parser.values = []
    parser.feed(response.text)
    assert parser.values == ["2814"]
    # Resubmit the county actually present in the rendered form after unlock.
    payload["powiaty"] = parser.values
    retry = client.post("/zamowienie", data=payload)
    assert retry.status_code == 200 and 'id="estimate-count">1<' in retry.text


@pytest.mark.parametrize("payload", (
    {"wojewodztwo": "28' OR 1=1", "branza": "materialy"},
    {"wojewodztwo": "28", "branza": "unknown"},
    {"wojewodztwo": "28", "branza": "materialy", "powiaty": ["1401"]},
    {"wojewodztwo": "28", "branza": "materialy", "customer_id": 5},
    {"wojewodztwo": "28", "branza": "materialy", "lat": 53},
    {"wojewodztwo": "28", "branza": "materialy", "lat": True, "lon": 20, "promien_km": 20},
    {"wojewodztwo": "28", "branza": "materialy", "kategorie": []},
))
def test_invalid_or_extra_filters_fail_closed(client, payload):
    assert client.post("/api/estimate", json=payload).status_code == 422


def test_bounded_body_and_content_type(client):
    assert client.post("/api/estimate", content=b"x" * 8193, headers={"Content-Type": "application/json"}).status_code == 413
    assert client.post("/api/estimate", content="{not json", headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post("/api/estimate", content="test", headers={"Content-Type": "text/plain"}).status_code == 415


def test_unconfigured_or_missing_database_is_unavailable_not_zero(tmp_path):
    for config in (WebConfig(), WebConfig(database_path=tmp_path / "absent" / "never.sqlite")):
        with TestClient(create_app(config)) as http:
            assert http.get("/").status_code == http.get("/zdrowie").status_code == 200
            response = http.post("/api/estimate", json={"wojewodztwo": "28", "branza": "materialy"})
            assert response.status_code == 503 and "count" not in response.json()
    assert not (tmp_path / "absent").exists()


def test_replacement_guard_blocks_estimate_but_engine_import_does_not(client, database):
    payload = {"wojewodztwo": "28", "branza": "materialy"}
    with database_replacement_lock(database):
        response = client.post("/api/estimate", json=payload)
        assert response.status_code == 503 and str(database) not in response.text
    with engine_process_lock(database), sqlite3.connect(database) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE investments SET is_noise=1")
        assert client.post("/api/estimate", json=payload).json()["count"] == 1
        writer.rollback()


def test_wrong_schema_has_generic_unavailable_response(client, database):
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA user_version=1")
    response = client.post("/api/estimate", json={"wojewodztwo": "28", "branza": "materialy"})
    assert response.status_code == 503
    assert str(database) not in response.text and "sqlite" not in response.text.lower()


def test_imported_county_label_is_escaped_and_native_form_validation_is_closed(client, database):
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE investments SET powiat=?", ('<script>alert("synthetic")</script>',))
    response = client.get("/zamowienie")
    assert '&lt;script&gt;' in response.text and '<script>alert' not in response.text
    invalid = client.post("/zamowienie", data={"wojewodztwo": "28", "branza": "materialy"})
    assert invalid.status_code == 422  # No checked investment types.
    assert 'id="estimate-result" hidden' in invalid.text


def test_web_database_setting_is_explicit_and_does_not_load_engine_env(tmp_path):
    config = WebConfig.from_env({"GUNB_WEB_DB_PATH": str(tmp_path / "x.sqlite")})
    assert config.database_path == tmp_path / "x.sqlite"
    assert replace(config, database_path=None).database_path is None


def test_read_service_is_really_readonly_and_holds_guard_until_connection_closes(database):
    from app.estimator import Estimator
    from core.instance import database_replacement_lock_path, lock_is_free

    def inspect(conn):
        assert not lock_is_free(database_replacement_lock_path(database))
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM investments")
        return conn.execute("SELECT COUNT(*) FROM investments").fetchone()[0]

    assert Estimator(database)._read(inspect) == 1
    assert lock_is_free(database_replacement_lock_path(database))


def test_failed_query_budget_releases_guard_and_connection(database):
    from app.estimator import EstimateUnavailable, Estimator
    from core.instance import database_replacement_lock_path, lock_is_free

    with pytest.raises(EstimateUnavailable):
        Estimator(database, query_seconds=0)._read(lambda conn: conn.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT SUM(x) FROM n"
        ).fetchone())
    assert lock_is_free(database_replacement_lock_path(database))


def test_symlink_alias_uses_same_actual_replacement_guard(database, tmp_path):
    alias = tmp_path / "alias" / "db.sqlite"
    alias.parent.mkdir()
    try:
        alias.symlink_to(database)
    except OSError:
        pytest.skip("Symlink creation unavailable; exercised on Linux CI")
    with TestClient(create_app(WebConfig(database_path=alias))) as http, database_replacement_lock(database):
        assert http.post("/api/estimate", json={"wojewodztwo": "28", "branza": "materialy"}).status_code == 503
