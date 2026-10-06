"""Raport tekstowy z faktami rejestrowymi; rendering adapterów należy do kolejnych PR."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence
import unicodedata
from urllib.parse import parse_qs, urlsplit

from .models import Investment, Status
from .notification_models import MAX_BODY_BYTES, MAX_REPORT_LEADS, LeadRef, NotificationEndpoint, ReportPart, bounded_int, utc_iso


def _clean(value: str | None, limit: int = 200) -> str:
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in (value or "")[:limit * 4])
    return " ".join(text.split())[:limit]


def _map_url(value: str | None) -> str | None:
    if not value or len(value) > 512 or any(ch.isspace() or unicodedata.category(ch) in ("Cc", "Cf") for ch in value):
        return None
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname in ("google.com", "www.google.com")
                and url.port in (None, 443) and url.username is None and url.password is None
                and url.path in ("/maps", "/maps/") and set(parse_qs(url.query)) == {"q"} and not url.fragment):
            return value
    except ValueError:
        pass
    return None


def build_report(endpoint: NotificationEndpoint, investments: Sequence[Investment], *,
                 now: datetime, part_size: int = 20) -> tuple[ReportPart, ...]:
    """Do 20 jawnie wybranych inwestycji; filtry i dostęp sprawdza store przy zapisie i dispatch."""
    utc_iso(now)
    bounded_int(part_size, 1, MAX_REPORT_LEADS)
    if len(investments) > MAX_REPORT_LEADS or any(not isinstance(inv, Investment) for inv in investments):
        raise ValueError("Niepoprawna partia inwestycji")
    refs = tuple(LeadRef(inv.id_sprawy, inv.status_zmieniony or "") for inv in investments)
    if len(set(refs)) != len(refs):
        raise ValueError("Powtórzona inwestycja")
    parts = []
    title = f"Żółta Tablica — raport {utc_iso(now)[:10]}"
    header = ["Żółta Tablica — inwestycje do sprawdzenia",
              "Dane z rejestru GUNB. Status sprawy nie określa etapu robót."]
    lines, selected = list(header), []
    for inv, ref in zip(investments, refs):
        try:
            status = Status(inv.status).label
        except ValueError:
            status = _clean(inv.status)
        entry = ["", f"{_clean(inv.id_sprawy)} — {_clean(inv.nazwa_zamierzenia) or 'Inwestycja'}",
                 f"Lokalizacja: {_clean(inv.miejscowosc or inv.adres_opisowy or inv.gmina) or 'brak danych'}",
                 f"Status GUNB: {status}", f"Data danych: {_clean(inv.data_aktualizacji, 30) or 'brak danych'}"]
        url = _map_url(inv.google_maps_url)
        if url:
            entry.append(f"Mapa: {url}")
        if selected and (len(selected) == part_size or len("\n".join(lines + entry).encode("utf-8")) > MAX_BODY_BYTES):
            parts.append(ReportPart(endpoint.chat_id, endpoint.version, title, "\n".join(lines), tuple(selected)))
            lines, selected = list(header), []
        lines.extend(entry)
        selected.append(ref)
    if selected:
        parts.append(ReportPart(endpoint.chat_id, endpoint.version, title, "\n".join(lines), tuple(selected)))
    return tuple(parts)
