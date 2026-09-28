"""Kategoryzacja inwestycji, odrzucanie szumu i ekstrakcja podmiotów (inwestor, projektant).

Reguła szumu opiera się na pozycji słów w opisie: przedmiotem inwestycji jest to, co opis
wymienia najpierw. Dzięki temu „Przyłącze gazowe do budynku mieszkalnego” jest szumem,
a „Budowa budynku mieszkalnego wraz z przyłączami i ogrodzeniem” – wartościowym leadem.
Wszystkie dopasowania wykonywane są na tekście znormalizowanym (małe litery, bez diakrytyków).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .config import FilterConfig
from .models import BUILDING_CATEGORIES, NOISE_CATEGORY, GunbCase
from .text import clean, fold_polish, normalize_text

RESIDENTIAL_CATEGORIES = frozenset({"I", "XIII"})
COMMERCIAL_CATEGORIES = frozenset({"XIV", "XVI", "XVII", "XVIII", "XX"})
PUBLIC_CATEGORIES = frozenset({"V", "IX", "X", "XI", "XII", "XV"})
AGRICULTURAL_CATEGORIES = frozenset({"II"})

_BUILDING_PATTERN = r"\bbudyn"
_DEMOLITION_RE = re.compile(r"rozbiork")
_CONSTRUCTION_RE = re.compile(r"\b(przebudow|rozbudow|nadbudow|odbudow|wybudow|budow(a|e|y|ie)\b)")
_MULTI_FAMILY_RE = re.compile(r"wielorodzin|wielolokal")
_AGRICULTURAL_RE = re.compile(r"inwentarsk|\bobor|chlewni|kurnik|stodol|rolnicz|gospodarstw")
_SINGLE_FAMILY_TYPE = "budynek mieszkalny jednorodzinny"

# Zwroty „brak…”/„bez…” rozpoczynające tekst albo same symbole zastępcze (cały tekst: „-”, „x”, „n/d”).
_PLACEHOLDER_RE = re.compile(
    r"^(?:(?:brak|bez|nie dotyczy|nie wymaga|nie podano|nieznany|osoba fizyczna)\b"
    r"|(?:n ?/ ?d|b ?/ ?d|nd|bd|x+|-+|\.+|0+|\?+)$)"
)
_FIRM_RE = re.compile(
    r"pracowni|biuro|studio|architekci|atelier|sp\.? ?z ?o\.? ?o|\bs\.? ?c\.?(\s|$)|spolk|\bgroup\b"
    r"|\bdesign\b|consult|projektow[aey]\b|\bprojekt\b|engineering|inzynieri"
)
_PREFIX_RE = re.compile(r"^\s*(projektant(ka)?|projektanci|architekt)\s*:?\s*", re.IGNORECASE)
_TITLE_RE = re.compile(r"(?<!\w)(mgr|inż|inz|arch|dr|hab|prof|techn|lic)\.?(?!\w)", re.IGNORECASE)
_SPACES_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Classification:
    """Wynik kategoryzacji sprawy."""

    is_residential: bool
    is_commercial: bool
    is_noise: bool
    kategoria: str
    noise_reason: str | None = None


@dataclass(frozen=True)
class Designer:
    """Projektant: osoba (imię i nazwisko) lub pracownia, z numerem uprawnień."""

    name: str | None
    license_no: str | None
    firm: str | None = None

    @property
    def display(self) -> str | None:
        """Nazwa do wyświetlenia: osoba, a gdy jej brak – pracownia."""
        return self.name or self.firm


@dataclass(frozen=True)
class FilterDecision:
    """Czy zachować sprawę jako lead – wraz z kategoryzacją i wyekstrahowanymi podmiotami."""

    keep: bool
    reason: str | None
    classification: Classification
    investor: str | None
    designer: Designer | None


class LeadFilter:
    """Kategoryzuje sprawy GUNB, wykrywa szum i wydobywa inwestora oraz projektanta.

    Args:
        config: słowa wykluczające, kategorie szumu, wzorce budynków mieszkalnych/komercyjnych.
    """

    def __init__(self, config: FilterConfig) -> None:
        self.config = config
        self._exclude = _compile(config.exclude_keywords)
        self._residential = _compile(config.residential_patterns)
        self._commercial = _compile(config.commercial_patterns)
        self._subject = _compile((_BUILDING_PATTERN, *config.residential_patterns, *config.commercial_patterns))
        self._noise_categories = frozenset(c.upper() for c in config.noise_categories)
        self._include = frozenset(config.include_categories)

    def classify(self, case: GunbCase) -> Classification:
        """Nadaje flagi ``is_residential``/``is_commercial``/``is_noise`` i kategorię biznesową."""
        text = normalize_text(case.nazwa_zamierzenia)
        category = (case.kategoria_obiektu or "").upper()

        residential_words = _first_match(self._residential, text) is not None
        commercial_words = _first_match(self._commercial, text) is not None
        # Pole „rodzaj inwestycji” bywa wypełniane niedokładnie – opis ma pierwszeństwo.
        single_family_type = normalize_text(case.rodzaj_inwestycji) == _SINGLE_FAMILY_TYPE
        is_residential = (
            category in RESIDENTIAL_CATEGORIES or residential_words or (single_family_type and not commercial_words)
        )
        is_commercial = category in COMMERCIAL_CATEGORIES or commercial_words
        noise_reason = self._noise_reason(case, text, category)

        if noise_reason:
            kategoria = NOISE_CATEGORY
        elif is_residential and is_commercial:
            kategoria = "mieszana"
        elif is_residential:
            multi = category == "XIII" or _MULTI_FAMILY_RE.search(text)
            kategoria = "mieszkaniowa-wielorodzinna" if multi else "mieszkaniowa-jednorodzinna"
        elif is_commercial:
            kategoria = "komercyjna"
        elif category in PUBLIC_CATEGORIES:
            kategoria = "publiczna"
        elif category in AGRICULTURAL_CATEGORIES or _AGRICULTURAL_RE.search(text):
            kategoria = "rolnicza"
        else:
            kategoria = "inna"

        return Classification(is_residential, is_commercial, noise_reason is not None, kategoria, noise_reason)

    def extract_investor(self, case: GunbCase) -> str | None:
        """Nazwa inwestora, jeśli jawna (GUNB ukrywa dane osób fizycznych)."""
        name = clean(case.inwestor_raw)
        if not name:
            return None
        name = name.strip('"').strip()
        if not name or _PLACEHOLDER_RE.match(normalize_text(name)):
            return None
        return name

    def extract_designer(self, case: GunbCase) -> Designer | None:
        """Projektant: imię i nazwisko (porządkowane) albo nazwa pracowni, plus nr uprawnień."""
        parts = [_PREFIX_RE.sub("", p).strip() for p in (clean(case.projektant_imie), clean(case.projektant_nazwisko)) if p]
        parts = [p for p in parts if p]
        if not parts or any(_PLACEHOLDER_RE.match(normalize_text(p)) for p in parts):
            return None

        license_no = clean(case.projektant_uprawnienia)
        if license_no and _PLACEHOLDER_RE.match(normalize_text(license_no)):
            license_no = None

        combined = " ".join(parts)
        if _FIRM_RE.search(normalize_text(combined)):
            return Designer(name=None, license_no=license_no, firm=combined)

        name = _SPACES_RE.sub(" ", _TITLE_RE.sub(" ", combined)).strip(" ,.")
        if not name:
            return None
        return Designer(name=_smart_title(name), license_no=license_no)

    def evaluate(self, case: GunbCase) -> FilterDecision:
        """Łączy kategoryzację, ekstrakcję podmiotów i reguły ``drop_noise``/``include_categories``."""
        classification = self.classify(case)
        keep, reason = True, None
        if classification.is_noise and self.config.drop_noise:
            keep, reason = False, classification.noise_reason
        elif self._include and classification.kategoria not in self._include:
            keep, reason = False, f"kategoria leada {classification.kategoria} spoza filter.include_categories"
        return FilterDecision(
            keep=keep,
            reason=reason,
            classification=classification,
            investor=self.extract_investor(case),
            designer=self.extract_designer(case),
        )

    def _noise_reason(self, case: GunbCase, text: str, category: str) -> str | None:
        subject = _first_match(self._subject, text)
        excluded = _first_match(self._exclude, text)
        if excluded and (subject is None or excluded[0] < subject[0]):
            return f"słowo wykluczające „{excluded[1]}” w opisie"
        work_text = normalize_text(case.rodzaj_robot)
        if self.config.drop_demolitions and (
            _DEMOLITION_RE.search(work_text) or (_DEMOLITION_RE.search(text) and not _CONSTRUCTION_RE.search(text))
        ):
            return "rozbiórka (bez budowy, przebudowy ani rozbudowy)"
        work = _first_match(self._exclude, work_text)
        if work:
            return f"rodzaj robót: {case.rodzaj_robot}"
        if category in self._noise_categories and subject is None:
            return f"kategoria {category} ({BUILDING_CATEGORIES.get(category, '?')})"
        return None


def _compile(patterns: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    compiled = []
    for pattern in patterns:
        normalized = fold_polish(pattern).lower().strip()
        if normalized:
            compiled.append((normalized, re.compile(normalized)))
    return compiled


def _first_match(patterns: list[tuple[str, re.Pattern[str]]], text: str) -> tuple[int, str] | None:
    """Najwcześniejsze dopasowanie w tekście: ``(pozycja, wzorzec)`` lub ``None``."""
    best: tuple[int, str] | None = None
    if not text:
        return None
    for raw, regex in patterns:
        match = regex.search(text)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), raw)
    return best


def _smart_title(text: str) -> str:
    """Zamienia wyrazy pisane WIELKIMI LITERAMI na „Tytułowe” (z obsługą nazwisk dwuczłonowych)."""

    def fix(token: str) -> str:
        letters = [c for c in token if c.isalpha()]
        if len(letters) > 1 and all(c.isupper() for c in letters):
            return "-".join(part.capitalize() for part in token.split("-"))
        return token

    return " ".join(fix(token) for token in text.split())
