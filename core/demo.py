"""Przykład dla osób bez dostępu – wyłącznie dane fikcyjne, wyraźnie oznaczone.

Przykład nigdy nie sięga do bazy: miejscowości, inwestorzy i numery spraw są wymyślone („Przykładowo”,
„Wzorcowo”), więc pokazanie go komuś bez dostępu nie ujawnia żadnego prawdziwego wpisu z rejestru.
Daty są liczone od dziś, żeby przykład wyglądał jak bieżący raport.
"""

from __future__ import annotations

from datetime import date, timedelta

from .models import Investment

DEMO_LABEL = "🧪 <b>PRZYKŁAD – dane fikcyjne</b> (tak wygląda raport; to nie są prawdziwe inwestycje)"


def demo_leads(today: date) -> list[Investment]:
    """Trzy fikcyjne inwestycje w formacie z rejestru – do pokazania przed testem."""
    def day(days_ago: int) -> str:
        return (today - timedelta(days=days_ago)).isoformat()

    common = dict(zrodlo="pozwolenia", status="decyzja", powiat="powiat przykładowy", precyzja_geo="dzialka")
    return [
        Investment(id_sprawy="PRZYKŁAD/1", kategoria="mieszkaniowa-jednorodzinna", priorytet="normal",
                   nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego z garażem",
                   adres_opisowy="Przykładowo, ul. Lipowa", miejscowosc="Przykładowo", gmina="Przykładowo",
                   rodzaj_robot="budowa nowego obiektu", kubatura=820.0, data_decyzji=day(3),
                   data_aktualizacji=day(3), lat=53.80, lon=20.45, **common),
        Investment(id_sprawy="PRZYKŁAD/2", kategoria="komercyjna", priorytet="hot",
                   nazwa_zamierzenia="Budowa hali magazynowej z częścią biurową",
                   adres_opisowy="Wzorcowo, ul. Przemysłowa", miejscowosc="Wzorcowo", gmina="Wzorcowo",
                   inwestor="Fikcyjna Spółka Przykładowa", kubatura=9400.0, data_decyzji=day(6),
                   data_aktualizacji=day(6), lat=53.82, lon=20.52, **common),
        Investment(id_sprawy="PRZYKŁAD/3", kategoria="mieszkaniowa-wielorodzinna", priorytet="hot",
                   nazwa_zamierzenia="Budowa budynku wielorodzinnego z 12 lokalami",
                   adres_opisowy="Testowice", miejscowosc="Testowice", gmina="Wzorcowo",
                   kubatura=5600.0, data_decyzji=day(9), data_aktualizacji=day(9), lat=53.75, lon=20.40, **common),
    ]
