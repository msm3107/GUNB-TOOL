"""Przykład dla osób bez dostępu – wyłącznie dane fikcyjne, wyraźnie oznaczone.

Przykład nigdy nie sięga do bazy: miejscowości, inwestorzy i numery spraw są wymyślone („Przykładowo”,
„Wzorcowo”), więc pokazanie go komuś bez dostępu nie ujawnia żadnego prawdziwego wpisu z rejestru.
Daty demo_leads zależą od przekazanego dnia; próbka paczki ma stały, jawny scenariusz.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from .delivery_memory import ClassifiedDeliveryItem
from .models import DeliveryCategory, Investment
from .storage import StatusChange

DEMO_LABEL = "🧪 <b>PRZYKŁAD – dane fikcyjne</b> (tak wygląda raport; to nie są prawdziwe inwestycje)"
DEMO_PACKAGE_DATE = date(2026, 11, 1)
DEMO_PACKAGE_TRADE = "dach"


def demo_leads(today: date) -> list[Investment]:
    """Trzy fikcyjne inwestycje w formacie z rejestru – do pokazania przed testem."""

    def day(days_ago: int) -> str:
        return (today - timedelta(days=days_ago)).isoformat()

    common = {
        "zrodlo": "pozwolenia",
        "status": "decyzja",
        "powiat": "powiat przykładowy",
        "precyzja_geo": "dzialka",
    }
    return [
        Investment(
            id_sprawy="PRZYKŁAD/1",
            kategoria="mieszkaniowa-jednorodzinna",
            priorytet="normal",
            nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego z garażem",
            adres_opisowy="Przykładowo, ul. Lipowa",
            miejscowosc="Przykładowo",
            gmina="Przykładowo",
            rodzaj_robot="budowa nowego obiektu",
            kubatura=820.0,
            data_decyzji=day(3),
            data_aktualizacji=day(3),
            lat=53.80,
            lon=20.45,
            **common,
        ),
        Investment(
            id_sprawy="PRZYKŁAD/2",
            kategoria="komercyjna",
            priorytet="hot",
            nazwa_zamierzenia="Budowa hali magazynowej z częścią biurową",
            adres_opisowy="Wzorcowo, ul. Przemysłowa",
            miejscowosc="Wzorcowo",
            gmina="Wzorcowo",
            inwestor="Fikcyjna Spółka Przykładowa",
            kubatura=9400.0,
            data_decyzji=day(6),
            data_aktualizacji=day(6),
            lat=53.82,
            lon=20.52,
            **common,
        ),
        Investment(
            id_sprawy="PRZYKŁAD/3",
            kategoria="mieszkaniowa-wielorodzinna",
            priorytet="hot",
            nazwa_zamierzenia="Budowa budynku wielorodzinnego z 12 lokalami",
            adres_opisowy="Testowice",
            miejscowosc="Testowice",
            gmina="Wzorcowo",
            kubatura=5600.0,
            data_decyzji=day(9),
            data_aktualizacji=day(9),
            lat=53.75,
            lon=20.40,
            **common,
        ),
    ]


def demo_package_items() -> tuple[ClassifiedDeliveryItem, ...]:
    """One fictional row per delivery variant, without a customer or DB history.

    Fixed dates describe a demonstration, never current coverage. Map pointers
    are deliberately omitted so synthetic entries cannot identify real property.
    """
    leads = [
        replace(
            lead,
            nazwa_zamierzenia="DANE FIKCYJNE — " + lead.nazwa_zamierzenia,
            organ="Fikcyjny organ RWDZ — demonstracja",
            lat=None,
            lon=None,
            precyzja_geo="brak",
            dzialki=[],
            google_maps_url=None,
            geoportal_url=None,
            telefon=None,
            email=None,
            projektant=None,
            projektant_uprawnienia=None,
        )
        for lead in demo_leads(DEMO_PACKAGE_DATE)
    ]
    leads[0] = replace(
        leads[0],
        status_opis="Fikcyjna decyzja o pozwoleniu — sprawa dotąd nieznana klientowi",
        data_wplywu="2026-05-01",
        data_decyzji="2026-06-01",
        data_aktualizacji="2026-06-01",
    )
    leads[1] = replace(
        leads[1],
        status_opis="Fikcyjna decyzja o pozwoleniu po wcześniejszym wniosku",
        data_wplywu="2026-02-01",
        data_decyzji="2026-03-01",
        data_aktualizacji="2026-03-01",
    )
    leads[2] = replace(
        leads[2],
        nazwa_zamierzenia="DANE FIKCYJNE — Rozbudowa szkoły w Testowicach",
        status_opis="Fikcyjna decyzja o pozwoleniu na rozbudowę",
        kategoria="publiczna",
        rodzaj_robot="rozbudowa",
        data_wplywu="2026-02-10",
        data_decyzji="2026-03-10",
        data_aktualizacji="2026-03-10",
        wymaga_weryfikacji=True,
    )
    items = []
    for lead, category in zip(leads, DeliveryCategory):
        history = (
            None
            if category == DeliveryCategory.NEW
            else (
                StatusChange(
                    lead.id_sprawy,
                    None,
                    "wniosek",
                    lead.data_wplywu + "T12:00:00+00:00",
                ),
                StatusChange(
                    lead.id_sprawy,
                    "wniosek",
                    "decyzja",
                    lead.data_decyzji + "T12:00:00+00:00",
                ),
            )
        )
        items.append(
            ClassifiedDeliveryItem(
                lead, "demo-" + category.value + "-v1", category, history
            )
        )
    return tuple(items)
