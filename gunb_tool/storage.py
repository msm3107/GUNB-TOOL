"""Warstwa trwałości: SQLite z tabelą ``investments``, historią statusów i cache geokodowania.

Wykrywanie zmian (``upsert``):

* ``NEW`` – sprawa pojawiła się pierwszy raz,
* ``STATUS_CHANGED`` – zmienił się status (np. ``wniosek`` → ``decyzja``): wpis w ``status_history``,
  wyzerowanie ``czy_wyslano`` – lead trafi ponownie do powiadomień,
* ``UPDATED`` – zmieniły się inne pola (trafi do synchronizacji arkusza, bez ponownego powiadomienia),
* ``UNCHANGED`` – bez zmian (aktualizowany jest tylko znacznik ``ostatnio_widziany``).
"""

from __future__ import annotations

import enum
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .geocoding_uldk import CachedGeocode, GeocodeResult
from .models import CONTENT_FIELDS, Investment

BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS investments (
    id_sprawy              TEXT PRIMARY KEY,
    zrodlo                 TEXT NOT NULL,
    status                 TEXT NOT NULL,
    status_opis            TEXT,
    data_aktualizacji      TEXT,
    data_wplywu            TEXT,
    data_decyzji           TEXT,
    numer_urzedu           TEXT,
    numer_decyzji          TEXT,
    organ                  TEXT,
    kategoria              TEXT,
    kategoria_obiektu      TEXT,
    rodzaj_robot           TEXT,
    nazwa_zamierzenia      TEXT,
    adres_opisowy          TEXT,
    miejscowosc            TEXT,
    wojewodztwo            TEXT,
    powiat                 TEXT,
    gmina                  TEXT,
    powiat_teryt           TEXT,
    gmina_teryt            TEXT,
    teryt_dzialki          TEXT,
    dzialki                TEXT NOT NULL DEFAULT '[]',
    lat                    REAL,
    lon                    REAL,
    precyzja_geo           TEXT,
    google_maps_url        TEXT,
    geoportal_url          TEXT,
    inwestor               TEXT,
    projektant             TEXT,
    projektant_uprawnienia TEXT,
    pracownia              TEXT,
    kubatura               REAL,
    is_residential         INTEGER NOT NULL DEFAULT 0,
    is_commercial          INTEGER NOT NULL DEFAULT 0,
    is_noise               INTEGER NOT NULL DEFAULT 0,
    czy_wyslano            INTEGER NOT NULL DEFAULT 0,
    wyslano_kanaly         TEXT NOT NULL DEFAULT '',
    utworzono              TEXT NOT NULL,
    zmieniono              TEXT NOT NULL,
    status_zmieniony       TEXT NOT NULL,
    zsynchronizowano       TEXT,
    ostatnio_widziany      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_investments_status ON investments (status);
CREATE INDEX IF NOT EXISTS ix_investments_notify ON investments (czy_wyslano, status_zmieniony);
CREATE INDEX IF NOT EXISTS ix_investments_powiat ON investments (powiat_teryt);

CREATE TABLE IF NOT EXISTS status_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    id_sprawy    TEXT NOT NULL REFERENCES investments (id_sprawy) ON DELETE CASCADE,
    stary_status TEXT,
    nowy_status  TEXT NOT NULL,
    zmieniono    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_status_history_case ON status_history (id_sprawy);

CREATE TABLE IF NOT EXISTS geocode_cache (
    klucz    TEXT PRIMARY KEY,
    wynik    TEXT,
    zapisano TEXT NOT NULL
);
"""

_BOT_SCHEMA = """
ALTER TABLE investments ADD COLUMN punkty INTEGER;
ALTER TABLE investments ADD COLUMN priorytet TEXT;
ALTER TABLE investments ADD COLUMN nr INTEGER;
UPDATE investments SET nr = rowid;
CREATE UNIQUE INDEX IF NOT EXISTS ix_investments_nr ON investments (nr);

CREATE TABLE IF NOT EXISTS bot_users (
    chat_id        INTEGER PRIMARY KEY,
    imie           TEXT,
    username       TEXT,
    status         TEXT NOT NULL DEFAULT 'oczekuje',
    tryb           TEXT NOT NULL DEFAULT 'rano',
    tylko_hot      INTEGER NOT NULL DEFAULT 0,
    filtry         TEXT NOT NULL DEFAULT '{}',
    oczekuje_na    TEXT,
    nowe_od        TEXT NOT NULL,
    ostatni_raport TEXT,
    utworzono      TEXT NOT NULL,
    zmieniono      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   INTEGER NOT NULL REFERENCES bot_users (chat_id) ON DELETE CASCADE,
    rodzaj    TEXT NOT NULL,
    wartosc   TEXT NOT NULL,
    etykieta  TEXT NOT NULL,
    utworzono TEXT NOT NULL,
    UNIQUE (chat_id, rodzaj, wartosc)
);

CREATE TABLE IF NOT EXISTS user_leads (
    chat_id   INTEGER NOT NULL REFERENCES bot_users (chat_id) ON DELETE CASCADE,
    id_sprawy TEXT NOT NULL REFERENCES investments (id_sprawy) ON DELETE CASCADE,
    stan      TEXT NOT NULL,
    zmieniono TEXT NOT NULL,
    PRIMARY KEY (chat_id, id_sprawy)
);

CREATE TABLE IF NOT EXISTS deliveries (
    chat_id   INTEGER NOT NULL REFERENCES bot_users (chat_id) ON DELETE CASCADE,
    id_sprawy TEXT NOT NULL REFERENCES investments (id_sprawy) ON DELETE CASCADE,
    rewizja   TEXT NOT NULL,
    rodzaj    TEXT NOT NULL,
    doreczono TEXT NOT NULL,
    PRIMARY KEY (chat_id, id_sprawy, rewizja)
);
CREATE INDEX IF NOT EXISTS ix_deliveries_chat_time ON deliveries (chat_id, doreczono);

CREATE TABLE IF NOT EXISTS bot_jobs (
    nazwa    TEXT PRIMARY KEY,
    ostatnio TEXT NOT NULL
);
"""

_CONTACT_COLUMNS = """
ALTER TABLE investments ADD COLUMN telefon TEXT;
ALTER TABLE investments ADD COLUMN email TEXT;
"""

_TRADE_COLUMN = """
ALTER TABLE bot_users ADD COLUMN branza TEXT;
"""

_SUBSCRIPTION_COLUMNS = """
ALTER TABLE bot_users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bot_users ADD COLUMN subscription_ends TEXT;
"""

def _independent_lead_flags(conn: sqlite3.Connection, from_version: int) -> None:
    """v7: zapisanie, przejrzenie i ukrycie jako niezależne flagi (dotąd jedno pole ``stan`` – nadpisywało się).

    Odtwarza tylko to, co da się odczytać ze starego pola; nadpisanych wcześniej stanów nie zgadujemy.
    Kolumna ``stan`` zostaje (kod sprzed v7 nadal ją czyta) i jest dalej uzupełniana wartością pochodną.
    """
    _run_script(conn, """
        ALTER TABLE user_leads ADD COLUMN zapisany INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE user_leads ADD COLUMN przejrzany INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE user_leads ADD COLUMN ukryty INTEGER NOT NULL DEFAULT 0;
        UPDATE user_leads SET zapisany = (stan = 'zapisany'), przejrzany = (stan = 'przejrzany'),
                              ukryty = (stan = 'ukryty');
    """)


_JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS zadania (
    nazwa    TEXT PRIMARY KEY,
    ostatnio TEXT,
    stan     TEXT,
    start    TEXT,
    koniec   TEXT,
    opis     TEXT
);

CREATE TABLE IF NOT EXISTS wysylki (
    zadanie        TEXT NOT NULL,
    chat_id        INTEGER NOT NULL,
    stan           TEXT NOT NULL DEFAULT 'oczekuje',
    proby          INTEGER NOT NULL DEFAULT 0,
    nastepna_proba TEXT NOT NULL,
    ostatni_blad   TEXT,
    utworzono      TEXT NOT NULL,
    zmieniono      TEXT NOT NULL,
    PRIMARY KEY (zadanie, chat_id)
);
CREATE INDEX IF NOT EXISTS ix_wysylki_stan ON wysylki (stan, nastepna_proba);

CREATE TABLE IF NOT EXISTS blokady (
    nazwa      TEXT PRIMARY KEY,
    wlasciciel TEXT NOT NULL,
    wygasa     TEXT NOT NULL
);
"""


def _jobs_in_utc(conn: sqlite3.Connection, from_version: int) -> None:
    """v8: stan zadań w UTC (``zadania``), kolejka wysyłek z ponowieniami (``wysylki``), blokady importu.

    Terminy z ``bot_jobs`` (czas lokalny serwera, bez strefy) trafiają do ``zadania`` w UTC, więc po
    aktualizacji bot nie powtarza dzisiejszych zadań. ``bot_jobs`` zostaje: offset Telegrama i starszy kod.
    """
    _run_script(conn, _JOBS_SCHEMA)
    for nazwa, ostatnio in conn.execute("SELECT nazwa, ostatnio FROM bot_jobs WHERE nazwa != 'telegram_offset'"):
        try:
            moment = datetime.fromisoformat(ostatnio)
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is None:
            moment = moment.astimezone()  # stary zapis: czas lokalny serwera, na którym działał bot
        conn.execute("INSERT OR REPLACE INTO zadania (nazwa, ostatnio) VALUES (?, ?)", (nazwa, _iso(moment)))


def _trial_and_access(conn: sqlite3.Connection, from_version: int) -> None:
    """v9: 7-dniowy test (raz na osobę) i dostęp bez limitu dla użytkowników sprzed abonamentów.

    Okno dostępu (test albo abonament) zostaje w ``is_active`` + ``subscription_ends`` – kod z v6 nadal je
    rozumie. Bazy sprzed v6 nie znały abonamentów: każdy zaakceptowany (także ten, który zablokował bota)
    miał dostęp – zachowuje go bez terminu, aż admin świadomie przełączy go na nowy model (``/nowymodel``).
    """
    _run_script(conn, """
        ALTER TABLE bot_users ADD COLUMN dostep_bez_limitu INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE bot_users ADD COLUMN rodzaj_dostepu TEXT;
        ALTER TABLE bot_users ADD COLUMN test_dozwolony INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE bot_users ADD COLUMN test_start TEXT;
        ALTER TABLE bot_users ADD COLUMN test_koniec TEXT;
        ALTER TABLE bot_users ADD COLUMN przypomniano_koniec TEXT;
        ALTER TABLE bot_users ADD COLUMN zgloszono_koniec TEXT;
        UPDATE bot_users SET rodzaj_dostepu = 'platny' WHERE subscription_ends IS NOT NULL;
        UPDATE bot_users SET zgloszono_koniec = subscription_ends
            WHERE is_active = 0 AND subscription_ends IS NOT NULL;
    """)
    if from_version < 6:
        conn.execute("UPDATE bot_users SET dostep_bez_limitu = 1 WHERE status IN ('aktywny', 'zablokowany')")


_SETUP_STEP = """
ALTER TABLE bot_users ADD COLUMN konfiguracja TEXT;
UPDATE bot_users SET konfiguracja = 'gotowe';
"""
"""v10: krok pierwszej konfiguracji (branża → obszar → gotowe); dotychczasowi użytkownicy jej nie powtarzają."""


Migration = str | Callable[[sqlite3.Connection, int], None]

_MIGRATIONS: tuple[Migration, ...] = (
    _SCHEMA,                                              # v1: schemat bazowy
    "ALTER TABLE investments ADD COLUMN segment TEXT;",   # v2: segment klientów
    _BOT_SCHEMA,                                          # v3: scoring, numer leada, bot Telegram
    _CONTACT_COLUMNS,                                     # v4: telefon/e-mail z surowych pól GUNB
    _TRADE_COLUMN,                                        # v5: branża użytkownika („Kiedy dzwonić”)
    _SUBSCRIPTION_COLUMNS,                                # v6: abonament (paywall) – is_active, subscription_ends
    _independent_lead_flags,                              # v7: zapisany / przejrzany / ukryty niezależnie
    _jobs_in_utc,                                         # v8: zadania w UTC, kolejka wysyłek, blokady
    _trial_and_access,                                    # v9: 7-dniowy test, dostęp dotychczasowych
    _SETUP_STEP,                                          # v10: krok pierwszej konfiguracji
)
"""Kolejne migracje schematu; indeks + 1 = wersja zapisywana w ``PRAGMA user_version``.

Krok może być skryptem SQL albo funkcją ``(połączenie, wersja_startowa)`` – gdy konwersja danych
zależy od tego, z jakiej wersji baza jest podnoszona. Każdy krok wykonuje się w osobnej transakcji
razem z podbiciem wersji: przerwana migracja nie zostawia bazy w połowie.
"""
SCHEMA_VERSION = len(_MIGRATIONS)


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    """Wykonuje skrypt SQL instrukcja po instrukcji w bieżącej transakcji (``executescript`` by ją zatwierdził)."""
    statement = ""
    for piece in script.split(";"):
        statement += piece + ";"
        if sqlite3.complete_statement(statement):
            if statement.strip(" \t\r\n;"):
                conn.execute(statement)
            statement = ""

GEO_FIELDS: tuple[str, ...] = (
    "lat", "lon", "precyzja_geo", "google_maps_url", "geoportal_url", "powiat", "gmina", "teryt_dzialki",
)
"""Pola z geokodowania – brak nowej lokalizacji nie nadpisuje zapisanej."""

_BOOL_FIELDS = frozenset({"is_residential", "is_commercial", "is_noise", "czy_wyslano"})
_INVESTMENT_FIELDS = tuple(f.name for f in fields(Investment))


class ChangeType(str, enum.Enum):
    """Rodzaj zmiany wykrytej przy zapisie leada."""

    NEW = "nowy"
    STATUS_CHANGED = "zmiana_statusu"
    UPDATED = "aktualizacja"
    UNCHANGED = "bez_zmian"


@dataclass(frozen=True)
class UpsertResult:
    """Wynik :meth:`LeadRepository.upsert`."""

    id_sprawy: str
    change: ChangeType
    old_status: str | None
    new_status: str


@dataclass(frozen=True)
class StatusChange:
    """Wpis historii statusów (``stary_status=None`` oznacza pojawienie się sprawy)."""

    id_sprawy: str
    stary_status: str | None
    nowy_status: str
    zmieniono: str


class LeadRepository:
    """Repozytorium leadów w SQLite.

    Args:
        db_path: ścieżka do pliku bazy (katalog zostanie utworzony) lub ``":memory:"``.
        now: źródło bieżącego czasu (UTC) – wstrzykiwane w testach.
        negative_cache_days: czas życia negatywnych wpisów cache geokodowania.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        now: Callable[[], datetime] | None = None,
        negative_cache_days: int = 30,
    ) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        if str(db_path) != ":memory:":
            # WAL: czytelnicy nie blokują zapisu i odwrotnie (np. --sync-sheets w trakcie --fetch z innego
            # zadania harmonogramu); tryb zapisuje się w pliku bazy. Baza musi leżeć na dysku lokalnym.
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.negative_cache_days = negative_cache_days
        self._depth = 0
        self._migrate()

    # --- Cykl życia ---------------------------------------------------------------

    @property
    def connection(self) -> sqlite3.Connection:
        """Połączenie z bazą (dla modułów współdzielących plik, np. ``bot_store``)."""
        return self._conn

    def now(self) -> datetime:
        """Bieżący czas UTC (wstrzykiwany w testach)."""
        return self._now()

    @property
    def journal_mode(self) -> str:
        """Tryb dziennika SQLite (``wal`` dla plików, ``memory`` dla ``:memory:``)."""
        return self._conn.execute("PRAGMA journal_mode").fetchone()[0]

    @property
    def busy_timeout_ms(self) -> int:
        """Jak długo (ms) czekać na zwolnienie blokady przez inny proces."""
        return self._conn.execute("PRAGMA busy_timeout").fetchone()[0]

    def close(self) -> None:
        """Zamyka połączenie z bazą."""
        self._conn.close()

    def __enter__(self) -> LeadRepository:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Transakcja (zagnieżdżone wywołania dołączają do zewnętrznej)."""
        if self._depth == 0:
            self._conn.execute("BEGIN")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                self._conn.execute("ROLLBACK")
            raise
        self._depth -= 1
        if self._depth == 0:
            self._conn.execute("COMMIT")

    # --- Odczyt ----------------------------------------------------------------

    def get(self, id_sprawy: str) -> Investment | None:
        """Zwraca lead o danym numerze sprawy albo ``None``."""
        row = self._conn.execute("SELECT * FROM investments WHERE id_sprawy = ?", (id_sprawy,)).fetchone()
        return _to_investment(row) if row else None

    def get_by_nr(self, nr: int) -> Investment | None:
        """Lead o stabilnym numerze ``nr`` (używanym m.in. w przyciskach bota)."""
        row = self._conn.execute("SELECT * FROM investments WHERE nr = ?", (nr,)).fetchone()
        return _to_investment(row) if row else None

    def status_history(self, id_sprawy: str) -> list[StatusChange]:
        """Pełna historia statusów sprawy (od najstarszego wpisu)."""
        rows = self._conn.execute(
            "SELECT id_sprawy, stary_status, nowy_status, zmieniono FROM status_history "
            "WHERE id_sprawy = ? ORDER BY id",
            (id_sprawy,),
        ).fetchall()
        return [StatusChange(**dict(row)) for row in rows]

    def last_status_change(self, id_sprawy: str) -> StatusChange | None:
        """Ostatni wpis historii statusów (``stary_status=None`` = nowa sprawa)."""
        row = self._conn.execute(
            "SELECT id_sprawy, stary_status, nowy_status, zmieniono FROM status_history "
            "WHERE id_sprawy = ? ORDER BY id DESC LIMIT 1",
            (id_sprawy,),
        ).fetchone()
        return StatusChange(**dict(row)) if row else None

    # --- Zapis i wykrywanie zmian ----------------------------------------------------

    def upsert(self, investment: Investment, *, historical: bool = False) -> UpsertResult:
        """Zapisuje lead i klasyfikuje zmianę względem stanu w bazie (patrz opis modułu).

        Args:
            historical: import historyczny – nowa sprawa „pojawia się” z datą swojej decyzji, a nie teraz,
                więc nie trafia do powiadomień o nowościach (służy przypomnieniom „Kiedy dzwonić”).
        """
        now = self._now_iso()
        values = _content_values(investment)
        with self.transaction():
            row = self._conn.execute(
                "SELECT * FROM investments WHERE id_sprawy = ?", (investment.id_sprawy,)
            ).fetchone()
            if row is None:
                self._insert(values, now, appeared=_event_iso(investment, now) if historical else now)
                self._log_status(investment.id_sprawy, None, investment.status, now)
                return UpsertResult(investment.id_sprawy, ChangeType.NEW, None, investment.status)

            if values["lat"] is None and row["lat"] is not None:
                values.update({name: row[name] for name in GEO_FIELDS})
            old_status = row["status"]
            if old_status != investment.status:
                self._update(values, now, extra={
                    "status_zmieniony": now, "czy_wyslano": 0, "wyslano_kanaly": "",
                })
                self._log_status(investment.id_sprawy, old_status, investment.status, now)
                return UpsertResult(investment.id_sprawy, ChangeType.STATUS_CHANGED, old_status, investment.status)

            if any(row[name] != value for name, value in values.items()):
                self._update(values, now)
                return UpsertResult(investment.id_sprawy, ChangeType.UPDATED, old_status, investment.status)

            self._conn.execute(
                "UPDATE investments SET ostatnio_widziany = ? WHERE id_sprawy = ?", (now, investment.id_sprawy)
            )
            return UpsertResult(investment.id_sprawy, ChangeType.UNCHANGED, old_status, investment.status)

    def upsert_many(self, investments: Iterable[Investment], *, historical: bool = False) -> list[UpsertResult]:
        """Zapisuje wiele leadów w jednej transakcji (``historical`` – patrz :meth:`upsert`)."""
        with self.transaction():
            return [self.upsert(investment, historical=historical) for investment in investments]

    # --- Powiadomienia ----------------------------------------------------------------

    def pending_notifications(
        self, channel: str, *, limit: int, max_age_days: int | None = None
    ) -> list[Investment]:
        """Leady (poza szumem), których bieżący stan nie trafił jeszcze na kanał ``channel``.

        Args:
            channel: nazwa kanału, np. ``"telegram"``.
            limit: maksymalna liczba leadów.
            max_age_days: pomija leady, których status zmienił się dawniej niż N dni temu.
        """
        sql = "SELECT * FROM investments WHERE is_noise = 0 AND instr(',' || wyslano_kanaly || ',', ?) = 0"
        params: list[Any] = [f",{_check_channel(channel)},"]
        if max_age_days is not None:
            sql += " AND status_zmieniony >= ?"
            params.append(_iso(self._now() - timedelta(days=max_age_days)))
        sql += " ORDER BY status_zmieniony, data_aktualizacji, id_sprawy LIMIT ?"
        params.append(limit)
        return [_to_investment(row) for row in self._conn.execute(sql, params).fetchall()]

    def mark_sent(self, id_sprawy: str, channel: str) -> None:
        """Oznacza bieżący stan leada jako wysłany na kanał ``channel`` (ustawia ``czy_wyslano``)."""
        row = self._conn.execute(
            "SELECT wyslano_kanaly FROM investments WHERE id_sprawy = ?", (id_sprawy,)
        ).fetchone()
        if row is None:
            raise KeyError(f"Brak leada {id_sprawy!r}")
        channels = _merge_channels(row["wyslano_kanaly"], [_check_channel(channel)])
        self._conn.execute(
            "UPDATE investments SET czy_wyslano = 1, wyslano_kanaly = ? WHERE id_sprawy = ?", (channels, id_sprawy)
        )

    def mark_all_sent(self, channels: Sequence[str]) -> int:
        """Oznacza wszystkie oczekujące leady jako wysłane (np. przy pierwszym uruchomieniu).

        Returns:
            Liczbę leadów, którym zmieniono stan wysyłki.
        """
        wanted = [_check_channel(c) for c in channels]
        changed = 0
        with self.transaction():
            rows = self._conn.execute("SELECT id_sprawy, wyslano_kanaly FROM investments WHERE is_noise = 0").fetchall()
            for row in rows:
                merged = _merge_channels(row["wyslano_kanaly"], wanted)
                if merged != row["wyslano_kanaly"]:
                    self._conn.execute(
                        "UPDATE investments SET czy_wyslano = 1, wyslano_kanaly = ? WHERE id_sprawy = ?",
                        (merged, row["id_sprawy"]),
                    )
                    changed += 1
        return changed

    # --- Synchronizacja arkusza ----------------------------------------------------------

    def pending_sheet_sync(self, limit: int | None = None) -> list[Investment]:
        """Leady nowe lub zmienione od ostatniej synchronizacji z arkuszem."""
        sql = "SELECT * FROM investments WHERE zsynchronizowano IS NULL OR zsynchronizowano < zmieniono ORDER BY zmieniono"
        params: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        return [_to_investment(row) for row in self._conn.execute(sql, params).fetchall()]

    def mark_synced(self, ids: Iterable[str]) -> None:
        """Zapisuje moment synchronizacji wskazanych leadów z arkuszem."""
        now = self._now_iso()
        with self.transaction():
            self._conn.executemany(
                "UPDATE investments SET zsynchronizowano = ? WHERE id_sprawy = ?", [(now, i) for i in ids]
            )

    # --- Cache geokodowania i statystyki -------------------------------------------------

    def geocode_cache(self) -> SqliteGeocodeCache:
        """Cache geokodowania w tej samej bazie (spełnia ``geocoding_uldk.GeocodeCache``)."""
        return SqliteGeocodeCache(self._conn, self._now, self.negative_cache_days)

    def stats(self) -> dict[str, Any]:
        """Podsumowanie zawartości bazy."""
        def grouped(column: str) -> dict[str, int]:
            rows = self._conn.execute(
                f"SELECT {column} AS k, COUNT(*) AS n FROM investments GROUP BY {column} ORDER BY n DESC"
            ).fetchall()
            return {row["k"]: row["n"] for row in rows}

        def count(where: str) -> int:
            return self._conn.execute(f"SELECT COUNT(*) FROM investments WHERE {where}").fetchone()[0]

        return {
            "razem": count("1 = 1"),
            "statusy": grouped("status"),
            "kategorie": grouped("kategoria"),
            "segmenty": grouped("coalesce(segment, 'bez segmentu')"),
            "priorytety": grouped("coalesce(priorytet, 'brak')"),
            "zrodla": grouped("zrodlo"),
            "niewyslane": count("czy_wyslano = 0 AND is_noise = 0"),
            "do_arkusza": count("zsynchronizowano IS NULL OR zsynchronizowano < zmieniono"),
            "z_lokalizacja": count("lat IS NOT NULL"),
            "zmiany_statusu": self._conn.execute(
                "SELECT COUNT(*) FROM status_history WHERE stary_status IS NOT NULL"
            ).fetchone()[0],
        }

    # --- Wewnętrzne -------------------------------------------------------------------

    def _migrate(self) -> None:
        """Podnosi schemat krok po kroku; każdy krok w transakcji ``IMMEDIATE`` razem z numerem wersji.

        Dwa procesy startujące naraz (bot i ``--fetch`` z harmonogramu) nie wykonają kroku dwa razy:
        drugi czeka na blokadę zapisu i po niej widzi już nową wersję.
        """
        start = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if start >= len(_MIGRATIONS):
            return  # schemat aktualny – bez blokady zapisu (inny proces może właśnie importować)
        while True:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
                if version < len(_MIGRATIONS):
                    step = _MIGRATIONS[version]
                    if callable(step):
                        step(self._conn, start)
                    else:
                        _run_script(self._conn, step)
                    self._conn.execute(f"PRAGMA user_version = {version + 1}")
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            if version + 1 >= len(_MIGRATIONS):
                return

    # --- Blokady (np. jeden import naraz) ---------------------------------------------

    def acquire_lease(self, name: str, owner: str, ttl: timedelta) -> bool:
        """Zajmuje blokadę ``name`` na ``ttl``; ``False``, gdy trzyma ją ktoś inny i jeszcze nie wygasła.

        Blokada w bazie działa między procesami (bot, ``--fetch`` z harmonogramu systemu, import historii),
        a po awarii procesu wygasa sama. Właściciel może ją odnowić tym samym wywołaniem.
        """
        now = self.now()
        cursor = self._conn.execute(
            "INSERT INTO blokady (nazwa, wlasciciel, wygasa) VALUES (?, ?, ?)"
            " ON CONFLICT (nazwa) DO UPDATE SET wlasciciel = excluded.wlasciciel, wygasa = excluded.wygasa"
            " WHERE blokady.wygasa <= ? OR blokady.wlasciciel = excluded.wlasciciel",
            (name, owner, _iso(now + ttl), _iso(now)),
        )
        return cursor.rowcount == 1

    def renew_lease(self, name: str, owner: str, ttl: timedelta) -> bool:
        """Przedłuża własną blokadę; ``False``, gdy już jej nie mamy (wygasła i przejął ją ktoś inny)."""
        cursor = self._conn.execute(
            "UPDATE blokady SET wygasa = ? WHERE nazwa = ? AND wlasciciel = ?", (_iso(self.now() + ttl), name, owner)
        )
        return cursor.rowcount == 1

    def release_lease(self, name: str, owner: str) -> None:
        self._conn.execute("DELETE FROM blokady WHERE nazwa = ? AND wlasciciel = ?", (name, owner))

    def lease_holder(self, name: str) -> str | None:
        """Kto trzyma ważną blokadę ``name`` (``None`` – wolna)."""
        row = self._conn.execute(
            "SELECT wlasciciel FROM blokady WHERE nazwa = ? AND wygasa > ?", (name, _iso(self.now()))
        ).fetchone()
        return row[0] if row else None

    def _insert(self, values: dict[str, Any], now: str, *, appeared: str) -> None:
        next_nr = self._conn.execute("SELECT coalesce(max(nr), 0) + 1 FROM investments").fetchone()[0]
        record = {
            **values,
            "nr": next_nr,
            "czy_wyslano": 0,
            "wyslano_kanaly": "",
            "utworzono": now,
            "zmieniono": now,
            "status_zmieniony": appeared,
            "zsynchronizowano": None,
            "ostatnio_widziany": now,
        }
        columns = ", ".join(record)
        placeholders = ", ".join("?" for _ in record)
        self._conn.execute(f"INSERT INTO investments ({columns}) VALUES ({placeholders})", list(record.values()))

    def _update(self, values: dict[str, Any], now: str, extra: dict[str, Any] | None = None) -> None:
        record = {**values, "zmieniono": now, "ostatnio_widziany": now, **(extra or {})}
        record.pop("id_sprawy")
        assignments = ", ".join(f"{column} = ?" for column in record)
        self._conn.execute(
            f"UPDATE investments SET {assignments} WHERE id_sprawy = ?", [*record.values(), values["id_sprawy"]]
        )

    def _log_status(self, id_sprawy: str, old: str | None, new: str, now: str) -> None:
        self._conn.execute(
            "INSERT INTO status_history (id_sprawy, stary_status, nowy_status, zmieniono) VALUES (?, ?, ?, ?)",
            (id_sprawy, old, new, now),
        )

    def _now_iso(self) -> str:
        return _iso(self._now())


class SqliteGeocodeCache:
    """Cache geokodowania w tabeli ``geocode_cache``; wpisy negatywne wygasają po ``negative_ttl_days``."""

    def __init__(self, conn: sqlite3.Connection, now: Callable[[], datetime], negative_ttl_days: int) -> None:
        self._conn = conn
        self._now = now
        self._negative_ttl = timedelta(days=negative_ttl_days)

    def get(self, key: str) -> CachedGeocode | None:
        row = self._conn.execute("SELECT wynik, zapisano FROM geocode_cache WHERE klucz = ?", (key,)).fetchone()
        if row is None:
            return None
        if row["wynik"] is None:
            if self._now() - datetime.fromisoformat(row["zapisano"]) > self._negative_ttl:
                return None
            return CachedGeocode(None)
        return CachedGeocode(GeocodeResult.from_dict(json.loads(row["wynik"])))

    def set(self, key: str, result: GeocodeResult | None) -> None:
        payload = json.dumps(result.to_dict(), ensure_ascii=False) if result is not None else None
        self._conn.execute(
            "INSERT OR REPLACE INTO geocode_cache (klucz, wynik, zapisano) VALUES (?, ?, ?)",
            (key, payload, _iso(self._now())),
        )


def _content_values(investment: Investment) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name in CONTENT_FIELDS:
        value = getattr(investment, name)
        if name == "dzialki":
            value = json.dumps(list(value), ensure_ascii=False)
        elif name in _BOOL_FIELDS:
            value = int(bool(value))
        values[name] = value
    return values


def investment_from_row(row: sqlite3.Row) -> Investment:
    """Buduje :class:`Investment` z wiersza tabeli ``investments`` (także dla ``bot_store``)."""
    return _to_investment(row)


def _to_investment(row: sqlite3.Row) -> Investment:
    data = {name: row[name] for name in _INVESTMENT_FIELDS}
    data["dzialki"] = json.loads(data["dzialki"] or "[]")
    for name in _BOOL_FIELDS:
        data[name] = bool(data[name])
    return Investment(**data)


def _merge_channels(current: str, add: Sequence[str]) -> str:
    channels = [c for c in current.split(",") if c]
    channels.extend(c for c in add if c not in channels)
    return ",".join(channels)


def _check_channel(channel: str) -> str:
    if not channel or "," in channel:
        raise ValueError(f"Niepoprawna nazwa kanału: {channel!r}")
    return channel


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _event_iso(investment: Investment, now: str) -> str:
    """Moment „pojawienia się” sprawy z importu historycznego: data zdarzenia (decyzji), nie później niż teraz."""
    day = (investment.data_aktualizacji or "")[:10]
    try:
        moment = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    except ValueError:
        return now
    return min(_iso(moment), now)
