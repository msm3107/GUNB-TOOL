"""„⏰ Kiedy dzwonić”: w którym momencie budowy potrzebna jest dana branża.

Dekarz czy monter okien nie potrzebuje leada w dniu pozwolenia – wtedy jest za wcześnie o kilka
miesięcy. Każda branża ma okno czasowe liczone od daty decyzji (dla zgłoszeń – od daty wpływu);
bot przypomina o budowie, gdy ta wchodzi w okno branży klienta.

Okna dotyczą domów jednorodzinnych; większe budynki (bloki, hale, obiekty usługowe) budują się
dłużej, więc ich okna są mnożone przez :data:`BIG_BUILDING_FACTOR`. To szacunki do kalibracji
z klientami – rejestr nie mówi, kiedy budowa faktycznie ruszyła.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .models import Investment

DAYS_PER_MONTH = 30.44
BIG_BUILDING_FACTOR = 1.5


@dataclass(frozen=True)
class Trade:
    """Branża klienta i jej okno czasowe.

    Attributes:
        key: identyfikator zapisywany w bazie.
        label: nazwa na przycisku.
        stage: etap w dopełniaczu („na etapie dachu”).
        months: okno od–do w miesiącach po decyzji (dom jednorodzinny); ``None`` – branża potrzebna
            od razu, więc wystarczają zwykłe powiadomienia o nowych leadach.
    """

    key: str
    label: str
    stage: str
    months: tuple[float, float] | None


TRADES: tuple[Trade, ...] = (
    Trade("stan_surowy", "🧱 Fundamenty / stan surowy", "fundamentów i murów", None),
    Trade("dach", "🏠 Dach", "dachu", (4, 6)),
    Trade("okna", "🪟 Okna i drzwi", "okien i drzwi", (5, 7)),
    Trade("instalacje", "🔥 Instalacje", "instalacji", (6, 9)),
    Trade("elewacja", "🎨 Elewacja / ocieplenie", "elewacji i ocieplenia", (8, 12)),
    Trade("wykonczenia", "🛋️ Wykończenia", "wykończeń", (9, 14)),
    Trade("ogrodzenie", "🚧 Ogrodzenie, brama, kostka", "ogrodzenia i podjazdu", (10, 18)),
)
_BY_KEY = {trade.key: trade for trade in TRADES}
LONGEST_WINDOW_DAYS = round(max(t.months[1] for t in TRADES if t.months) * BIG_BUILDING_FACTOR * DAYS_PER_MONTH)
"""Najstarsza decyzja, która może jeszcze wchodzić w czyjeś okno – do zawężenia zapytań."""


def get_trade(key: str | None) -> Trade | None:
    """Branża o danym kluczu (``None`` dla nieznanego)."""
    return _BY_KEY.get(key or "")


def stage_window(trade: Trade, inv: Investment) -> tuple[date, date] | None:
    """Pierwszy i ostatni dzień, w którym budowa jest na etapie branży (``None`` – brak okna lub daty)."""
    base = inv.data_decyzji or inv.data_wplywu
    if trade.months is None or not base:
        return None
    try:
        decided = date.fromisoformat(base[:10])
    except ValueError:
        return None
    factor = 1.0 if inv.kategoria == "mieszkaniowa-jednorodzinna" else BIG_BUILDING_FACTOR
    start, end = (decided + timedelta(days=round(months * factor * DAYS_PER_MONTH)) for months in trade.months)
    return start, end


def is_due(trade: Trade, inv: Investment, today: date) -> bool:
    """Czy budowa jest dziś na etapie danej branży."""
    window = stage_window(trade, inv)
    return window is not None and window[0] <= today <= window[1]
