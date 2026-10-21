"""Czas: harmonogram i daty dla ludzi – w strefie Europe/Warsaw; w bazie zawsze UTC.

Strefa jest podana wprost, więc godziny raportów nie zależą od strefy ustawionej na serwerze
(VPS-y często mają UTC), a zmiana czasu letni/zimowy przesuwa je razem z zegarkiem klienta.
Na Windowsie dane stref dostarcza pakiet ``tzdata`` (w ``requirements.txt``).
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo("Europe/Warsaw")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local(moment: datetime) -> datetime:
    """Czas polski dla chwili z bazy (UTC)."""
    return moment.astimezone(WARSAW)


def at_local_time(moment: datetime, hhmm: str) -> datetime:
    """Dzisiejsza (wg czasu polskiego) godzina ``HH:MM`` jako chwila w UTC."""
    hour, minute = (int(part) for part in hhmm.split(":"))
    today = local(moment).date()
    return datetime.combine(today, time(hour, minute), tzinfo=WARSAW).astimezone(timezone.utc)
