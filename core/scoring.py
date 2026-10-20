"""Prosty scoring leadów: 🔥 HOT / 🟡 NORMAL / ⚪ LOW – punkty z czytelnym uzasadnieniem, bez AI.

Punkty (suma):

* kubatura: ≥ 20 000 m³ +4, ≥ 5 000 m³ +3, ≥ 2 000 m³ +2, ≥ 800 m³ +1, < 150 m³ −1,
* rodzaj: wielorodzinny / mieszany / komercyjny +2, publiczny i dom jednorodzinny +1, inny −1,
* kilka budynków (zespół, osiedle, „dwóch/9 budynków”, liczba mnoga) +2,
* budowa nowego obiektu +1, drobne roboty („wykonanie robót budowlanych innych…”) −1,
* mały obiekt (garaż, wiata, budynek gospodarczy, kat. III…) −2,
* zmiana wcześniejszego pozwolenia (to nie nowa inwestycja) −2,
* inwestor firmowy (jawny w rejestrze – łatwiej dotrzeć) +1.

Próg: ≥ 6 pkt → HOT, ≥ 2 pkt → NORMAL, poniżej → LOW.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import Investment
from .text import normalize_text

HOT, NORMAL, LOW = "hot", "normal", "low"
HOT_MIN_POINTS = 6
NORMAL_MIN_POINTS = 2

PRIORITY_BADGES: dict[str, str] = {HOT: "🔥 HOT", NORMAL: "🟡 NORMAL", LOW: "⚪ LOW"}

_VOLUME_BANDS: tuple[tuple[float, int, str], ...] = (
    (20000, 4, "kubatura ≥ 20 000 m³"),
    (5000, 3, "kubatura ≥ 5 000 m³"),
    (2000, 2, "kubatura ≥ 2 000 m³"),
    (800, 1, "kubatura ≥ 800 m³"),
)
_CATEGORY_POINTS: dict[str, tuple[int, str]] = {
    "mieszkaniowa-wielorodzinna": (2, "wielorodzinny"),
    "mieszana": (2, "mieszkalno-usługowy"),
    "komercyjna": (2, "komercyjny"),
    "publiczna": (1, "publiczny"),
    "mieszkaniowa-jednorodzinna": (1, "dom jednorodzinny"),
    "inna": (-1, "inny obiekt"),
    "szum": (-3, "szum"),
}
_SEVERAL_BUILDINGS_RE = re.compile(
    r"\b(zespol\w*|osiedl\w*|kompleks\w*)\b"
    r"|\b(dw(och|u|a|ie)|trzech|trzy|czterech|cztery|pieciu|piec|szesciu|siedmiu|osmiu|dziewieciu"
    r"|dziesieciu|\d{1,3})\s+(budynk|dom)"
    r"|\bbudynk(ow|i|ami)\b|\bdomow\b"
)
_AMENDMENT_RE = re.compile(r"\bzmian\w* (pozwolen|decyzj)")
_SMALL_OBJECT_RE = re.compile(
    r"\b(garaz|wiat|altan|budyn\w* gospodarcz|gospodarcz\w* budyn|komin|kotlown|taras|balkon|schod|zadaszen"
    r"|carport|pomieszczen|lokal\w* mieszkal)"
)


@dataclass(frozen=True)
class Score:
    """Wynik scoringu: punkty, priorytet (``hot``/``normal``/``low``) i powody (od najważniejszych)."""

    points: int
    priority: str
    reasons: tuple[str, ...]

    @property
    def badge(self) -> str:
        """Etykieta z ikoną, np. „🔥 HOT”."""
        return PRIORITY_BADGES[self.priority]


def score_investment(inv: Investment) -> Score:
    """Liczy punkty leada i przypisuje priorytet HOT / NORMAL / LOW."""
    points = 0
    reasons: list[str] = []

    def add(value: int, reason: str) -> None:
        nonlocal points
        points += value
        reasons.append(reason)

    if inv.kubatura:
        for threshold, value, reason in _VOLUME_BANDS:
            if inv.kubatura >= threshold:
                add(value, reason)
                break
        else:
            if inv.kubatura < 150:
                add(-1, "mała kubatura")

    category = _CATEGORY_POINTS.get(inv.kategoria or "")
    if category:
        add(*category)

    text = normalize_text(inv.nazwa_zamierzenia)
    if _SEVERAL_BUILDINGS_RE.search(text):
        add(2, "kilka budynków")

    work = normalize_text(inv.rodzaj_robot)
    if work.startswith("budowa nowego"):
        add(1, "nowa budowa")
    elif work.startswith("wykonanie robot"):
        add(-1, "drobne roboty")

    if inv.kategoria_obiektu == "III" or _SMALL_OBJECT_RE.search(text):
        add(-2, "mały obiekt")
    if _AMENDMENT_RE.search(text):
        add(-2, "zmiana wcześniejszego pozwolenia")

    if inv.inwestor:
        add(1, "inwestor firmowy")

    if points >= HOT_MIN_POINTS:
        priority = HOT
    elif points >= NORMAL_MIN_POINTS:
        priority = NORMAL
    else:
        priority = LOW
    return Score(points, priority, tuple(reasons))
