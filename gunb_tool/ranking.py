"""Kolejność inwestycji i powód dopasowania – według zastosowania konkretnej osoby, nie według skali.

Większa inwestycja nie jest lepsza dla każdej firmy, więc skala (🔥 HOT w danych) nie wpływa na kolejność.
Kolejność (od najważniejszego):

1. okno etapu branży (gdy branża ma okno, np. dach 4–6 mies. po decyzji): teraz w oknie → okno zacznie się
   w ciągu 30 dni → pozostałe; branże „od razu” (materiały, fundamenty) i osoby bez branży – bez tego kroku,
2. odległość od bazy w pasach po 10 km (bez bazy – pomijana),
3. data decyzji albo wpływu – najnowsze pierwsze.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Sequence

from .bot_store import BotUser, mentions_place
from .models import Investment
from .stages import Trade, get_trade, stage_window
from .text import normalize_text

DISTANCE_BAND_KM = 10
SOON = timedelta(days=30)
"""Okno etapu zaczyna się „wkrótce”, gdy do jego początku zostało najwyżej tyle."""

CATEGORY_PLURALS: dict[str, str] = {
    "mieszkaniowa-jednorodzinna": "domy jednorodzinne",
    "mieszkaniowa-wielorodzinna": "budynki wielorodzinne",
    "mieszana": "budynki mieszkalno-usługowe",
    "komercyjna": "hale, sklepy, biura",
    "publiczna": "obiekty publiczne",
    "rolnicza": "budynki rolnicze",
    "inna": "inne obiekty",
}


def event_date(inv: Investment) -> str:
    """Data, od której liczy się wiek sprawy: decyzja, a bez niej wpływ (ISO)."""
    return inv.data_decyzji or inv.data_wplywu or inv.data_aktualizacji or ""


def stage_bucket(trade: Trade | None, inv: Investment, today: date) -> int:
    """0 – teraz w oknie etapu (albo branża bez okna), 1 – okno zacznie się w ciągu 30 dni, 2 – poza oknem."""
    if trade is None or trade.months is None:
        return 0
    window = stage_window(trade, inv)
    if window is None:
        return 2
    start, end = window
    if start <= today <= end:
        return 0
    return 1 if today < start <= today + SOON else 2


def ranked(user: BotUser, leads: Sequence[Investment], today: date) -> list[Investment]:
    """Inwestycje w kolejności dla tej osoby (patrz opis modułu); kolejność jest stała dla tych samych danych."""
    trade = get_trade(user.branza)
    filters = user.filtry

    def band(inv: Investment) -> int:
        km = filters.distance_km(inv) if filters.baza else None
        if filters.baza is None:
            return 0
        return int(km // DISTANCE_BAND_KM) if km is not None else 10**6

    newest_first = sorted(leads, key=lambda inv: (event_date(inv), inv.nr or 0), reverse=True)
    return sorted(newest_first, key=lambda inv: (stage_bucket(trade, inv, today), band(inv)))


def match_reasons(user: BotUser, inv: Investment, place_names: dict[str, str], today: date) -> list[str]:
    """Dlaczego ta inwestycja jest na liście tej osoby – jej własne ustawienia, krótko."""
    filters = user.filtry
    reasons: list[str] = []
    if filters.radius_active:
        reasons.append(f"do {filters.promien_km} km od Twojej bazy")
    else:
        place = normalize_text(" ".join(p for p in (inv.gmina, inv.miejscowosc, inv.adres_opisowy, inv.powiat) if p))
        reasons += [name for name in filters.miejsca if mentions_place(place, name)]
        if inv.powiat_teryt in filters.powiaty:
            reasons.append(place_names.get(inv.powiat_teryt, f"powiat {inv.powiat_teryt}"))
    if filters.kategorie and inv.kategoria in filters.kategorie:
        reasons.append(CATEGORY_PLURALS.get(inv.kategoria or "", inv.kategoria or ""))
    if filters.min_kubatura:
        reasons.append(f"kubatura od {format(filters.min_kubatura, ',.0f').replace(',', ' ')} m³")
    if filters.inwestor == "firma":
        reasons.append("z nazwą inwestora w rejestrze")
    elif filters.inwestor:
        reasons.append(f"inwestor: {filters.inwestor}")
    if user.tylko_hot:
        reasons.append("duża skala")
    trade = get_trade(user.branza)
    if trade is not None and trade.months is not None and stage_bucket(trade, inv, today) == 0:
        reasons.append(f"okno etapu {trade.stage} (szacunek)")
    return reasons or ["cały monitorowany obszar"]


def stage_note(trade: Trade | None, inv: Investment, today: date) -> str | None:
    """Orientacyjne okno etapu branży dla tej budowy – z zastrzeżeniem, że etap trzeba sprawdzić."""
    if trade is None or trade.months is None:
        return None
    window = stage_window(trade, inv)
    if window is None:
        return None
    start, end = window
    if start <= today <= end:
        when = "teraz"
    elif today < start:
        when = f"za ok. {max(1, round((start - today).days / 7))} tyg."
    else:
        when = "minęło"
    first = f"{start:%d.%m}" if start.year == end.year else f"{start:%d.%m.%Y}"
    return (f"⏳ Etap {trade.stage}: orientacyjnie {first}–{end:%d.%m.%Y} (od daty decyzji) – {when}; "
            "trzeba sprawdzić na miejscu")
