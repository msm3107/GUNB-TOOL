"""Public-page contracts: safe samples, usable navigation and honest availability."""

from html.parser import HTMLParser
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


class PageParser(HTMLParser):
    """Collect rendered landmarks and native disclosures, without a web dependency."""

    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.details = []
        self.current_detail = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if tag == "details":
            assert self.current_detail is None, "Disclosures must not be nested"
            self.current_detail = {"attrs": attrs, "text": "", "summaries": 0}
        elif tag == "summary" and self.current_detail is not None:
            self.current_detail["summaries"] += 1

    def handle_data(self, data):
        if self.current_detail is not None:
            self.current_detail["text"] += data

    def handle_endtag(self, tag):
        if tag == "details":
            self.details.append(self.current_detail)
            self.current_detail = None


@pytest.fixture
def landing(monkeypatch):
    monkeypatch.delenv("GUNB_WEB_CORS_ORIGINS", raising=False)
    with TestClient(create_app()) as http:
        response = http.get("/")
        assert response.status_code == 200
        yield http, response, PageParser(response.text)


def test_home_has_named_sections_unique_ids_and_semantic_landmarks(landing):
    _, response, page = landing
    assert response.template.name == "index.html"
    tags = [tag for tag, _ in page.elements]
    assert tags.count("h1") == tags.count("main") == 1
    assert "nav" in tags and "footer" in tags
    ids = [attrs["id"] for _, attrs in page.elements if "id" in attrs]
    assert len(ids) == len(set(ids))
    assert {"jak-to-dziala", "przyklady", "oferta", "pytania"} <= set(ids)
    for tag, attrs in page.elements:
        if tag == "section":
            assert attrs.get("aria-labelledby") in ids


def test_public_examples_are_synthetic_disclosures_with_quality_and_timing(landing):
    _, response, page = landing
    samples = [item for item in page.details if "lead-example" in item["attrs"].get("class", "").split()]
    assert len(samples) == 3
    for sample in samples:
        assert sample["summaries"] == 1
        assert "Fikcyjny przykład" in sample["text"]
        assert "Dlaczego pasuje" in sample["text"]
        assert "Szacunek" in sample["text"]
        assert "Źródło" in sample["text"] and "Jakość lokalizacji" in sample["text"]
        assert "nie potwierdza rozpoczęcia robót" in sample["text"]
        assert "@" not in sample["text"] and "http" not in sample["text"]
    assert "brak współrzędnych działki" in response.text
    assert "Nie są to rzeczywiste oferty ani dane konkretnych inwestorów" in response.text


def test_planned_offers_have_no_purchase_flow_or_invented_price(landing):
    http, response, page = landing
    assert "Paczka jednorazowa" in response.text and "Abonament" in response.text
    assert "Cena przed uruchomieniem sprzedaży" in response.text
    assert "Zamówienia i płatności online nie są jeszcze dostępne" in response.text
    tags = [tag for tag, _ in page.elements]
    assert "table" in tags and "caption" in tags
    assert all(attrs.get("scope") in {"col", "row"} for tag, attrs in page.elements if tag == "th")
    assert not {"form", "input", "button"} & set(tags)
    for path in ("/zamowienie", "/logowanie"):
        assert http.get(path).status_code == 404


def test_all_navigation_targets_and_assets_resolve_without_external_requests(landing):
    http, _, page = landing
    ids = {attrs["id"] for _, attrs in page.elements if "id" in attrs}
    for tag, attrs in page.elements:
        assert not any(name.startswith("on") for name in attrs)
        assert "style" not in attrs
        assert tag not in {"script", "iframe"}
        for attribute in ("href", "src"):
            if attribute not in attrs:
                continue
            url = urlsplit(attrs[attribute])
            assert url.hostname in {None, "testserver"}
            assert url.scheme in {"", "http"}
            if url.fragment:
                assert url.fragment in ids
            else:
                assert http.get(attrs[attribute]).status_code == 200


def test_faq_uses_native_summary_controls(landing):
    _, _, page = landing
    faq = [item for item in page.details if "faq-item" in item["attrs"].get("class", "").split()]
    assert len(faq) >= 4
    assert all(item["summaries"] == 1 for item in faq)


def test_public_landing_does_not_read_database(monkeypatch, tmp_path):
    import sqlite3

    def forbidden_database(*args, **kwargs):
        raise AssertionError("Public examples must not access the investment database")

    monkeypatch.setattr(sqlite3, "connect", forbidden_database)
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as http:
        response = http.get("/")
    assert response.status_code == 200
    assert "Fikcyjny przykład" in response.text
    assert not (tmp_path / "data").exists()
