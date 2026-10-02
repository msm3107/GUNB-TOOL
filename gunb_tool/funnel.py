"""Lejek pilotażu: aktywacja, kohorty testu i mianowniki konwersji – liczone z surowych zdarzeń.

Zdarzenia (``zdarzenia``) są zapisywane bez interpretacji, a definicje z tego modułu liczą wynik na bieżąco.
Zmiana definicji aktywacji (nowa :class:`ActivationRule` z nową wersją) nie niszczy historii – wystarczy
policzyć ją od nowa na tych samych zdarzeniach.

Robocza definicja aktywacji (``v1``): w ciągu 48 godzin od startu testu osoba otworzyła szczegóły co najmniej
trzech różnych inwestycji i zapisała albo pozytywnie oceniła (👍, wynik „rozmowa” lub „złożona oferta”) co
najmniej jedną.

Kohorta testu = osoby, które zaczęły test w raportowanym okresie. Do konwersji (aktywacja, zamówienie,
płatność) liczymy tylko te, których okres obserwacji już minął (:data:`OBSERVATION`: 7 dni testu + 7 dni na
decyzję); trwające pokazujemy osobno i nie wliczamy do mianownika.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from .bot_store import POSITIVE_OUTCOMES, BotUser, Event

OBSERVATION = timedelta(days=14)
"""Ile od startu testu obserwujemy kohortę (test 7 dni + 7 dni na zamówienie i płatność)."""


@dataclass(frozen=True)
class ActivationRule:
    """Definicja aktywacji – wersjonowana, żeby jej zmiana nie mieszała się z wcześniejszymi raportami."""

    version: str = "v1"
    opened: int = 3
    within: timedelta = timedelta(hours=48)

    def describe(self) -> str:
        hours = int(self.within.total_seconds() // 3600)
        return (f"aktywacja ({self.version}): ≥ {self.opened} różne otwarte inwestycje i zapis lub 👍/rozmowa/"
                f"oferta w {hours} h od startu testu")


ACTIVATION = ActivationRule()


def _when(event: Event) -> datetime:
    return datetime.fromisoformat(event.kiedy)


def is_positive(event: Event) -> bool:
    """Zapis, 👍 albo wynik „rozmowa” / „złożona oferta”."""
    return event.rodzaj in ("zapis", "przydatne") or (event.rodzaj == "wynik" and event.szczegoly in POSITIVE_OUTCOMES)


def activation_time(events: Iterable[Event], start: datetime, rule: ActivationRule = ACTIVATION) -> datetime | None:
    """Kiedy osoba spełniła definicję aktywacji (``None`` – nie w oknie po starcie testu)."""
    end = start + rule.within
    opened: set[str] = set()
    positive = False
    for event in sorted(events, key=_when):
        moment = _when(event)
        if not start <= moment <= end:
            continue
        if event.rodzaj == "szczegoly" and event.id_sprawy:
            opened.add(event.id_sprawy)
        elif is_positive(event):
            positive = True
        if len(opened) >= rule.opened and positive:
            return moment
    return None


@dataclass
class Cohort:
    """Kohorta testu z okresu raportu: kto zaczął, kto ma zakończoną obserwację i co z tego wyszło."""

    started: list[int] = field(default_factory=list)
    observed: list[int] = field(default_factory=list)
    ongoing: list[int] = field(default_factory=list)
    activated: list[int] = field(default_factory=list)
    ordered: list[int] = field(default_factory=list)
    paid: list[int] = field(default_factory=list)


def trial_cohort(users: Sequence[BotUser], events: Sequence[Event], *, since: datetime, now: datetime,
                 rule: ActivationRule = ACTIVATION) -> Cohort:
    """Kohorta osób, które zaczęły test od ``since``; konwersje tylko dla zakończonej obserwacji."""
    by_person: dict[int, list[Event]] = {}
    for event in events:
        by_person.setdefault(event.chat_id, []).append(event)
    cohort = Cohort()
    for user in users:
        if not user.test_start:
            continue
        start = datetime.fromisoformat(user.test_start)
        if start < since or start > now:
            continue
        cohort.started.append(user.chat_id)
        if now - start < OBSERVATION:
            cohort.ongoing.append(user.chat_id)
            continue
        cohort.observed.append(user.chat_id)
        own = by_person.get(user.chat_id, [])
        window = [e for e in own if start <= _when(e) <= start + OBSERVATION]
        if activation_time(own, start, rule) is not None:
            cohort.activated.append(user.chat_id)
        if any(e.rodzaj == "zamowienie" for e in window):
            cohort.ordered.append(user.chat_id)
        if any(e.rodzaj == "platnosc" for e in window):
            cohort.paid.append(user.chat_id)
    return cohort


def people(events: Iterable[Event], kind: str) -> set[int]:
    """Unikalne osoby ze zdarzeniem ``kind``."""
    return {e.chat_id for e in events if e.rodzaj == kind}


def count(events: Iterable[Event], kind: str) -> int:
    """Liczba zdarzeń ``kind`` (np. otwarć) – obok unikalnych osób, żeby jedna aktywna osoba nie udawała wielu."""
    return sum(1 for e in events if e.rodzaj == kind)
