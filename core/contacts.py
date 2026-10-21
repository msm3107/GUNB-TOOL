"""Telefony i e-maile wpisane w pola inwestora lub projektanta rejestru GUNB.

Rejestr nie ma pól kontaktowych, ale zdarza się, że ktoś wpisze telefon lub e-mail w nazwę inwestora
albo w pole numeru uprawnień projektanta. Dopasowanie jest ostrożne – lepiej pominąć numer niż podać
jako telefon REGON, NIP czy numer uprawnień:

* telefon to 9 cyfr (numeracja krajowa, pierwsza cyfra różna od 0) poprzedzonych słowem
  „tel.”/„telefon”/„kom.” lub prefiksem +48, albo zapisanych w typowych grupach telefonu
  (``600 123 456``, ``600-123-456``, ``(89) 527 00 00``),
* same 9 cyfr bez takiego kontekstu (``516042210``) są pomijane.

Telefony zapisywane są w formacie E.164 (``+48600123456``), e-maile małymi literami.

:func:`split_contact` rozdziela takie brudne pole („Kowalski tel. 600 123 456, ul. Długa 5, 10-123 Olsztyn”)
na nazwę, telefon, e-mail i adres – na karcie leada zostaje samo nazwisko.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_KEYWORD = r"(?:tel(?:efon)?|kom(?:[óo]rka)?|mobile|phone)\.?\s*(?:[:.]\s*)?"
_PREFIX = r"(?:(?:\+|00)\s?48[\s-]?)"
_GROUPED = r"\d{3}[\s-]\d{3}[\s-]\d{3}|\(?\d{2}\)?[\s-]\d{3}[\s-]\d{2}[\s-]\d{2}"
_PHONE_RE = re.compile(
    rf"(?<![\w/+])(?:{_KEYWORD}{_PREFIX}?(?P<a>{_GROUPED}|\d{{9}})"
    rf"|{_PREFIX}(?P<b>{_GROUPED}|\d{{9}})"
    rf"|(?P<c>{_GROUPED}))(?![\w/])",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?![\w-])", re.IGNORECASE)
_EMAIL_LABEL_RE = re.compile(r"(?:e-?mail|poczta)\s*:?\s*$", re.IGNORECASE)
# Początek adresu: ulica/aleja/osiedle/plac albo kod pocztowy. Szukany dopiero za nazwą (nie od 1. znaku),
# bo nazwy firm potrafią zaczynać się od „Osiedle…” czy „Aleja…”.
_ADDRESS_START_RE = re.compile(r"(?<!\w)(?:ul\.|al\.|os\.|pl\.|ulica\b|aleja\b|osiedle\b|\d{2}-\d{3}(?!\d))",
                               re.IGNORECASE)
_EDGE_PUNCTUATION = " ,;:-–/|"
_PLACEHOLDER_NUMBERS = frozenset({"123456789", "987654321"})


@dataclass(frozen=True)
class ContactParts:
    """Pole tekstowe rozdzielone na nazwę (osoby/firmy) i dane kontaktowe."""

    name: str | None
    phone: str | None
    email: str | None
    address: str | None


def find_phone(text: str | None) -> str | None:
    """Pierwszy polski numer telefonu w tekście (``+48XXXXXXXXX``) albo ``None``."""
    found = _phone_match(text or "")
    return found[1] if found else None


def find_email(text: str | None) -> str | None:
    """Pierwszy adres e-mail w tekście (małymi literami) albo ``None``."""
    match = _EMAIL_RE.search(text or "")
    return match.group(0).lower().rstrip(".") if match else None


def extract_contact(*texts: str | None) -> tuple[str | None, str | None]:
    """``(telefon, email)`` z pierwszych pól, w których występują."""
    phone = next((p for p in map(find_phone, texts) if p), None)
    email = next((e for e in map(find_email, texts) if e), None)
    return phone, email


def split_contact(text: str | None, *, address: bool = True) -> ContactParts:
    """Rozdziela brudne pole na nazwę, telefon, e-mail i (opcjonalnie) adres.

    Args:
        address: czy wydzielać adres (dla numeru uprawnień – nie, tam nie ma adresów).
    """
    raw = " ".join((text or "").split())
    if not raw:
        return ContactParts(None, None, None, None)
    cuts: list[tuple[int, int]] = []
    phone = None
    found_phone = _phone_match(raw)
    if found_phone:
        match, phone = found_phone
        cuts.append(match.span())
    email = None
    email_match = _EMAIL_RE.search(raw)
    if email_match:
        email = email_match.group(0).lower().rstrip(".")
        label = _EMAIL_LABEL_RE.search(raw[: email_match.start()])
        cuts.append((label.start() if label else email_match.start(), email_match.end()))
    remainder = raw
    for start, end in sorted(_merge(cuts), reverse=True):
        remainder = remainder[:start] + " " + remainder[end:]

    address_text = None
    if address:
        start_match = _ADDRESS_START_RE.search(remainder, 1)
        candidate = remainder[start_match.start():] if start_match else ""
        name_part = remainder[: start_match.start()] if start_match else remainder
        if any(ch.isdigit() for ch in candidate) and _tidy(name_part):
            address_text, remainder = _tidy(candidate), name_part
    return ContactParts(_tidy(remainder) or None, phone, email, address_text or None)


def _phone_match(text: str) -> tuple[re.Match[str], str] | None:
    """Pierwsze dopasowanie telefonu, które przeszło walidację, i numer w formacie E.164."""
    for match in _PHONE_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group("a") or match.group("b") or match.group("c"))
        if len(digits) == 9 and digits[0] != "0" and digits not in _PLACEHOLDER_NUMBERS and len(set(digits)) > 1:
            return match, "+48" + digits
    return None


def _merge(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _tidy(text: str) -> str:
    return " ".join(text.split()).strip(_EDGE_PUNCTUATION)
