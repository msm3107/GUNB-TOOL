"""Handelski eksport sklasyfikowanych dostaw B2B."""

from __future__ import annotations

import csv
import json
from datetime import date
from io import BytesIO, StringIO
from zipfile import is_zipfile

import pytest
from openpyxl import load_workbook

from core.delivery_memory import ClassifiedDeliveryItem
from core.export_package import write_csv, write_xlsx
from core.models import DeliveryCategory, Investment
from core.storage import StatusChange


def make_item(
    case_id: str,
    category: DeliveryCategory,
    **investment_fields: object,
) -> ClassifiedDeliveryItem:
    investment = Investment(
        id_sprawy=case_id,
        zrodlo="pozwolenia",
        status="decyzja",
        **investment_fields,
    )
    return ClassifiedDeliveryItem(investment, f"revision-{case_id}", category)


def test_xlsx_has_three_delivery_sheets_with_scoring_timing_location_and_history() -> None:
    full_item = make_item("CASE-FULL", DeliveryCategory.FULL)
    full_item = ClassifiedDeliveryItem(
        full_item.investment,
        full_item.revision,
        full_item.category,
        (
            StatusChange("CASE-FULL", None, "wniosek", "2026-08-01T10:00:00+00:00"),
            StatusChange("CASE-FULL", "wniosek", "decyzja", "2026-09-01T10:00:00+00:00"),
        ),
    )
    items = (
        make_item(
            "CASE-NEW",
            DeliveryCategory.NEW,
            organ="Starostwo Powiatowe",
            data_decyzji="2025-09-01",
            kategoria="mieszkaniowa-jednorodzinna",
            nazwa_zamierzenia="Budowa zespołu dwóch budynków mieszkalnych",
            miejscowosc="Łódź",
            powiat="łódzki",
            gmina="Łódź",
            dzialki=["123/4"],
            geoportal_url="https://example.test/geoportal",
            google_maps_url="https://example.test/maps",
        ),
        make_item("CASE-CHANGE", DeliveryCategory.CHANGE),
        full_item,
    )
    output = BytesIO()

    write_xlsx(items, output, trade_key="dach", as_of=date(2026, 1, 1))

    payload = output.getvalue()
    assert is_zipfile(BytesIO(payload))
    workbook = load_workbook(BytesIO(payload), data_only=False)
    assert workbook.sheetnames == ["1. Nowe", "2. Zmiany", "3. Pełna baza"]

    new_sheet = workbook["1. Nowe"]
    header_columns = {cell.value: cell.column for cell in new_sheet[1]}
    assert new_sheet.max_row == 2
    assert new_sheet.cell(2, header_columns["ID sprawy"]).value == "CASE-NEW"
    assert new_sheet.cell(2, header_columns["Organ"]).value == "Starostwo Powiatowe"
    assert new_sheet.cell(2, header_columns["Miejscowość"]).value == "Łódź"
    assert new_sheet.cell(2, header_columns["Działki"]).value == "123/4"
    assert new_sheet.cell(2, header_columns["Uzasadnienie"]).value
    assert "Etap dachu" in new_sheet.cell(2, header_columns["Kiedy dzwonić"]).value
    assert new_sheet.cell(2, header_columns["Geoportal"]).value == "https://example.test/geoportal"
    assert new_sheet.cell(2, header_columns["Google Maps"]).value == "https://example.test/maps"
    assert new_sheet.cell(2, header_columns["Historia statusów"]).value is None
    assert workbook["2. Zmiany"].max_row == 2
    full_sheet = workbook["3. Pełna baza"]
    assert full_sheet.max_row == 2
    history_column = header_columns.get("Historia statusów")
    assert history_column is not None
    assert json.loads(full_sheet.cell(2, history_column).value) == [
        {"stary_status": None, "nowy_status": "wniosek", "zmieniono": "2026-08-01T10:00:00+00:00"},
        {"stary_status": "wniosek", "nowy_status": "decyzja", "zmieniono": "2026-09-01T10:00:00+00:00"},
    ]
    workbook.close()


def test_csv_is_flat_utf8_bom_export_with_polish_text_and_csv_quoting() -> None:
    item = make_item(
        "CASE-ŁÓDŹ",
        DeliveryCategory.NEW,
        nazwa_zamierzenia='Budowa "Domu", w Łodzi',
        miejscowosc="Łódź",
    )
    output = BytesIO()

    write_csv((item,), output)

    payload = output.getvalue()
    assert payload.startswith(b"\xef\xbb\xbf")
    assert payload.count(b"\xef\xbb\xbf") == 1
    assert b"\r\r\n" not in payload
    rows = list(csv.reader(StringIO(payload.decode("utf-8-sig"), newline="")))
    header_columns = {name: index for index, name in enumerate(rows[0])}
    assert len(rows) == 2
    assert rows[1][header_columns["ID sprawy"]] == "CASE-ŁÓDŹ"
    assert rows[1][header_columns["Nazwa zamierzenia"]] == 'Budowa "Domu", w Łodzi'
    assert rows[1][header_columns["Miejscowość"]] == "Łódź"


def test_spreadsheet_formula_text_is_kept_as_literal_in_xlsx_and_escaped_in_csv() -> None:
    formula_text = "=HYPERLINK(\"https://example.test\",\"lead\")"
    item = make_item("CASE-FORMULA", DeliveryCategory.NEW, inwestor=formula_text)
    xlsx_output = BytesIO()

    write_xlsx((item,), xlsx_output)

    workbook = load_workbook(BytesIO(xlsx_output.getvalue()), data_only=False)
    sheet = workbook["1. Nowe"]
    header_columns = {cell.value: cell.column for cell in sheet[1]}
    investor_cell = sheet.cell(2, header_columns["Inwestor"])
    assert investor_cell.value == formula_text
    assert investor_cell.data_type != "f"
    workbook.close()

    csv_output = BytesIO()
    write_csv((item,), csv_output)
    rows = list(csv.reader(StringIO(csv_output.getvalue().decode("utf-8-sig"), newline="")))
    csv_headers = {name: index for index, name in enumerate(rows[0])}
    assert rows[1][csv_headers["Inwestor"]] == "'" + formula_text


@pytest.mark.parametrize("formula_text", ("=1+1", "+1+1", "-1+1", "@SUM(1,1)", " \t=1+1"))
def test_csv_escapes_each_supported_formula_prefix(formula_text: str) -> None:
    item = make_item("CASE-FORMULA", DeliveryCategory.NEW, inwestor=formula_text)
    output = BytesIO()

    write_csv((item,), output)

    rows = list(csv.reader(StringIO(output.getvalue().decode("utf-8-sig"), newline="")))
    investor_column = rows[0].index("Inwestor")
    assert rows[1][investor_column] == "'" + formula_text


def test_csv_binary_stream_has_deterministic_utf8_bom_and_crlf(tmp_path) -> None:
    destination = tmp_path / "delivery.csv"

    with destination.open("wb") as output:
        write_csv((), output)

    payload = destination.read_bytes()
    assert payload.count(b"\xef\xbb\xbf") == 1
    assert payload.endswith(b"\r\n")
    assert payload.count(b"\r\n") == 1
    assert b"\r\r\n" not in payload


def test_xlsx_requires_reference_date_when_a_trade_window_is_requested() -> None:
    with pytest.raises(ValueError, match="as_of"):
        write_xlsx((make_item("CASE-1", DeliveryCategory.NEW),), BytesIO(), trade_key="dach")


def test_xlsx_requires_status_history_for_full_snapshot() -> None:
    with pytest.raises(ValueError, match="historii statusów"):
        write_xlsx((make_item("CASE-FULL", DeliveryCategory.FULL),), BytesIO())


def test_history_column_distinguishes_unloaded_delta_from_empty_snapshot_history() -> None:
    delta_item = make_item("CASE-DELTA", DeliveryCategory.NEW)
    change_item = make_item("CASE-CHANGE", DeliveryCategory.CHANGE)
    full_item = make_item("CASE-FULL-EMPTY", DeliveryCategory.FULL)
    full_item = ClassifiedDeliveryItem(
        full_item.investment, full_item.revision, full_item.category, ()
    )
    items = (delta_item, change_item, full_item)
    xlsx_output = BytesIO()

    write_xlsx(items, xlsx_output)

    workbook = load_workbook(BytesIO(xlsx_output.getvalue()), data_only=False)
    new_sheet = workbook["1. Nowe"]
    new_columns = {cell.value: cell.column for cell in new_sheet[1]}
    assert new_sheet.cell(2, new_columns["Historia statusów"]).value is None
    change_sheet = workbook["2. Zmiany"]
    change_columns = {cell.value: cell.column for cell in change_sheet[1]}
    assert change_sheet.cell(2, change_columns["Historia statusów"]).value is None
    full_sheet = workbook["3. Pełna baza"]
    full_columns = {cell.value: cell.column for cell in full_sheet[1]}
    assert json.loads(full_sheet.cell(2, full_columns["Historia statusów"]).value) == []
    workbook.close()

    csv_output = BytesIO()
    write_csv(items, csv_output)
    rows = list(csv.reader(StringIO(csv_output.getvalue().decode("utf-8-sig"), newline="")))
    columns = {name: index for index, name in enumerate(rows[0])}
    history_column = columns["Historia statusów"]
    assert rows[1][history_column] == ""
    assert rows[2][history_column] == ""
    assert rows[3][history_column] == "[]"
