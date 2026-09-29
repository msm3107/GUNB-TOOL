"""Dane bota Telegram w tej samej bazie SQLite: użytkownicy, filtry, watchlista, stany leadów, doręczenia.

Każdy użytkownik bota ma własne ustawienia (filtry, tryb raportów, „tylko HOT”) i własną historię
doręczeń – lead trafia do niego raz na każdą „rewizję” (pojawienie się sprawy albo zmianę statusu).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Iterable, Sequence, TypeGuard

from .models import Investment
from .storage import LeadRepository, investment_from_row
from .text import normalize_text

MODES: tuple[str, ...] = ("natychmiast", "rano", "wieczor")
LEAD_STATES: tuple[str, ...] = ("zapisany", "przejrzany", "ukryty")
WATCH_KINDS: tuple[str, ...] = ("inwestor", "gmina")

_LEGAL_FORM_RE = re.compile(
    r"(?<![\w])(spolka z ograniczona odpowiedzialnoscia|spolka komandytowo-akcyjna|spolka komandytowa"
    r"|spolka akcyjna|spolka jawna|spolka cywilna|spolka partnerska"
    r"|sp\.? ?z ?o\.? ?o\.?|sp\.? ?k\.? ?a\.?|sp\.? ?k\.?|sp\.? ?j\.?|sp\.? ?p\.?|s\.? ?a\.?|s\.? ?c\.?)(?![\w])"
)
_PUNCTUATION_RE = re.compile(r"[^\w\s]")
_NUMBER_RE = re.compile(r"(?<![\w])\d+(?![\w])")


# --- Filtry użytkownika ------------------------------------------------------------------------

@dataclass(frozen=True)
class UserFilters:
    """Filtry leadów użytkownika (warunki łączone „i”; lokalizacje – „lub”).

    Attributes:
        powiaty: kody TERYT powiatów.
        miejsca: fragmenty nazw miejscowości/gmin (np. „Warszawa”).
        kategorie: kategorie leadów (``mieszkaniowa-wielorodzinna``…); puste = dowolne.
        min_kubatura: minimalna kubatura w m³ (leady bez kubatury nie przechodzą).
        inwestor: ``None`` – dowolny, ``"firma"`` – tylko jawni inwestorzy, inny tekst – fragment nazwy.
        baza: ``(lat, lon)`` bazy firmy z pinezki Telegrama – od niej liczona jest odległość do budów.
        promien_km: „📍 Blisko mnie” – tylko budowy w tym promieniu od bazy (w linii prostej);
            zastępuje wtedy powiaty i miejscowości.
    """

    powiaty: tuple[str, ...] = ()
    miejsca: tuple[str, ...] = ()
    kategorie: tuple[str, ...] = ()
    min_kubatura: float | None = None
    inwestor: str | None = None
    baza: tuple[float, float] | None = None
    promien_km: int | None = None

    @property
    def radius_active(self) -> bool:
        """Czy działa filtr „📍 Blisko mnie” (jest baza i promień)."""
        return self.baza is not None and bool(self.promien_km)

    def distance_km(self, inv: Investment) -> float | None:
        """Odległość budowy od bazy w km (w linii prostej); ``None`` bez bazy lub współrzędnych."""
        if self.baza is None or inv.lat is None or inv.lon is None:
            return None
        return haversine_km(self.baza, (inv.lat, inv.lon))

    def matches(self, inv: Investment) -> bool:
        """Czy lead spełnia wszystkie ustawione warunki."""
        if self.radius_active:
            distance = self.distance_km(inv)
            if distance is None or distance > (self.promien_km or 0):
                return False
        elif self.powiaty or self.miejsca:
            place = normalize_text(" ".join(p for p in (inv.gmina, inv.miejscowosc, inv.adres_opisowy, inv.powiat) if p))
            in_powiat = inv.powiat_teryt in self.powiaty
            in_place = any(normalize_text(m) in place for m in self.miejsca if normalize_text(m))
            if not (in_powiat or in_place):
                return False
        if self.kategorie and inv.kategoria not in self.kategorie:
            return False
        if self.min_kubatura is not None and (inv.kubatura is None or inv.kubatura < self.min_kubatura):
            return False
        if self.inwestor == "firma":
            return inv.inwestor is not None
        if self.inwestor:
            return bool(inv.inwestor) and normalize_text(self.inwestor) in normalize_text(inv.inwestor)
        return True

    def is_empty(self) -> bool:
        """Czy nie ustawiono żadnego warunku (sama zapamiętana baza, bez promienia, niczego nie filtruje)."""
        return not (self.powiaty or self.miejsca or self.kategorie or self.min_kubatura is not None
                    or self.inwestor or self.radius_active)

    def to_json(self) -> str:
        """Postać zapisywana w bazie."""
        return json.dumps({
            "powiaty": list(self.powiaty),
            "miejsca": list(self.miejsca),
            "kategorie": list(self.kategorie),
            "min_kubatura": self.min_kubatura,
            "inwestor": self.inwestor,
            "baza": list(self.baza) if self.baza else None,
            "promien_km": self.promien_km,
        }, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str | None) -> UserFilters:
        """Odtwarza filtry z bazy; uszkodzony zapis daje filtry puste."""
        try:
            data = json.loads(text or "{}")
        except ValueError:
            return cls()
        if not isinstance(data, dict):
            return cls()
        kubatura = data.get("min_kubatura")
        baza = data.get("baza")
        promien = data.get("promien_km")
        return cls(
            powiaty=tuple(str(v) for v in data.get("powiaty") or ()),
            miejsca=tuple(str(v) for v in data.get("miejsca") or ()),
            kategorie=tuple(str(v) for v in data.get("kategorie") or ()),
            min_kubatura=float(kubatura) if isinstance(kubatura, (int, float)) else None,
            inwestor=str(data["inwestor"]) if data.get("inwestor") else None,
            baza=(float(baza[0]), float(baza[1])) if _is_point(baza) else None,
            promien_km=int(promien) if isinstance(promien, (int, float)) and promien > 0 else None,
        )


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Odległość w km między punktami ``(lat, lon)`` po powierzchni Ziemi (wzór haversine)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _is_point(value: object) -> TypeGuard[Sequence[float]]:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value))


@dataclass(frozen=True)
class BotUser:
    """Użytkownik bota."""

    chat_id: int
    imie: str | None
    username: str | None
    status: str
    tryb: str
    tylko_hot: bool
    filtry: UserFilters = field(default_factory=UserFilters)
    oczekuje_na: str | None = None
    nowe_od: str = ""
    ostatni_raport: str | None = None

    @property
    def display_name(self) -> str:
        """Nazwa do wyświetlenia (imię, @login albo numer czatu)."""
        return self.imie or (f"@{self.username}" if self.username else str(self.chat_id))


@dataclass(frozen=True)
class WatchItem:
    """Pozycja watchlisty: obserwowany inwestor (klucz nazwy) albo gmina (kod TERC)."""

    id: int
    rodzaj: str
    wartosc: str
    etykieta: str


def investor_key(name: str | None) -> str | None:
    """Klucz inwestora do obserwowania: bez formy prawnej, interpunkcji i numerów spółek celowych.

    Deweloperzy działają przez spółki celowe („Napollo 3 Sp. z o.o.”, „Napollo 4 …”) –
    obserwacja jednej z nich obejmuje całą grupę.
    """
    text = _LEGAL_FORM_RE.sub(" ", normalize_text(name))
    text = _NUMBER_RE.sub(" ", _PUNCTUATION_RE.sub(" ", text))
    text = " ".join(text.split())
    return text or None


def watch_match(inv: Investment, items: Sequence[WatchItem]) -> WatchItem | None:
    """Pierwsza pozycja watchlisty, której dotyczy lead (albo ``None``)."""
    key = investor_key(inv.inwestor)
    for item in items:
        if item.rodzaj == "inwestor" and key and item.wartosc == key:
            return item
        if item.rodzaj == "gmina" and inv.gmina_teryt and item.wartosc == inv.gmina_teryt:
            return item
    return None


# --- Magazyn -------------------------------------------------------------------------------

class BotStore:
    """Operacje na tabelach bota; korzysta z połączenia i zegara :class:`LeadRepository`."""

    def __init__(self, repo: LeadRepository) -> None:
        self.repo = repo
        self._conn = repo.connection

    # Użytkownicy ---------------------------------------------------------------------------

    def get_user(self, chat_id: int) -> BotUser | None:
        row = self._conn.execute("SELECT * FROM bot_users WHERE chat_id = ?", (chat_id,)).fetchone()
        return _user(row) if row else None

    def register(self, chat_id: int, imie: str | None, username: str | None, *, status: str,
                 backlog_days: int) -> BotUser:
        """Rejestruje użytkownika przy pierwszym ``/start``; istniejącemu nie zmienia ustawień.

        ``backlog_days`` – ile dni wstecz leady są dla nowego użytkownika „nowe” (pierwszy raport).
        """
        existing = self.get_user(chat_id)
        if existing is not None:
            return existing
        now = self.repo.now()
        self._conn.execute(
            "INSERT INTO bot_users (chat_id, imie, username, status, nowe_od, utworzono, zmieniono)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, imie, username, status, _iso(now - timedelta(days=backlog_days)), _iso(now), _iso(now)),
        )
        return self.get_user(chat_id)  # type: ignore[return-value]

    def users(self, status: str = "aktywny", tryb: str | None = None) -> list[BotUser]:
        sql, params = "SELECT * FROM bot_users WHERE status = ?", [status]
        if tryb is not None:
            sql += " AND tryb = ?"
            params.append(tryb)
        return [_user(row) for row in self._conn.execute(sql + " ORDER BY chat_id", params).fetchall()]

    def set_status(self, chat_id: int, status: str) -> None:
        self._update(chat_id, status=status)

    def set_mode(self, chat_id: int, tryb: str) -> None:
        if tryb not in MODES:
            raise ValueError(f"Nieznany tryb: {tryb!r}")
        self._update(chat_id, tryb=tryb)

    def set_hot_only(self, chat_id: int, value: bool) -> None:
        self._update(chat_id, tylko_hot=int(value))

    def set_filters(self, chat_id: int, filters: UserFilters) -> None:
        self._update(chat_id, filtry=filters.to_json())

    def set_awaiting(self, chat_id: int, what: str | None) -> None:
        self._update(chat_id, oczekuje_na=what)

    def mark_report(self, chat_id: int, when_iso: str) -> None:
        self._update(chat_id, ostatni_raport=when_iso)

    # Watchlista -------------------------------------------------------------------------------

    def add_watch(self, chat_id: int, rodzaj: str, wartosc: str, etykieta: str) -> bool:
        """Dodaje obserwację; ``False``, gdy już istniała."""
        if rodzaj not in WATCH_KINDS:
            raise ValueError(f"Nieznany rodzaj obserwacji: {rodzaj!r}")
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO watchlist (chat_id, rodzaj, wartosc, etykieta, utworzono) VALUES (?, ?, ?, ?, ?)",
            (chat_id, rodzaj, wartosc, etykieta, _iso(self.repo.now())),
        )
        return cursor.rowcount == 1

    def remove_watch(self, chat_id: int, watch_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM watchlist WHERE chat_id = ? AND id = ?", (chat_id, watch_id))
        return cursor.rowcount == 1

    def watchlist(self, chat_id: int) -> list[WatchItem]:
        rows = self._conn.execute(
            "SELECT id, rodzaj, wartosc, etykieta FROM watchlist WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()
        return [WatchItem(**dict(row)) for row in rows]

    # Stany leadów (⭐ / ✅ / 🗑️) -----------------------------------------------------------------

    def set_lead_state(self, chat_id: int, id_sprawy: str, stan: str) -> None:
        if stan not in LEAD_STATES:
            raise ValueError(f"Nieznany stan leada: {stan!r}")
        self._conn.execute(
            "INSERT INTO user_leads (chat_id, id_sprawy, stan, zmieniono) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (chat_id, id_sprawy) DO UPDATE SET stan = excluded.stan, zmieniono = excluded.zmieniono",
            (chat_id, id_sprawy, stan, _iso(self.repo.now())),
        )

    def clear_lead_state(self, chat_id: int, id_sprawy: str) -> None:
        """Usuwa oznaczenie leada (np. „↩️ Przywróć” po ukryciu)."""
        self._conn.execute("DELETE FROM user_leads WHERE chat_id = ? AND id_sprawy = ?", (chat_id, id_sprawy))

    def lead_state(self, chat_id: int, id_sprawy: str) -> str | None:
        row = self._conn.execute(
            "SELECT stan FROM user_leads WHERE chat_id = ? AND id_sprawy = ?", (chat_id, id_sprawy)
        ).fetchone()
        return row["stan"] if row else None

    def saved(self, chat_id: int, *, limit: int, offset: int = 0) -> list[Investment]:
        """Zapisane leady użytkownika – ostatnio zapisane pierwsze."""
        rows = self._conn.execute(
            "SELECT i.* FROM user_leads u JOIN investments i ON i.id_sprawy = u.id_sprawy"
            " WHERE u.chat_id = ? AND u.stan = 'zapisany' ORDER BY u.zmieniono DESC, u.rowid DESC LIMIT ? OFFSET ?",
            (chat_id, limit, offset),
        ).fetchall()
        return [investment_from_row(row) for row in rows]

    def saved_count(self, chat_id: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM user_leads WHERE chat_id = ? AND stan = 'zapisany'", (chat_id,)
        ).fetchone()[0]

    # Doręczenia --------------------------------------------------------------------------------

    def candidates(self, chat_id: int, since_iso: str) -> list[Investment]:
        """Leady (bez szumu i ukrytych), których bieżąca rewizja nie trafiła jeszcze do użytkownika."""
        rows = self._conn.execute(
            """
            SELECT i.* FROM investments i
            WHERE i.is_noise = 0 AND i.status_zmieniony > ?
              AND NOT EXISTS (SELECT 1 FROM user_leads u
                              WHERE u.chat_id = ? AND u.id_sprawy = i.id_sprawy AND u.stan = 'ukryty')
              AND NOT EXISTS (SELECT 1 FROM deliveries d
                              WHERE d.chat_id = ? AND d.id_sprawy = i.id_sprawy AND d.rewizja = i.status_zmieniony)
            ORDER BY i.status_zmieniony, i.data_aktualizacji, i.nr
            """,
            (since_iso, chat_id, chat_id),
        ).fetchall()
        return [investment_from_row(row) for row in rows]

    def recent_leads(self, chat_id: int, date_from: str, limit: int = 1000) -> list[Investment]:
        """Leady (bez szumu i ukrytych) z datą zdarzenia od ``date_from`` (RRRR-MM-DD), także już widziane.

        Służy do „pasujących z ostatnich dni” – np. zaraz po zmianie filtrów.
        """
        rows = self._conn.execute(
            """
            SELECT i.* FROM investments i
            WHERE i.is_noise = 0 AND i.data_aktualizacji >= ?
              AND NOT EXISTS (SELECT 1 FROM user_leads u
                              WHERE u.chat_id = ? AND u.id_sprawy = i.id_sprawy AND u.stan = 'ukryty')
            ORDER BY i.data_aktualizacji DESC, i.nr DESC LIMIT ?
            """,
            (date_from, chat_id, limit),
        ).fetchall()
        return [investment_from_row(row) for row in rows]

    def record_delivery(self, chat_id: int, investments: Iterable[Investment], rodzaj: str) -> None:
        now = _iso(self.repo.now())
        self._conn.executemany(
            "INSERT OR IGNORE INTO deliveries (chat_id, id_sprawy, rewizja, rodzaj, doreczono) VALUES (?, ?, ?, ?, ?)",
            [(chat_id, inv.id_sprawy, inv.status_zmieniony or "", rodzaj, now) for inv in investments],
        )

    def deliveries_since(self, chat_id: int, since_iso: str, rodzaj: str) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE chat_id = ? AND rodzaj = ? AND doreczono > ?",
            (chat_id, rodzaj, since_iso),
        ).fetchone()[0]

    # Zadania i słowniki ---------------------------------------------------------------------------

    def job_last_run(self, nazwa: str) -> str | None:
        row = self._conn.execute("SELECT ostatnio FROM bot_jobs WHERE nazwa = ?", (nazwa,)).fetchone()
        return row["ostatnio"] if row else None

    def mark_job(self, nazwa: str, when_iso: str) -> None:
        self._conn.execute(
            "INSERT INTO bot_jobs (nazwa, ostatnio) VALUES (?, ?)"
            " ON CONFLICT (nazwa) DO UPDATE SET ostatnio = excluded.ostatnio",
            (nazwa, when_iso),
        )

    def clear_job(self, nazwa: str) -> None:
        self._conn.execute("DELETE FROM bot_jobs WHERE nazwa = ?", (nazwa,))

    def place_options(self, powiat_codes: Sequence[str], limit: int = 30) -> list[tuple[str, str]]:
        """Powiaty do wyboru w filtrach: ``(kod, nazwa)`` – nazwa z danych ULDK, gdy już jest w bazie.

        Bez listy powiatów w konfiguracji (monitorowane całe województwo) proponuje ``limit``
        powiatów z największą liczbą leadów.
        """
        rows = self._conn.execute(
            "SELECT powiat_teryt, max(powiat) AS nazwa, COUNT(*) AS n FROM investments"
            " WHERE powiat_teryt IS NOT NULL GROUP BY powiat_teryt ORDER BY n DESC"
        ).fetchall()
        names = {row["powiat_teryt"]: row["nazwa"] for row in rows}
        codes = list(powiat_codes) or [row["powiat_teryt"] for row in rows][:limit]
        return [(code, _powiat_label(names.get(code), code)) for code in codes]

    # Wewnętrzne ----------------------------------------------------------------------------------

    def _update(self, chat_id: int, **values: object) -> None:
        values["zmieniono"] = _iso(self.repo.now())
        assignments = ", ".join(f"{column} = ?" for column in values)
        self._conn.execute(f"UPDATE bot_users SET {assignments} WHERE chat_id = ?", [*values.values(), chat_id])


def _user(row) -> BotUser:
    return BotUser(
        chat_id=row["chat_id"],
        imie=row["imie"],
        username=row["username"],
        status=row["status"],
        tryb=row["tryb"],
        tylko_hot=bool(row["tylko_hot"]),
        filtry=UserFilters.from_json(row["filtry"]),
        oczekuje_na=row["oczekuje_na"],
        nowe_od=row["nowe_od"],
        ostatni_raport=row["ostatni_raport"],
    )


def _powiat_label(name: str | None, code: str) -> str:
    if not name:
        return f"powiat {code}"
    return name.removeprefix("powiat ").strip() if name.startswith("powiat ") and name[7:8].isupper() else name


def _iso(moment) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
