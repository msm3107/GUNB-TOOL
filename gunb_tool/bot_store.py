"""Dane bota Telegram w tej samej bazie SQLite: użytkownicy, filtry, watchlista, stany leadów, doręczenia.

Każdy użytkownik bota ma własne ustawienia (filtry, tryb raportów, „tylko HOT”) i własną historię
doręczeń – lead trafia do niego raz na każdą „rewizję” (pojawienie się sprawy albo zmianę statusu).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence, TypeGuard

from .models import Investment
from .storage import LeadRepository, investment_from_row
from .text import normalize_text

MODES: tuple[str, ...] = ("natychmiast", "rano", "wieczor")
SETUP_STEPS: tuple[str, ...] = ("branza", "obszar")
SETUP_DONE = "gotowe"
LEAD_STATES: tuple[str, ...] = ("zapisany", "przejrzany", "ukryty")
_STATE_FLAG = {"zapisany": "saved", "przejrzany": "reviewed", "ukryty": "hidden"}
WATCH_KINDS: tuple[str, ...] = ("inwestor", "gmina")


@dataclass(frozen=True)
class LeadFlags:
    """Oznaczenia inwestycji przez użytkownika – niezależne: zapisana, przejrzana, ukryta."""

    saved: bool = False
    reviewed: bool = False
    hidden: bool = False

    @property
    def legacy_state(self) -> str | None:
        """Jedno pole ``stan`` sprzed v7 (dla starszego kodu): ukryty > zapisany > przejrzany."""
        if self.hidden:
            return "ukryty"
        if self.saved:
            return "zapisany"
        return "przejrzany" if self.reviewed else None

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
    branza: str | None = None
    is_active: bool = False
    subscription_ends: str | None = None
    bez_limitu: bool = False
    rodzaj_dostepu: str | None = None
    test_dozwolony: bool = False
    test_start: str | None = None
    test_koniec: str | None = None
    konfiguracja: str | None = None

    @property
    def setup_done(self) -> bool:
        """Czy pierwsza konfiguracja (branża, obszar) jest za nim."""
        return self.konfiguracja == SETUP_DONE

    @property
    def display_name(self) -> str:
        """Nazwa do wyświetlenia (imię, @login albo numer czatu)."""
        return self.imie or (f"@{self.username}" if self.username else str(self.chat_id))

    def has_subscription(self, now_iso: str) -> bool:
        """Czy okno dostępu (test albo abonament) jest otwarte i nie minęło (daty w UTC, format ISO)."""
        return self.is_active and bool(self.subscription_ends) and (self.subscription_ends or "") > now_iso

    @property
    def trial_used(self) -> bool:
        """Test już wystartował (raz na osobę – nawet po odebraniu dostępu drugiego nie ma)."""
        return self.test_start is not None

    @property
    def trial_available(self) -> bool:
        """Admin pozwolił na test, a osoba jeszcze go nie zaczęła."""
        return self.test_dozwolony and not self.trial_used

    @property
    def on_trial(self) -> bool:
        """Obecne (albo ostatnie) okno dostępu to darmowy test."""
        return self.rodzaj_dostepu == "test"


@dataclass(frozen=True)
class Send:
    """Wysyłka zadania (np. ``raport_rano:2026-09-29``) do jednej osoby – wiersz kolejki ``wysylki``.

    Stany: ``oczekuje`` (także przed ponowieniem), ``wysylanie`` (próba w toku), ``wyslano``, ``pusto``
    (nic nowego – bez wiadomości), ``pominieto`` (np. brak dostępu, cisza nocna), ``zablokowany``, ``blad``
    (wyczerpane próby).
    """

    zadanie: str
    chat_id: int
    stan: str
    proby: int
    nastepna_proba: str
    ostatni_blad: str | None
    utworzono: str
    zmieniono: str


@dataclass(frozen=True)
class JobStatus:
    """Stan zadania w tle (np. importu) dla admina; czasy w UTC."""

    nazwa: str
    ostatnio: str | None
    stan: str | None
    start: str | None
    koniec: str | None
    opis: str | None


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

    def subscribers(self, now_iso: str, *, admins: Sequence[int], tryb: str | None = None) -> list[BotUser]:
        """Odbiorcy automatycznych wysyłek – ta sama reguła co ``LeadBot._has_access`` (test to pilnuje):
        dostęp bez limitu, otwarte okno testu/abonamentu albo administrator; tylko status ``aktywny``."""
        marks = ",".join("?" for _ in admins) or "NULL"
        sql = (f"SELECT * FROM bot_users WHERE status = 'aktywny' AND (dostep_bez_limitu = 1"
               f" OR (is_active = 1 AND subscription_ends > ?) OR chat_id IN ({marks}))")
        params: list[object] = [now_iso, *admins]
        if tryb is not None:
            sql += " AND tryb = ?"
            params.append(tryb)
        return [_user(row) for row in self._conn.execute(sql + " ORDER BY chat_id", params).fetchall()]

    def set_subscription(self, chat_id: int, ends_iso: str | None, *, active: bool) -> None:
        """Ustawia okno dostępu wprost: ``active`` i termin (UTC, ISO)."""
        self._update(chat_id, is_active=int(active), subscription_ends=ends_iso)

    # Dostęp: test i abonament --------------------------------------------------------------------

    def set_access(self, chat_id: int, ends_iso: str, *, kind: str = "platny") -> None:
        """Dostęp nadany przez admina do ``ends_iso`` – zastępuje też dostęp bez limitu (nowy model)."""
        self._update(chat_id, is_active=1, subscription_ends=ends_iso, rodzaj_dostepu=kind, dostep_bez_limitu=0)

    def revoke_access(self, chat_id: int) -> None:
        """Admin odbiera dostęp od razu; zapisane inwestycje, ustawienia i historia testu zostają."""
        self._update(chat_id, is_active=0, dostep_bez_limitu=0, test_dozwolony=0)

    def allow_trial(self, chat_id: int) -> bool:
        """Admin pozwala na test; ``False`` – ta osoba już go wykorzystała."""
        cursor = self._conn.execute(
            "UPDATE bot_users SET test_dozwolony = 1, zmieniono = ? WHERE chat_id = ? AND test_start IS NULL",
            (_iso(self.repo.now()), chat_id),
        )
        return cursor.rowcount == 1

    def start_trial(self, chat_id: int, start: datetime, end: datetime) -> bool:
        """Start testu kliknięty przez osobę – raz na zawsze; ``False``, gdy nie był dozwolony albo już ruszył."""
        cursor = self._conn.execute(
            "UPDATE bot_users SET test_start = ?, test_koniec = ?, is_active = 1, subscription_ends = ?,"
            " rodzaj_dostepu = 'test', zmieniono = ? WHERE chat_id = ? AND test_dozwolony = 1 AND test_start IS NULL",
            (_iso(start), _iso(end), _iso(end), _iso(start), chat_id),
        )
        return cursor.rowcount == 1

    def unlimited_users(self) -> list[BotUser]:
        """Dotychczasowi użytkownicy z dostępem bez terminu (sprzed abonamentów)."""
        rows = self._conn.execute("SELECT * FROM bot_users WHERE dostep_bez_limitu = 1 ORDER BY chat_id").fetchall()
        return [_user(row) for row in rows]

    def access_ending(self, now: datetime, until: datetime, *, admins: Sequence[int]) -> list[BotUser]:
        """Okna dostępu kończące się w ``(now, until]``, o których jeszcze nie przypomnieliśmy."""
        return self._access_query("subscription_ends > ? AND subscription_ends <= ?"
                                  " AND przypomniano_koniec IS NOT subscription_ends", [_iso(now), _iso(until)], admins)

    def access_ended(self, now: datetime, *, admins: Sequence[int]) -> list[BotUser]:
        """Okna dostępu, które minęły, a informacja o końcu jeszcze nie wyszła."""
        return self._access_query("subscription_ends <= ? AND zgloszono_koniec IS NOT subscription_ends",
                                  [_iso(now)], admins)

    def mark_access_reminded(self, chat_id: int, ends_iso: str) -> None:
        self._conn.execute("UPDATE bot_users SET przypomniano_koniec = ? WHERE chat_id = ?", (ends_iso, chat_id))

    def mark_access_end_reported(self, chat_id: int, ends_iso: str) -> None:
        self._conn.execute("UPDATE bot_users SET zgloszono_koniec = ? WHERE chat_id = ?", (ends_iso, chat_id))

    def _access_query(self, condition: str, params: list[object], admins: Sequence[int]) -> list[BotUser]:
        marks = ",".join("?" for _ in admins) or "NULL"
        rows = self._conn.execute(
            f"SELECT * FROM bot_users WHERE status = 'aktywny' AND is_active = 1 AND dostep_bez_limitu = 0"
            f" AND chat_id NOT IN ({marks}) AND {condition} ORDER BY chat_id", [*admins, *params]
        ).fetchall()
        return [_user(row) for row in rows]

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

    def set_trade(self, chat_id: int, branza: str | None) -> None:
        """Branża użytkownika do przypomnień „Kiedy dzwonić” (``None`` – bez przypomnień)."""
        self._update(chat_id, branza=branza)

    def set_setup_step(self, chat_id: int, step: str) -> None:
        """Krok pierwszej konfiguracji: ``branza``, ``obszar`` albo ``gotowe``."""
        self._update(chat_id, konfiguracja=step)

    def place_is_known(self, name: str) -> bool:
        """Czy w danych (monitorowany obszar) jest inwestycja z tej miejscowości lub gminy."""
        wanted = normalize_text(name)
        if not wanted:
            return False
        rows = self._conn.execute(
            "SELECT DISTINCT gmina, miejscowosc, adres_opisowy, powiat FROM investments WHERE is_noise = 0"
        ).fetchall()
        return any(wanted in normalize_text(" ".join(value for value in row if value)) for row in rows)

    def nearest_investment_km(self, point: tuple[float, float]) -> float | None:
        """Odległość (w linii prostej) od punktu do najbliższej inwestycji w danych; ``None`` – brak danych."""
        rows = self._conn.execute("SELECT lat, lon FROM investments WHERE lat IS NOT NULL AND lon IS NOT NULL")
        return min((haversine_km(point, (row["lat"], row["lon"])) for row in rows), default=None)

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

    # Oznaczenia inwestycji (⭐ zapisana / ✅ przejrzana / 🗑️ ukryta) – niezależne od siebie ------------

    def lead_flags(self, chat_id: int, id_sprawy: str) -> LeadFlags:
        row = self._conn.execute(
            "SELECT zapisany, przejrzany, ukryty FROM user_leads WHERE chat_id = ? AND id_sprawy = ?",
            (chat_id, id_sprawy),
        ).fetchone()
        return LeadFlags(bool(row["zapisany"]), bool(row["przejrzany"]), bool(row["ukryty"])) if row else LeadFlags()

    def set_lead_flags(self, chat_id: int, id_sprawy: str, *, saved: bool | None = None,
                       reviewed: bool | None = None, hidden: bool | None = None) -> LeadFlags:
        """Ustawia wskazane oznaczenia (pozostałe bez zmian); ponowienie tego samego jest bez skutków.

        Kolumna ``stan`` dostaje wartość pochodną – kod sprzed rozdzielenia oznaczeń (v7) nadal ją czyta.
        """
        current = self.lead_flags(chat_id, id_sprawy)
        flags = LeadFlags(current.saved if saved is None else saved,
                          current.reviewed if reviewed is None else reviewed,
                          current.hidden if hidden is None else hidden)
        if flags == current:
            return flags
        self._conn.execute(
            "INSERT INTO user_leads (chat_id, id_sprawy, stan, zmieniono, zapisany, przejrzany, ukryty)"
            " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (chat_id, id_sprawy) DO UPDATE SET stan = excluded.stan,"
            " zmieniono = excluded.zmieniono, zapisany = excluded.zapisany, przejrzany = excluded.przejrzany,"
            " ukryty = excluded.ukryty",
            (chat_id, id_sprawy, flags.legacy_state or "przejrzany", _iso(self.repo.now()),
             int(flags.saved), int(flags.reviewed), int(flags.hidden)),
        )
        return flags

    def set_lead_state(self, chat_id: int, id_sprawy: str, stan: str) -> None:
        """Zgodność ze starszym API: włącza jedno oznaczenie (``zapisany`` / ``przejrzany`` / ``ukryty``)."""
        if stan not in LEAD_STATES:
            raise ValueError(f"Nieznany stan leada: {stan!r}")
        self.set_lead_flags(chat_id, id_sprawy, **{_STATE_FLAG[stan]: True})

    def clear_lead_state(self, chat_id: int, id_sprawy: str) -> None:
        """„↩️ Przywróć” – zdejmuje tylko ukrycie; zapisanie i przejrzenie zostają."""
        self.set_lead_flags(chat_id, id_sprawy, hidden=False)

    def lead_state(self, chat_id: int, id_sprawy: str) -> str | None:
        """Zgodność ze starszym API: najważniejsze oznaczenie (ukryty > zapisany > przejrzany)."""
        return self.lead_flags(chat_id, id_sprawy).legacy_state

    def saved(self, chat_id: int, *, limit: int, offset: int = 0) -> list[Investment]:
        """Zapisane (i nieukryte) inwestycje użytkownika – ostatnio zmienione pierwsze."""
        rows = self._conn.execute(
            "SELECT i.* FROM user_leads u JOIN investments i ON i.id_sprawy = u.id_sprawy"
            " WHERE u.chat_id = ? AND u.zapisany = 1 AND u.ukryty = 0"
            " ORDER BY u.zmieniono DESC, u.rowid DESC LIMIT ? OFFSET ?",
            (chat_id, limit, offset),
        ).fetchall()
        return [investment_from_row(row) for row in rows]

    def saved_count(self, chat_id: int) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM user_leads WHERE chat_id = ? AND zapisany = 1 AND ukryty = 0", (chat_id,)
        ).fetchone()[0]

    # Doręczenia --------------------------------------------------------------------------------

    def candidates(self, chat_id: int, since_iso: str) -> list[Investment]:
        """Leady (bez szumu i ukrytych), których bieżąca rewizja nie trafiła jeszcze do użytkownika."""
        rows = self._conn.execute(
            """
            SELECT i.* FROM investments i
            WHERE i.is_noise = 0 AND i.status_zmieniony > ?
              AND NOT EXISTS (SELECT 1 FROM user_leads u
                              WHERE u.chat_id = ? AND u.id_sprawy = i.id_sprawy AND u.ukryty = 1)
              AND NOT EXISTS (SELECT 1 FROM deliveries d
                              WHERE d.chat_id = ? AND d.id_sprawy = i.id_sprawy AND d.rewizja = i.status_zmieniony)
            ORDER BY i.status_zmieniony, i.data_aktualizacji, i.nr
            """,
            (since_iso, chat_id, chat_id),
        ).fetchall()
        return [investment_from_row(row) for row in rows]

    def recent_leads(self, chat_id: int, date_from: str) -> list[Investment]:
        """Wszystkie inwestycje (bez szumu i ukrytych) z datą zdarzenia od ``date_from``, także już widziane.

        Bez limitu: filtry użytkownika (promień, rodzaj, kubatura, inwestor) stosuje wywołujący, a limit
        przed filtrowaniem dawał fałszywe „brak pasujących”, gdy pasująca była starsza niż tysiąc innych.
        Kolejność jest stała (data, numer), więc strony „Dalej/Wstecz” się nie przesuwają.
        """
        rows = self._conn.execute(
            """
            SELECT i.* FROM investments i
            WHERE i.is_noise = 0 AND i.data_aktualizacji >= ?
              AND NOT EXISTS (SELECT 1 FROM user_leads u
                              WHERE u.chat_id = ? AND u.id_sprawy = i.id_sprawy AND u.ukryty = 1)
            ORDER BY i.data_aktualizacji DESC, i.nr DESC
            """,
            (date_from, chat_id),
        )
        return [investment_from_row(row) for row in rows]

    def stage_candidates(self, chat_id: int, rewizja: str, decided_since: str) -> list[Investment]:
        """Leady (bez szumu i ukrytych) z decyzją od ``decided_since``, o których nie było przypomnienia ``rewizja``.

        Przypomnienia „Kiedy dzwonić” mają własną rewizję doręczenia (np. ``etap:dach``), więc nie mieszają
        się z raportem nowości, a zmiana branży daje przypomnienia dla nowego etapu.
        """
        rows = self._conn.execute(
            """
            SELECT i.* FROM investments i
            WHERE i.is_noise = 0 AND coalesce(i.data_decyzji, i.data_wplywu) >= ?
              AND NOT EXISTS (SELECT 1 FROM user_leads u
                              WHERE u.chat_id = ? AND u.id_sprawy = i.id_sprawy AND u.ukryty = 1)
              AND NOT EXISTS (SELECT 1 FROM deliveries d
                              WHERE d.chat_id = ? AND d.id_sprawy = i.id_sprawy AND d.rewizja = ?)
            ORDER BY coalesce(i.data_decyzji, i.data_wplywu), i.nr
            """,
            (decided_since, chat_id, chat_id, rewizja),
        ).fetchall()
        return [investment_from_row(row) for row in rows]

    def record_delivery(self, chat_id: int, investments: Iterable[Investment], rodzaj: str,
                        rewizja: str | None = None) -> None:
        """Zapisuje doręczenie; domyślna rewizja to bieżący stan leada (``status_zmieniony``)."""
        now = _iso(self.repo.now())
        self._conn.executemany(
            "INSERT OR IGNORE INTO deliveries (chat_id, id_sprawy, rewizja, rodzaj, doreczono) VALUES (?, ?, ?, ?, ?)",
            [(chat_id, inv.id_sprawy, rewizja or inv.status_zmieniony or "", rodzaj, now) for inv in investments],
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

    # Harmonogram i stan zadań (UTC) ------------------------------------------------------------

    def job_time(self, nazwa: str) -> datetime | None:
        """Ostatnie uruchomienie zadania (albo termin, np. ponowienia importu); ``None`` – brak."""
        row = self._conn.execute("SELECT ostatnio FROM zadania WHERE nazwa = ?", (nazwa,)).fetchone()
        return datetime.fromisoformat(row["ostatnio"]) if row and row["ostatnio"] else None

    def set_job_time(self, nazwa: str, moment: datetime | None) -> None:
        self._conn.execute(
            "INSERT INTO zadania (nazwa, ostatnio) VALUES (?, ?)"
            " ON CONFLICT (nazwa) DO UPDATE SET ostatnio = excluded.ostatnio",
            (nazwa, _iso(moment) if moment else None),
        )

    def job_started(self, nazwa: str) -> None:
        """Początek zadania (np. importu) – widoczny dla admina, zanim się skończy."""
        self._conn.execute(
            "INSERT INTO zadania (nazwa, stan, start) VALUES (?, 'trwa', ?) ON CONFLICT (nazwa) DO UPDATE"
            " SET stan = 'trwa', start = excluded.start, koniec = NULL, opis = NULL",
            (nazwa, _iso(self.repo.now())),
        )

    def job_finished(self, nazwa: str, stan: str, opis: str | None = None) -> None:
        """Koniec zadania: ``ok`` / ``blad`` / ``pominieto`` z krótkim opisem (wynik albo treść błędu)."""
        self._conn.execute(
            "INSERT INTO zadania (nazwa, stan, koniec, opis) VALUES (?, ?, ?, ?) ON CONFLICT (nazwa) DO UPDATE"
            " SET stan = excluded.stan, koniec = excluded.koniec, opis = excluded.opis",
            (nazwa, stan, _iso(self.repo.now()), (opis or "")[:300] or None),
        )

    def job_status(self, nazwa: str) -> JobStatus | None:
        row = self._conn.execute("SELECT * FROM zadania WHERE nazwa = ?", (nazwa,)).fetchone()
        return JobStatus(**dict(row)) if row else None

    # Kolejka wysyłek (raporty, przypomnienia) – każda osoba osobno, z ponowieniami ---------------

    def enqueue_sends(self, zadanie: str, chat_ids: Iterable[int]) -> None:
        """Zadanie wystartowało: po jednej wysyłce na osobę; powtórne dodanie tego samego nic nie zmienia."""
        now = _iso(self.repo.now())
        self._conn.executemany(
            "INSERT OR IGNORE INTO wysylki (zadanie, chat_id, nastepna_proba, utworzono, zmieniono)"
            " VALUES (?, ?, ?, ?, ?)",
            [(zadanie, chat_id, now, now, now) for chat_id in chat_ids],
        )

    def due_sends(self) -> list[Send]:
        """Wysyłki czekające na próbę, których termin już minął – w kolejności dodania."""
        rows = self._conn.execute(
            "SELECT * FROM wysylki WHERE stan = 'oczekuje' AND nastepna_proba <= ? ORDER BY nastepna_proba, rowid",
            (_iso(self.repo.now()),),
        ).fetchall()
        return [Send(**dict(row)) for row in rows]

    def claim_send(self, zadanie: str, chat_id: int) -> bool:
        """Początek próby (``wysylanie``, licznik prób +1); ``False`` – wysyłkę wziął już ktoś inny."""
        cursor = self._conn.execute(
            "UPDATE wysylki SET stan = 'wysylanie', proby = proby + 1, zmieniono = ?"
            " WHERE zadanie = ? AND chat_id = ? AND stan = 'oczekuje'",
            (_iso(self.repo.now()), zadanie, chat_id),
        )
        return cursor.rowcount == 1

    def finish_send(self, zadanie: str, chat_id: int, stan: str, *, blad: str | None = None,
                    retry_at: datetime | None = None) -> None:
        """Wynik próby; z ``retry_at`` wysyłka wraca do kolejki (``oczekuje``) na ten termin."""
        now = _iso(self.repo.now())
        self._conn.execute(
            "UPDATE wysylki SET stan = ?, nastepna_proba = coalesce(?, nastepna_proba),"
            " ostatni_blad = coalesce(?, ostatni_blad), zmieniono = ? WHERE zadanie = ? AND chat_id = ?",
            ("oczekuje" if retry_at else stan, _iso(retry_at) if retry_at else None, (blad or "")[:300] or None,
             now, zadanie, chat_id),
        )

    def requeue_stuck_sends(self, older_than: datetime) -> int:
        """Próby przerwane w trakcie (proces padł) wracają do kolejki – wiadomość mogła już dojść."""
        cursor = self._conn.execute(
            "UPDATE wysylki SET stan = 'oczekuje', ostatni_blad = 'próba przerwana (restart w trakcie wysyłki)',"
            " zmieniono = ? WHERE stan = 'wysylanie' AND zmieniono <= ?",
            (_iso(self.repo.now()), _iso(older_than)),
        )
        return cursor.rowcount

    def send_row(self, zadanie: str, chat_id: int) -> Send | None:
        row = self._conn.execute(
            "SELECT * FROM wysylki WHERE zadanie = ? AND chat_id = ?", (zadanie, chat_id)
        ).fetchone()
        return Send(**dict(row)) if row else None

    def send_counts(self, since: datetime) -> dict[str, dict[str, int]]:
        """Zadania wysyłek od ``since``: ``{zadanie: {stan: liczba osób}}`` – do podglądu admina."""
        counts: dict[str, dict[str, int]] = {}
        for row in self._conn.execute(
            "SELECT zadanie, stan, COUNT(*) AS n FROM wysylki WHERE utworzono >= ? GROUP BY zadanie, stan"
            " ORDER BY min(rowid)", (_iso(since),)
        ):
            counts.setdefault(row["zadanie"], {})[row["stan"]] = row["n"]
        return counts

    def failed_sends(self, since: datetime) -> list[Send]:
        rows = self._conn.execute(
            "SELECT * FROM wysylki WHERE stan = 'blad' AND utworzono >= ? ORDER BY rowid", (_iso(since),)
        ).fetchall()
        return [Send(**dict(row)) for row in rows]

    def prune_sends(self, before: datetime) -> None:
        """Usuwa zakończone wysyłki starsze niż ``before`` (tabela nie rośnie bez końca)."""
        self._conn.execute(
            "DELETE FROM wysylki WHERE utworzono < ? AND stan NOT IN ('oczekuje', 'wysylanie')", (_iso(before),)
        )

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
        branza=row["branza"],
        is_active=bool(row["is_active"]),
        subscription_ends=row["subscription_ends"],
        bez_limitu=bool(row["dostep_bez_limitu"]),
        rodzaj_dostepu=row["rodzaj_dostepu"],
        test_dozwolony=bool(row["test_dozwolony"]),
        test_start=row["test_start"],
        test_koniec=row["test_koniec"],
        konfiguracja=row["konfiguracja"],
    )


def _powiat_label(name: str | None, code: str) -> str:
    if not name:
        return f"powiat {code}"
    return name.removeprefix("powiat ").strip() if name.startswith("powiat ") and name[7:8].isupper() else name


def _iso(moment) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
