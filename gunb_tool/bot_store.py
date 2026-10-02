"""Dane bota Telegram w tej samej bazie SQLite: użytkownicy, filtry, watchlista, stany leadów, doręczenia.

Każdy użytkownik bota ma własne ustawienia (filtry, tryb raportów, „tylko HOT”) i własną historię
doręczeń – lead trafia do niego raz na każdą „rewizję” (pojawienie się sprawy albo zmianę statusu).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence, TypeGuard

from .config import OfferConfig
from .models import Investment
from .storage import LeadRepository, investment_from_row
from .text import normalize_text

MODES: tuple[str, ...] = ("natychmiast", "rano", "wieczor")
SETUP_STEPS: tuple[str, ...] = ("branza", "obszar")
SETUP_DONE = "gotowe"
LEAD_STATES: tuple[str, ...] = ("zapisany", "przejrzany", "ukryty")
_STATE_FLAG = {"zapisany": "saved", "przejrzany": "reviewed", "ukryty": "hidden"}
WATCH_KINDS: tuple[str, ...] = ("inwestor", "gmina")
OUTCOMES: tuple[str, ...] = ("do_sprawdzenia", "sprawdzona", "rozmowa", "oferta", "niepasujaca")
"""Wynik pracy z inwestycją (jeden na osobę i inwestycję) – nie CRM, tylko prosty ślad, co się stało."""
NOT_MATCHING_REASONS: tuple[str, ...] = ("obszar", "rodzaj", "moment", "brak_dzialania", "bledne_dane")
POSITIVE_OUTCOMES: frozenset[str] = frozenset({"rozmowa", "oferta"})
ORDER_STATES: tuple[str, ...] = ("zgloszone", "oplacone", "anulowane")


def mentions_place(place: str, name: str) -> bool:
    """Czy znormalizowany opis miejsca zawiera nazwę całymi słowami („Olsztyn” to nie „Olsztynek” ani „olsztyński”)."""
    wanted = normalize_text(name)
    return bool(wanted) and re.search(rf"(?<!\w){re.escape(wanted)}(?!\w)", place) is not None


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
            in_place = any(mentions_place(place, m) for m in self.miejsca)
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
    wstrzymane: bool = False
    zrodlo: str | None = None
    firma: str | None = None
    prosba_o_test: str | None = None
    porady: tuple[str, ...] = ()
    tips_enabled: bool = True
    podpowiedz_test: str | None = None
    test_przedluzono: str | None = None
    test_przedluzenie_powod: str | None = None

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

    @property
    def ever_had_access(self) -> bool:
        """Czy kiedykolwiek miał dostęp (test, ręczny, opłacony albo sprzed abonamentów) – stąd jego zapisana praca."""
        return self.bez_limitu or self.subscription_ends is not None or self.test_start is not None


@dataclass(frozen=True)
class Outcome:
    """Wynik pracy z inwestycją (:data:`OUTCOMES`), powód „niepasującej” i ocena 👍 (1) / 👎 (-1)."""

    wynik: str | None = None
    powod: str | None = None
    ocena: int | None = None

    @property
    def positive(self) -> bool:
        return self.ocena == 1 or self.wynik in POSITIVE_OUTCOMES


@dataclass(frozen=True)
class Order:
    """Zamówienie dostępu z migawką oferty z chwili zamówienia (cena się nie zmienia po fakcie).

    Stany: ``zgloszone`` → ``oplacone`` (admin potwierdził otrzymaną płatność) albo ``anulowane``.
    To nie jest faktura ani dowód płatności – tylko ślad, kto co zamówił i kiedy admin potwierdził wpłatę.
    """

    id: int
    chat_id: int
    stan: str
    oferta: str
    cena: str
    waluta: str
    podatek: str
    do_zaplaty: str
    opis_ceny: str
    dni: int
    utworzono: str
    zmieniono: str
    oplacono: str | None
    potwierdzil: int | None
    uwagi: str | None
    dostep_do: str | None

    @property
    def number(self) -> str:
        """Numer dla ludzi, np. „Z-7”."""
        return f"Z-{self.id}"


@dataclass(frozen=True)
class Event:
    """Zdarzenie pilotażu (``zdarzenia``): kto, co, której inwestycji, kiedy (UTC) i krótki szczegół."""

    chat_id: int
    rodzaj: str
    id_sprawy: str | None
    kiedy: str
    szczegoly: str | None


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
                 backlog_days: int, zrodlo: str | None = None) -> BotUser:
        """Rejestruje użytkownika przy pierwszym ``/start``; istniejącemu nie zmienia ustawień ani źródła.

        ``backlog_days`` – ile dni wstecz leady są dla nowego użytkownika „nowe” (pierwszy raport);
        ``zrodlo`` – sprawdzony parametr startowy (np. ``strona``), zapisywany tylko przy pierwszym wejściu.
        """
        existing = self.get_user(chat_id)
        if existing is not None:
            return existing
        now = self.repo.now()
        self._conn.execute(
            "INSERT INTO bot_users (chat_id, imie, username, status, nowe_od, utworzono, zmieniono, zrodlo)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, imie, username, status, _iso(now - timedelta(days=backlog_days)), _iso(now), _iso(now), zrodlo),
        )
        return self.get_user(chat_id)  # type: ignore[return-value]

    def set_company(self, chat_id: int, firma: str | None) -> None:
        """Firma osoby (ustawia admin) – raport liczy wtedy firmy, nie tylko konta."""
        self._update(chat_id, firma=firma)

    def request_trial(self, chat_id: int) -> bool:
        """Osoba prosi o test; ``False`` – prosiła już wcześniej (admin dostaje jedno zgłoszenie)."""
        cursor = self._conn.execute(
            "UPDATE bot_users SET prosba_o_test = ?, zmieniono = ? WHERE chat_id = ? AND prosba_o_test IS NULL",
            (_iso(self.repo.now()), _iso(self.repo.now()), chat_id),
        )
        return cursor.rowcount == 1

    def mark_tip(self, chat_id: int, key: str) -> None:
        """Podpowiedź ``key`` została pokazana – drugi raz już nie przyjdzie."""
        user = self.get_user(chat_id)
        if user is not None and key not in user.porady:
            self._update(chat_id, porady=",".join((*user.porady, key)))

    def set_tips_enabled(self, chat_id: int, value: bool) -> None:
        """Podpowiedzi i podsumowania testu (wiadomości usługowe ponad raporty) – włączone domyślnie."""
        self._update(chat_id, podpowiedzi=int(value))

    def mark_trial_nudged(self, chat_id: int, test_start: str) -> None:
        """Podpowiedź po starcie testu bez efektów zaplanowana – raz na dany test."""
        self._conn.execute("UPDATE bot_users SET podpowiedz_test = ? WHERE chat_id = ?", (test_start, chat_id))

    def extend_trial(self, chat_id: int, new_end: datetime, reason: str) -> bool:
        """Jednorazowe przedłużenie testu przez admina (z powodem); ``False`` – brak testu albo już przedłużany.

        Dotyczy tylko okna testowego (trwającego albo zakończonego), nigdy płatnego – i nie resetuje testu.
        """
        now = _iso(self.repo.now())
        cursor = self._conn.execute(
            "UPDATE bot_users SET subscription_ends = ?, test_koniec = ?, is_active = 1, test_przedluzono = ?,"
            " test_przedluzenie_powod = ?, zmieniono = ? WHERE chat_id = ? AND rodzaj_dostepu = 'test'"
            " AND test_start IS NOT NULL AND test_przedluzono IS NULL",
            (_iso(new_end), _iso(new_end), now, reason[:200], now, chat_id),
        )
        return cursor.rowcount == 1

    def users(self, status: str = "aktywny", tryb: str | None = None) -> list[BotUser]:
        sql, params = "SELECT * FROM bot_users WHERE status = ?", [status]
        if tryb is not None:
            sql += " AND tryb = ?"
            params.append(tryb)
        return [_user(row) for row in self._conn.execute(sql + " ORDER BY chat_id", params).fetchall()]

    def subscribers(self, now_iso: str, *, admins: Sequence[int], tryb: str | None = None) -> list[BotUser]:
        """Odbiorcy automatycznych wysyłek – ta sama reguła co ``LeadBot._receives_automatic`` (test to
        pilnuje): status ``aktywny``, bez pauzy i z dostępem (bez limitu, otwarte okno testu/abonamentu, admin)."""
        marks = ",".join("?" for _ in admins) or "NULL"
        sql = (f"SELECT * FROM bot_users WHERE status = 'aktywny' AND wstrzymane = 0 AND (dostep_bez_limitu = 1"
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

    def set_paused(self, chat_id: int, value: bool) -> None:
        """Pauza wszystkich automatycznych wiadomości (raporty, alerty, przypomnienia); dostęp się nie zmienia."""
        self._update(chat_id, wstrzymane=int(value))

    def set_setup_step(self, chat_id: int, step: str) -> None:
        """Krok pierwszej konfiguracji: ``branza``, ``obszar`` albo ``gotowe``."""
        self._update(chat_id, konfiguracja=step)

    def has_investments(self) -> bool:
        """Czy w bazie są już jakiekolwiek dane (świeża instalacja przed pierwszym importem – nie)."""
        return self._conn.execute("SELECT 1 FROM investments WHERE is_noise = 0 LIMIT 1").fetchone() is not None

    def place_is_known(self, name: str) -> bool:
        """Czy w danych (monitorowany obszar) jest inwestycja z tej miejscowości lub gminy."""
        if not normalize_text(name):
            return False
        rows = self._conn.execute(
            "SELECT DISTINCT gmina, miejscowosc, adres_opisowy, powiat FROM investments WHERE is_noise = 0"
        ).fetchall()
        return any(mentions_place(normalize_text(" ".join(value for value in row if value)), name) for row in rows)

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

    # Osobiste przypomnienia („⏰ Przypomnij”) i prywatne notatki ----------------------------------

    def set_reminder(self, chat_id: int, id_sprawy: str, termin: datetime, dni: int) -> None:
        """Jedno przypomnienie na osobę i inwestycję – ponowny wybór przesuwa termin."""
        self._conn.execute(
            "INSERT INTO przypomnienia (chat_id, id_sprawy, termin, dni, utworzono) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (chat_id, id_sprawy) DO UPDATE SET termin = excluded.termin, dni = excluded.dni",
            (chat_id, id_sprawy, _iso(termin), dni, _iso(self.repo.now())),
        )

    def clear_reminder(self, chat_id: int, id_sprawy: str) -> None:
        self._conn.execute("DELETE FROM przypomnienia WHERE chat_id = ? AND id_sprawy = ?", (chat_id, id_sprawy))

    def reminder(self, chat_id: int, id_sprawy: str) -> datetime | None:
        row = self._conn.execute(
            "SELECT termin FROM przypomnienia WHERE chat_id = ? AND id_sprawy = ?", (chat_id, id_sprawy)
        ).fetchone()
        return datetime.fromisoformat(row["termin"]) if row else None

    def due_reminders(self, now: datetime) -> dict[int, list[Investment]]:
        """Przypomnienia po terminie: ``{chat_id: [inwestycje]}`` (najstarsze terminy najpierw)."""
        rows = self._conn.execute(
            "SELECT r.chat_id AS przypomnienie_dla, i.* FROM przypomnienia r"
            " JOIN investments i ON i.id_sprawy = r.id_sprawy WHERE r.termin <= ? ORDER BY r.chat_id, r.termin",
            (_iso(now),),
        ).fetchall()
        due: dict[int, list[Investment]] = {}
        for row in rows:
            due.setdefault(row["przypomnienie_dla"], []).append(investment_from_row(row))
        return due

    def note(self, chat_id: int, id_sprawy: str) -> str | None:
        row = self._conn.execute(
            "SELECT tekst FROM notatki WHERE chat_id = ? AND id_sprawy = ?", (chat_id, id_sprawy)
        ).fetchone()
        return row["tekst"] if row else None

    def set_note(self, chat_id: int, id_sprawy: str, tekst: str) -> None:
        """Prywatna notatka do inwestycji (widzi ją tylko ta osoba)."""
        self._conn.execute(
            "INSERT INTO notatki (chat_id, id_sprawy, tekst, zmieniono) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (chat_id, id_sprawy) DO UPDATE SET tekst = excluded.tekst, zmieniono = excluded.zmieniono",
            (chat_id, id_sprawy, tekst, _iso(self.repo.now())),
        )

    def delete_note(self, chat_id: int, id_sprawy: str) -> None:
        self._conn.execute("DELETE FROM notatki WHERE chat_id = ? AND id_sprawy = ?", (chat_id, id_sprawy))

    # Wynik pracy (📋) i ocena 👍/👎 – jeden wiersz na osobę i inwestycję ------------------------------

    def outcome(self, chat_id: int, id_sprawy: str) -> Outcome:
        row = self._conn.execute(
            "SELECT wynik, powod, ocena FROM wyniki WHERE chat_id = ? AND id_sprawy = ?", (chat_id, id_sprawy)
        ).fetchone()
        return Outcome(row["wynik"], row["powod"], row["ocena"]) if row else Outcome()

    _KEEP = object()

    def set_outcome(self, chat_id: int, id_sprawy: str, *, wynik: object = _KEEP, powod: object = _KEEP,
                    ocena: object = _KEEP) -> bool:
        """Ustawia wynik (``None`` – wyczyść), powód „niepasującej” albo ocenę; ``True`` – coś się zmieniło.

        Ponowione kliknięcie tego samego niczego nie zmienia (ani nie dopisuje zdarzeń – robi to wywołujący
        tylko przy zmianie). Powód zostaje wyłącznie przy wyniku ``niepasujaca``.
        """
        current = self.outcome(chat_id, id_sprawy)
        new_wynik = current.wynik if wynik is self._KEEP else wynik
        new_powod = current.powod if powod is self._KEEP else powod
        new_ocena = current.ocena if ocena is self._KEEP else ocena
        if new_wynik is not None and new_wynik not in OUTCOMES:
            raise ValueError(f"Nieznany wynik: {new_wynik!r}")
        if new_powod is not None and new_powod not in NOT_MATCHING_REASONS:
            raise ValueError(f"Nieznany powód: {new_powod!r}")
        if new_ocena not in (None, 1, -1):
            raise ValueError(f"Ocena to 1 albo -1: {new_ocena!r}")
        if new_wynik != "niepasujaca":
            new_powod = None
        updated = Outcome(new_wynik, new_powod, new_ocena)  # type: ignore[arg-type]
        if updated == current:
            return False
        self._conn.execute(
            "INSERT INTO wyniki (chat_id, id_sprawy, wynik, powod, ocena, zmieniono) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (chat_id, id_sprawy) DO UPDATE SET wynik = excluded.wynik, powod = excluded.powod,"
            " ocena = excluded.ocena, zmieniono = excluded.zmieniono",
            (chat_id, id_sprawy, updated.wynik, updated.powod, updated.ocena, _iso(self.repo.now())),
        )
        return True

    def outcome_counts(self, since: datetime) -> dict[str, int]:
        """Stan ocen i wyników zmienionych od ``since``: pary osoba–inwestycja (powtórne kliknięcia się nie liczą)."""
        counts: dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT wynik, powod, ocena FROM wyniki WHERE zmieniono >= ?", (_iso(since),)
        ):
            if row["ocena"] is not None:
                key = "ocena_plus" if row["ocena"] == 1 else "ocena_minus"
                counts[key] = counts.get(key, 0) + 1
            if row["wynik"]:
                counts[row["wynik"]] = counts.get(row["wynik"], 0) + 1
            if row["powod"]:
                counts[f"powod:{row['powod']}"] = counts.get(f"powod:{row['powod']}", 0) + 1
        return counts

    def owns_work(self, chat_id: int, id_sprawy: str) -> bool:
        """Czy osoba ma przy tej inwestycji własną pracę (zapis, notatkę, wynik, przypomnienie) – tylko te
        inwestycje są widoczne po końcu dostępu, więc stare przyciski nie otwierają nowych danych."""
        row = self._conn.execute(
            "SELECT EXISTS (SELECT 1 FROM user_leads WHERE chat_id = :c AND id_sprawy = :i AND zapisany = 1)"
            " OR EXISTS (SELECT 1 FROM notatki WHERE chat_id = :c AND id_sprawy = :i)"
            " OR EXISTS (SELECT 1 FROM wyniki WHERE chat_id = :c AND id_sprawy = :i)"
            " OR EXISTS (SELECT 1 FROM przypomnienia WHERE chat_id = :c AND id_sprawy = :i)",
            {"c": chat_id, "i": id_sprawy},
        ).fetchone()
        return bool(row[0])

    def work_summary(self, chat_id: int, since: str) -> dict[str, int]:
        """Rzeczywiste działania osoby od ``since`` (UTC ISO) – do podsumowania testu; same liczby z bazy."""
        def one(sql: str, *params: object) -> int:
            return self._conn.execute(sql, (chat_id, *params)).fetchone()[0]

        return {
            "dostarczone": one("SELECT COUNT(DISTINCT id_sprawy) FROM deliveries WHERE chat_id = ? AND doreczono >= ?"
                               " AND rodzaj IN ('raport', 'natychmiast', 'watchlista', 'etap')", since),
            "otwarte": one("SELECT COUNT(DISTINCT id_sprawy) FROM zdarzenia WHERE chat_id = ? AND rodzaj = 'szczegoly'"
                           " AND kiedy >= ?", since),
            "zapisane": one("SELECT COUNT(*) FROM user_leads WHERE chat_id = ? AND zapisany = 1 AND ukryty = 0"),
            "notatki": one("SELECT COUNT(*) FROM notatki WHERE chat_id = ?"),
            "przypomnienia": one("SELECT COUNT(*) FROM przypomnienia WHERE chat_id = ?"),
            "wyniki": one("SELECT COUNT(*) FROM wyniki WHERE chat_id = ? AND wynik IS NOT NULL"),
        }

    # Zamówienia (ręczne potwierdzenie płatności) ----------------------------------------------------------

    def create_order(self, chat_id: int, offer: OfferConfig) -> tuple[Order, bool]:
        """Zamówienie z migawką kompletnej oferty; ``(zamówienie, False)`` – osoba ma już otwarte (to samo)."""
        existing = self.open_order(chat_id)
        if existing is not None:
            return existing, False
        if not offer.complete:
            raise ValueError("Oferta jest niepełna – nie przyjmujemy zamówień")
        now = _iso(self.repo.now())
        try:
            cursor = self._conn.execute(
                "INSERT INTO zamowienia (chat_id, oferta, cena, waluta, podatek, do_zaplaty, opis_ceny, dni,"
                " utworzono, zmieniono) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, offer.name, str(offer.price), offer.currency, offer.tax, str(offer.amount_due),
                 offer.price_line(), offer.period_days, now, now),
            )
        except sqlite3.IntegrityError:  # drugie kliknięcie z innego procesu w tej samej chwili
            return self.open_order(chat_id), False  # type: ignore[return-value]
        return self.get_order(cursor.lastrowid), True  # type: ignore[return-value]

    def get_order(self, order_id: int) -> Order | None:
        row = self._conn.execute("SELECT * FROM zamowienia WHERE id = ?", (order_id,)).fetchone()
        return Order(**dict(row)) if row else None

    def open_order(self, chat_id: int) -> Order | None:
        row = self._conn.execute(
            "SELECT * FROM zamowienia WHERE chat_id = ? AND stan = 'zgloszone'", (chat_id,)
        ).fetchone()
        return Order(**dict(row)) if row else None

    def orders(self, *, stan: str | None = None, chat_id: int | None = None, limit: int = 20) -> list[Order]:
        sql, params = "SELECT * FROM zamowienia WHERE 1 = 1", []
        if stan is not None:
            sql += " AND stan = ?"
            params.append(stan)
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(chat_id)
        rows = self._conn.execute(sql + " ORDER BY id DESC LIMIT ?", [*params, limit]).fetchall()
        return [Order(**dict(row)) for row in rows]

    def confirm_order(self, order_id: int, *, admin: int, note: str | None = None) -> Order | None:
        """Admin potwierdza otrzymaną płatność – dokładnie raz; ``None`` – już opłacone, anulowane albo brak."""
        now = _iso(self.repo.now())
        cursor = self._conn.execute(
            "UPDATE zamowienia SET stan = 'oplacone', oplacono = ?, potwierdzil = ?, uwagi = ?, zmieniono = ?"
            " WHERE id = ? AND stan = 'zgloszone'", (now, admin, (note or "")[:200] or None, now, order_id),
        )
        return self.get_order(order_id) if cursor.rowcount == 1 else None

    def set_order_access(self, order_id: int, ends_iso: str) -> None:
        """Do kiedy dostęp po tej płatności (dla historii zamówień)."""
        self._conn.execute("UPDATE zamowienia SET dostep_do = ? WHERE id = ?", (ends_iso, order_id))

    def cancel_order(self, order_id: int) -> bool:
        cursor = self._conn.execute(
            "UPDATE zamowienia SET stan = 'anulowane', zmieniono = ? WHERE id = ? AND stan = 'zgloszone'",
            (_iso(self.repo.now()), order_id),
        )
        return cursor.rowcount == 1

    def last_paid_order(self, chat_id: int) -> Order | None:
        row = self._conn.execute(
            "SELECT * FROM zamowienia WHERE chat_id = ? AND stan = 'oplacone' ORDER BY oplacono DESC, id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return Order(**dict(row)) if row else None

    # Zdarzenia pilotażu (tylko te, których nie ma w ``deliveries``) --------------------------------

    def record_event(self, chat_id: int, rodzaj: str, id_sprawy: str | None = None,
                     szczegoly: str | None = None) -> None:
        """Zdarzenie do pomiaru pilotażu; ``szczegoly`` – krótki kod (np. źródło, powód), nigdy dane osobowe."""
        self._conn.execute(
            "INSERT INTO zdarzenia (chat_id, rodzaj, id_sprawy, kiedy, szczegoly) VALUES (?, ?, ?, ?, ?)",
            (chat_id, rodzaj, id_sprawy, _iso(self.repo.now()), (szczegoly or "")[:40] or None),
        )

    def has_event(self, chat_id: int, rodzaj: str, *, since: str = "") -> bool:
        return self._conn.execute(
            "SELECT 1 FROM zdarzenia WHERE chat_id = ? AND rodzaj = ? AND kiedy >= ? LIMIT 1", (chat_id, rodzaj, since)
        ).fetchone() is not None

    def events(self, since: datetime | None = None, *, kinds: Sequence[str] = ()) -> list[Event]:
        """Surowe zdarzenia (najstarsze pierwsze) – z nich liczy się lejek i aktywacja (definicja może się zmienić)."""
        sql, params = "SELECT chat_id, rodzaj, id_sprawy, kiedy, szczegoly FROM zdarzenia WHERE kiedy >= ?", \
            [_iso(since) if since else ""]
        if kinds:
            sql += f" AND rodzaj IN ({','.join('?' for _ in kinds)})"
            params += list(kinds)
        return [Event(**dict(row)) for row in self._conn.execute(sql + " ORDER BY kiedy, id", params)]

    def prune_events(self, before: datetime) -> int:
        """Retencja: zdarzenia starsze niż ``before`` są usuwane (dane pomiaru nie leżą bez końca)."""
        return self._conn.execute("DELETE FROM zdarzenia WHERE kiedy < ?", (_iso(before),)).rowcount

    def event_counts(self, since: datetime) -> dict[str, tuple[int, int]]:
        """``{rodzaj: (unikalne osoby, unikalne inwestycje)}`` od ``since``."""
        rows = self._conn.execute(
            "SELECT rodzaj, COUNT(DISTINCT chat_id) AS osoby, COUNT(DISTINCT id_sprawy) AS inwestycje"
            " FROM zdarzenia WHERE kiedy >= ? GROUP BY rodzaj", (_iso(since),)
        ).fetchall()
        return {row["rodzaj"]: (row["osoby"], row["inwestycje"]) for row in rows}

    def delivery_counts(self, since: datetime) -> tuple[int, int]:
        """Wysłane w raportach i alertach od ``since``: (unikalne inwestycje, unikalne osoby).

        Wysłanie to nie przeczytanie – Telegram nie mówi, czy ktoś wiadomość obejrzał.
        """
        row = self._conn.execute(
            "SELECT COUNT(DISTINCT id_sprawy) AS inwestycje, COUNT(DISTINCT chat_id) AS osoby FROM deliveries"
            " WHERE doreczono >= ? AND rodzaj IN ('raport', 'natychmiast', 'watchlista', 'etap')", (_iso(since),)
        ).fetchone()
        return row["inwestycje"], row["osoby"]

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
        wstrzymane=bool(row["wstrzymane"]),
        zrodlo=row["zrodlo"],
        firma=row["firma"],
        prosba_o_test=row["prosba_o_test"],
        porady=tuple(key for key in (row["porady"] or "").split(",") if key),
        tips_enabled=bool(row["podpowiedzi"]),
        podpowiedz_test=row["podpowiedz_test"],
        test_przedluzono=row["test_przedluzono"],
        test_przedluzenie_powod=row["test_przedluzenie_powod"],
    )


def _powiat_label(name: str | None, code: str) -> str:
    if not name:
        return f"powiat {code}"
    return name.removeprefix("powiat ").strip() if name.startswith("powiat ") and name[7:8].isupper() else name


def _iso(moment) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
