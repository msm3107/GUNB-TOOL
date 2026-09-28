"""Narzędzia do normalizacji polskiego tekstu z rejestru GUNB."""

from __future__ import annotations

import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
# Litery, których NFKD nie rozkłada na literę bazową + znak diakrytyczny.
_EXTRA_FOLD = str.maketrans({"ł": "l", "Ł": "L", "đ": "d", "Đ": "D"})


def fold_polish(text: str) -> str:
    """Usuwa polskie znaki diakrytyczne (``"Łódź"`` -> ``"Lodz"``)."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_EXTRA_FOLD))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(text: str | None) -> str:
    """Zwraca tekst do porównań: małe litery, bez diakrytyków, pojedyncze spacje."""
    if not text:
        return ""
    return _WHITESPACE.sub(" ", fold_polish(text).lower()).strip()


def clean(value: str | None) -> str | None:
    """Czyści surową wartość z CSV: scala białe znaki, ``""``/puste -> ``None``."""
    if value is None:
        return None
    text = _WHITESPACE.sub(" ", value).strip()
    if text in ("", '""'):
        return None
    return text
