"""Telefony i e-maile wpisane w pola inwestora lub projektanta rejestru GUNB.

Rejestr nie ma pól kontaktowych, ale zdarza się, że ktoś wpisze telefon lub e-mail w nazwę inwestora
albo w pole numeru uprawnień projektanta. Dopasowanie jest ostrożne – lepiej pominąć numer niż podać
jako telefon REGON, NIP czy numer uprawnień:

* telefon to 9 cyfr (numeracja krajowa, pierwsza cyfra różna od 0) poprzedzonych słowem
  „tel.”/„telefon”/„kom.” lub prefiksem +48, albo zapisanych w typowych grupach telefonu
  (``600 123 456``, ``600-123-456``, ``(89) 527 00 00``),
* same 9 cyfr bez takiego kontekstu (``516042210``) są pomijane.

Telefony zapisywane są w formacie E.164 (``+48600123456``), e-maile małymi literami.
"""

from __future__ import annotations

import re

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
_PLACEHOLDER_NUMBERS = frozenset({"123456789", "987654321"})


def find_phone(text: str | None) -> str | None:
    """Pierwszy polski numer telefonu w tekście (``+48XXXXXXXXX``) albo ``None``."""
    for match in _PHONE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", match.group("a") or match.group("b") or match.group("c"))
        if len(digits) == 9 and digits[0] != "0" and digits not in _PLACEHOLDER_NUMBERS and len(set(digits)) > 1:
            return "+48" + digits
    return None


def find_email(text: str | None) -> str | None:
    """Pierwszy adres e-mail w tekście (małymi literami) albo ``None``."""
    match = _EMAIL_RE.search(text or "")
    return match.group(0).lower().rstrip(".") if match else None


def extract_contact(*texts: str | None) -> tuple[str | None, str | None]:
    """``(telefon, email)`` z pierwszych pól, w których występują."""
    phone = next((p for p in map(find_phone, texts) if p), None)
    email = next((e for e in map(find_email, texts) if e), None)
    return phone, email
