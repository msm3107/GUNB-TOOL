"""Eksport sklasyfikowanych dostaw B2B do XLSX i CSV."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from datetime import date
from typing import BinaryIO, TextIO

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell

from .delivery_memory import ClassifiedDeliveryItem
from .models import DeliveryCategory
from .ranking import stage_note
from .scoring import score_investment
from .stages import Trade, get_trade

SHEET_NAMES: tuple[tuple[DeliveryCategory, str], ...] = (
    (DeliveryCategory.NEW, "1. Nowe"),
    (DeliveryCategory.CHANGE, "2. Zmiany"),
    (DeliveryCategory.FULL, "3. Pełna baza"),
)

EXPORT_COLUMNS: tuple[str, ...] = (
    "ID sprawy",
    "Typ dostawy",
    "Status",
    "Opis statusu",
    "Data aktualizacji",
    "Data wpływu",
    "Data decyzji",
    "Organ",
    "Kategoria obiektu",
    "Rodzaj robót",
    "Nazwa zamierzenia",
    "Punkty",
    "Priorytet",
    "Uzasadnienie",
    "Kiedy dzwonić",
    "Adres",
    "Miejscowość",
    "Gmina",
    "Powiat",
    "Działki",
    "Szerokość geogr.",
    "Długość geogr.",
    "Precyzja geokodowania",
    "Inwestor",
    "Projektant",
    "Uprawnienia projektanta",
    "Kubatura (m³)",
    "Kategoria biznesowa",
    "Segment",
    "Geoportal",
    "Google Maps",
)

_FORMULA_PREFIXES = "=+-@"


def _resolve_trade(trade_key: str | None, as_of: date | None) -> Trade | None:
    if trade_key is None:
        return None
    trade = get_trade(trade_key)
    if trade is None:
        raise ValueError(f"Nieznana branża: {trade_key}")
    if as_of is None:
        raise ValueError("as_of jest wymagane przy wyliczaniu pola Kiedy dzwonić")
    return trade


def _row_values(
    item: ClassifiedDeliveryItem,
    trade: Trade | None,
    as_of: date | None,
) -> tuple[object, ...]:
    investment = item.investment
    score = score_investment(investment)
    timing = stage_note(trade, investment, as_of) if trade is not None and as_of is not None else None

    return (
        investment.id_sprawy,
        item.category.value,
        investment.status,
        investment.status_opis,
        investment.data_aktualizacji,
        investment.data_wplywu,
        investment.data_decyzji,
        investment.organ,
        investment.kategoria_obiektu,
        investment.rodzaj_robot,
        investment.nazwa_zamierzenia,
        score.points,
        score.priority,
        "; ".join(score.reasons),
        timing,
        investment.adres_opisowy,
        investment.miejscowosc,
        investment.gmina,
        investment.powiat,
        ", ".join(investment.dzialki),
        investment.lat,
        investment.lon,
        investment.precyzja_geo,
        investment.inwestor,
        investment.projektant,
        investment.projektant_uprawnienia,
        investment.kubatura,
        investment.kategoria,
        investment.segment,
        investment.geoportal_url,
        investment.google_maps_url,
    )


def _append_literal_row(sheet, values: Iterable[object]) -> None:
    cells = []
    for value in values:
        if isinstance(value, str):
            cell = WriteOnlyCell(sheet, value=value)
            # Keep source text literal even when it starts with a formula marker.
            cell.data_type = "s"
            cells.append(cell)
        else:
            cells.append(value)
    sheet.append(cells)


def _protect_csv_value(value: object) -> object:
    if not isinstance(value, str):
        return value
    first = value.lstrip().lstrip("\ufeff").lstrip()[:1]
    return "'" + value if first in _FORMULA_PREFIXES else value


def write_xlsx(
    items: Sequence[ClassifiedDeliveryItem],
    destination: BinaryIO,
    *,
    trade_key: str | None = None,
    as_of: date | None = None,
) -> None:
    """Zapisuje trzy arkusze dostawy do strumienia binarnego w trybie write-only.

    ``trade_key`` razem z ``as_of`` włącza orientacyjne okno branżowe. Funkcja
    nie otwiera ścieżek ani nie zapisuje historii doręczeń.
    """
    trade = _resolve_trade(trade_key, as_of)
    workbook = Workbook(write_only=True)
    for category, name in SHEET_NAMES:
        sheet = workbook.create_sheet(title=name)
        sheet.freeze_panes = "A2"
        _append_literal_row(sheet, EXPORT_COLUMNS)
        for item in items:
            if item.category == category:
                _append_literal_row(sheet, _row_values(item, trade, as_of))
    workbook.save(destination)


def write_csv(
    items: Iterable[ClassifiedDeliveryItem],
    destination: TextIO,
    *,
    trade_key: str | None = None,
    as_of: date | None = None,
) -> None:
    """Zapisuje płaski CSV UTF-8 z BOM i ochroną komórek przed formułami.

    ``destination`` to otwarty strumień tekstowy w pustej pozycji. Eksport nie
    zamyka strumienia i nie wykonuje operacji na bazie ani sieci.
    """
    trade = _resolve_trade(trade_key, as_of)
    writer = csv.writer(destination, lineterminator="\r\n")
    destination.write("\ufeff")
    writer.writerow(EXPORT_COLUMNS)
    for item in items:
        writer.writerow(
            _protect_csv_value(value)
            for value in _row_values(item, trade, as_of)
        )
