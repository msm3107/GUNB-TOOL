"""Słownik województw (kody TERYT) i nazw plików udostępnianych przez GUNB."""

from __future__ import annotations

from dataclasses import dataclass

from .text import normalize_text


@dataclass(frozen=True)
class Voivodeship:
    """Województwo: dwucyfrowy kod TERYT, nazwa urzędowa i slug pliku GUNB."""

    code: str
    name: str
    slug: str


VOIVODESHIPS: tuple[Voivodeship, ...] = (
    Voivodeship("02", "dolnośląskie", "dolnoslaskie"),
    Voivodeship("04", "kujawsko-pomorskie", "kujawsko-pomorskie"),
    Voivodeship("06", "lubelskie", "lubelskie"),
    Voivodeship("08", "lubuskie", "lubuskie"),
    Voivodeship("10", "łódzkie", "lodzkie"),
    Voivodeship("12", "małopolskie", "malopolskie"),
    Voivodeship("14", "mazowieckie", "mazowieckie"),
    Voivodeship("16", "opolskie", "opolskie"),
    Voivodeship("18", "podkarpackie", "podkarpackie"),
    Voivodeship("20", "podlaskie", "podlaskie"),
    Voivodeship("22", "pomorskie", "pomorskie"),
    Voivodeship("24", "śląskie", "slaskie"),
    Voivodeship("26", "świętokrzyskie", "swietokrzyskie"),
    Voivodeship("28", "warmińsko-mazurskie", "warminsko-mazurskie"),
    Voivodeship("30", "wielkopolskie", "wielkopolskie"),
    Voivodeship("32", "zachodniopomorskie", "zachodniopomorskie"),
)

_BY_CODE = {v.code: v for v in VOIVODESHIPS}
_BY_NAME = {normalize_text(v.name): v for v in VOIVODESHIPS}


def get_voivodeship(code_or_name: str) -> Voivodeship:
    """Zwraca województwo po kodzie TERYT (``"12"``, ``"2"``) lub nazwie (z/bez diakrytyków).

    Raises:
        KeyError: gdy kod/nazwa nie odpowiada żadnemu województwu.
    """
    key = code_or_name.strip()
    if key.isdigit():
        found = _BY_CODE.get(key.zfill(2))
    else:
        found = _BY_NAME.get(normalize_text(key))
    if found is None:
        raise KeyError(f"Nieznane województwo: {code_or_name!r}")
    return found
