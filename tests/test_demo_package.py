"""Public synthetic samples: real exporters, no customer database or delivery writes."""

import csv
import json
import sqlite3
from dataclasses import replace
from io import BytesIO, StringIO
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from app.config import WebConfig
from app.main import create_app
from app.routes import demo
from core.delivery_memory import DeliveryClassifier
from core.demo import DEMO_PACKAGE_DATE, DEMO_PACKAGE_TRADE, demo_package_items
from core.export_package import EXPORT_COLUMNS, SHEET_NAMES
from core.models import DeliveryCategory
from core.stages import get_trade, is_due
from core.storage import LeadRepository
from tests.test_landing_page import PageParser


@pytest.fixture
def demo_client(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("Demo must not touch SQLite or delivery memory")

    demo._artifact.cache_clear()
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(LeadRepository, "__init__", forbidden)
    monkeypatch.setattr(DeliveryClassifier, "classify", forbidden)
    monkeypatch.chdir(tmp_path)
    with TestClient(
        create_app(WebConfig(database_path=tmp_path / "never.sqlite")),
        raise_server_exceptions=False,
    ) as client:
        yield client
    demo._artifact.cache_clear()
    assert not (tmp_path / "never.sqlite").exists()
    assert not (tmp_path / "data").exists()


def csv_rows(response):
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(StringIO(response.content.decode("utf-8-sig"), newline="")))


def test_fixed_demo_has_three_fictional_scenarios_without_contacts_or_maps():
    items = demo_package_items()
    assert len(items) == 3 and [i.category for i in items] == list(DeliveryCategory)
    assert items[0].investment.status == "decyzja"
    assert items[1].investment.status == "decyzja"
    assert items[2].status_history
    for item in items:
        lead = item.investment
        assert lead.id_sprawy.startswith("PRZYKŁAD/")
        assert "DANE FIKCYJNE" in lead.nazwa_zamierzenia
        assert all(
            getattr(lead, key) is None
            for key in (
                "email",
                "telefon",
                "projektant",
                "projektant_uprawnienia",
                "lat",
                "lon",
                "google_maps_url",
                "geoportal_url",
            )
        )
        assert not lead.dzialki
        assert lead.inwestor is None or lead.inwestor.startswith("Fikcyjn")
    assert demo_package_items() == items


def test_downloads_keep_production_schema_and_same_three_records(demo_client):
    csv_response = demo_client.get("/przyklad.csv")
    rows = csv_rows(csv_response)
    assert tuple(rows[0]) == EXPORT_COLUMNS and len(rows) == 4
    assert [r[0] for r in rows[1:]] == [
        i.investment.id_sprawy for i in demo_package_items()
    ]
    assert [r[1] for r in rows[1:]] == ["new", "change", "full"]
    assert "Przykładowo" in csv_response.text and "DANE FIKCYJNE" in csv_response.text
    assert csv_response.content.count(b"\xef\xbb\xbf") == 1
    xlsx_response = demo_client.get("/przyklad.xlsx")
    assert xlsx_response.status_code == 200
    workbook = load_workbook(BytesIO(xlsx_response.content), data_only=False)
    try:
        assert workbook.sheetnames == [name for _, name in SHEET_NAMES]
        exported = []
        for category, name in SHEET_NAMES:
            sheet_rows = list(workbook[name].values)
            assert tuple(sheet_rows[0]) == EXPORT_COLUMNS and len(sheet_rows) == 2
            assert sheet_rows[1][1] == category.value
            exported.append(sheet_rows[1][0])
        assert exported == [r[0] for r in rows[1:]]
        history = rows[3][EXPORT_COLUMNS.index("Historia statusów")]
        assert json.loads(history)[-1]["nowy_status"] == "decyzja"
    finally:
        workbook.close()


def test_demo_dates_support_the_exported_trade_window(demo_client):
    rows = csv_rows(demo_client.get("/przyklad.csv"))
    for row in rows[1:]:
        if "od daty decyzji" in row[EXPORT_COLUMNS.index("Kiedy dzwonić")]:
            assert row[EXPORT_COLUMNS.index("Data decyzji")]
    trade = get_trade(DEMO_PACKAGE_TRADE)
    assert trade is not None
    assert all(
        is_due(trade, item.investment, DEMO_PACKAGE_DATE)
        for item in demo_package_items()
    )


@pytest.mark.parametrize(
    "extension,media",
    [
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("csv", "text/csv"),
    ],
)
def test_demo_attachment_headers_and_head_without_database(
    demo_client, extension, media
):
    response = demo_client.get("/przyklad." + extension)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media)
    assert (
        response.headers["content-disposition"]
        == f'attachment; filename="zolta-tablica-przyklad-dane-fikcyjne.{extension}"'
    )
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    head = demo_client.head("/przyklad." + extension)
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) == len(response.content)
    assert (
        demo_client.get("/przyklad." + extension + "?filename=private.sqlite").content
        == response.content
    )


def test_routes_use_existing_exporters_with_fixed_context(demo_client, monkeypatch):
    seen = []
    for extension in ("xlsx", "csv"):
        original = getattr(demo, "write_" + extension)

        def spy(items, destination, *, trade_key, as_of, original=original):
            seen.append((items, trade_key, as_of))
            return original(items, destination, trade_key=trade_key, as_of=as_of)

        monkeypatch.setattr(demo, "write_" + extension, spy)
        assert demo_client.get("/przyklad." + extension).status_code == 200
    assert len(seen) == 2
    assert all(
        items == demo_package_items()
        and trade == DEMO_PACKAGE_TRADE
        and day == DEMO_PACKAGE_DATE
        for items, trade, day in seen
    )


@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_downloads_keep_formula_like_source_text_literal(
    demo_client, monkeypatch, prefix
):
    items = list(demo_package_items())
    text = prefix + "1+1"
    items[0] = replace(
        items[0], investment=replace(items[0].investment, nazwa_zamierzenia=text)
    )
    monkeypatch.setattr(demo, "demo_package_items", lambda: tuple(items))
    rows = csv_rows(demo_client.get("/przyklad.csv"))
    assert rows[1][EXPORT_COLUMNS.index("Nazwa zamierzenia")] == "'" + text
    workbook = load_workbook(
        BytesIO(demo_client.get("/przyklad.xlsx").content), data_only=False
    )
    try:
        cell = workbook["1. Nowe"].cell(
            2, EXPORT_COLUMNS.index("Nazwa zamierzenia") + 1
        )
        assert cell.data_type == "s" and cell.value == text
    finally:
        workbook.close()


def test_generation_failure_is_safe_and_retry_does_not_touch_delivery_memory(
    demo_client, monkeypatch
):
    original = demo.write_xlsx

    def fail(*args, **kwargs):
        raise OSError("synthetic-export-failure")

    monkeypatch.setattr(demo, "write_xlsx", fail)
    response = demo_client.get("/przyklad.xlsx")
    assert (
        response.status_code == 500 and "synthetic-export-failure" not in response.text
    )
    monkeypatch.setattr(demo, "write_xlsx", original)
    assert demo_client.get("/przyklad.xlsx").status_code == 200


def test_preview_is_synthetic_native_html_and_escapes_source(demo_client, monkeypatch):
    items = list(demo_package_items())
    unsafe = '<img src=x onerror="synthetic()">'
    items[0] = replace(
        items[0], investment=replace(items[0].investment, nazwa_zamierzenia=unsafe)
    )
    monkeypatch.setattr(demo, "demo_package_items", lambda: tuple(items))
    response = demo_client.get("/przyklad")
    assert response.status_code == 200
    page = PageParser(response.text)
    assert (
        "fikcyjne" in response.text.lower()
        and "Nowe" in response.text
        and "Pełna baza" in response.text
    )
    assert unsafe not in response.text and "&lt;img" in response.text
    assert not any(
        tag == "script" or any(k.startswith("on") for k in attrs)
        for tag, attrs in page.elements
    )
    links = {urlsplit(attrs["href"]).path for tag, attrs in page.elements if tag == "a"}
    assert {"/przyklad.xlsx", "/przyklad.csv", "/zamowienie"} <= links


def test_sample_ctas_are_available_on_landing_and_beside_estimate(demo_client):
    home = PageParser(demo_client.get("/").text)
    assert any(
        tag == "a" and urlsplit(attrs.get("href", "")).path == "/przyklad"
        for tag, attrs in home.elements
    )
    configurator = demo_client.get("/zamowienie")
    assert configurator.status_code == 200
    links = [
        attrs
        for tag, attrs in PageParser(configurator.text).elements
        if tag == "a" and urlsplit(attrs.get("href", "")).path == "/przyklad"
    ]
    assert len(links) == 1
    assert (
        links[0].get("target") == "_blank"
        and "noopener" in links[0].get("rel", "").split()
    )
    assert "nowa karta" in configurator.text.lower()
