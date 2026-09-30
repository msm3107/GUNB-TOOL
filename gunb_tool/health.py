"""Kontrola zdrowia bota (``python main.py --zdrowie``) – tylko do odczytu, bez migracji i bez Telegrama.

Rozróżnia: czy działa proces (blokada instancji), czy żyją pętle (bicie serca odbierania wiadomości i wątku
zadań), kiedy był ostatni pełny import GUNB, czy wysyłki zalegają albo się nie udają, czy baza jest cała
i zgodna z kodem, czy jest miejsce na dysku i czy tym samym tokenem nie odbiera inny proces.

Kod wyjścia: 0 – w porządku, 1 – ostrzeżenie, 2 – awaria. Kontrola niczego nie restartuje – awaria GUNB
czy Telegrama nie może wywołać pętli restartów. Do alarmu, gdy proces całkiem stanie, służy zewnętrzny
„dead man's switch” (np. healthchecks.io): ``--ping URL`` zgłasza wynik, a brak zgłoszeń budzi alarm.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .bot import CONFLICT_JOB, HEARTBEAT_JOB, IMPORT_JOB, LAST_IMPORT_JOB, RECEIVE_HEARTBEAT_JOB
from .config import AppConfig
from .instance import bot_lock_path, holder_pid, lock_is_free
from .pipeline import IMPORT_LEASE
from .storage import SCHEMA_VERSION

OK, WARNING, CRITICAL = 0, 1, 2
LOOP_STALE = timedelta(minutes=5)
IMPORT_WARN, IMPORT_CRITICAL = timedelta(hours=30), timedelta(hours=72)
SEND_STUCK = timedelta(minutes=30)
CONFLICT_RECENT = timedelta(minutes=15)
DISK_CRITICAL_BYTES, DISK_WARN_BYTES, DISK_WARN_RATIO = 200 * 2**20, 2 * 2**30, 0.10
_ICONS = {OK: "✔", WARNING: "⚠", CRITICAL: "✘"}


@dataclass(frozen=True)
class Check:
    level: int
    text: str

    def __str__(self) -> str:
        return f"{_ICONS[self.level]} {self.text}"


def check_health(config: AppConfig, *, now: datetime | None = None) -> tuple[int, list[Check]]:
    """Zwraca poziom całości (najgorszy z kontroli) i listę kontroli do wypisania."""
    moment = now or datetime.now(timezone.utc)
    db_path = config.storage.db_path
    checks: list[Check] = []
    lock = bot_lock_path(db_path)
    running = not lock_is_free(lock)
    checks.append(Check(OK, f"proces bota działa (PID {holder_pid(lock) or '?'})") if running
                  else Check(CRITICAL, "proces bota nie działa (brak blokady instancji)"))
    checks += _disk(db_path.parent)
    if not db_path.is_file():
        checks.append(Check(CRITICAL, f"brak bazy {db_path}"))
        return max(c.level for c in checks), checks
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
    except sqlite3.DatabaseError as exc:
        checks.append(Check(CRITICAL, f"baza niedostępna: {exc}"))
        return max(c.level for c in checks), checks
    try:
        conn.execute("PRAGMA query_only = ON")
        checks += _database(conn)
        if all(c.level < CRITICAL or not c.text.startswith("baza") for c in checks):
            checks += _state(conn, moment, running=running)
    except sqlite3.DatabaseError as exc:
        checks.append(Check(CRITICAL, f"baza uszkodzona albo niedostępna: {exc}"))
    finally:
        conn.close()
    return max(c.level for c in checks), checks


def ping(url: str, level: int, *, timeout: float = 10.0) -> None:
    """Zgłasza wynik do zewnętrznego monitora (healthchecks.io i podobne): awaria idzie na ``<url>/fail``."""
    target = url.rstrip("/") + "/fail" if level >= CRITICAL else url
    try:
        requests.get(target, timeout=timeout)
    except requests.RequestException:
        pass  # brak zgłoszenia i tak uruchomi alarm po stronie monitora


def _database(conn: sqlite3.Connection) -> list[Check]:
    result = conn.execute("PRAGMA quick_check").fetchone()[0]
    if result != "ok":
        return [Check(CRITICAL, f"baza uszkodzona (quick_check: {result})")]
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > SCHEMA_VERSION:
        return [Check(CRITICAL, f"baza ma schemat v{version}, a kod zna tylko v{SCHEMA_VERSION} – kod jest za stary")]
    if version < SCHEMA_VERSION:
        return [Check(WARNING, f"schemat bazy v{version} → v{SCHEMA_VERSION} przy najbliższym starcie (z kopią)")]
    return [Check(OK, f"baza cała, schemat v{version}")]


def _state(conn: sqlite3.Connection, now: datetime, *, running: bool) -> list[Check]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "zadania" not in tables:
        return []  # stary schemat – te dane pojawią się po migracji

    def job_time(name: str) -> datetime | None:
        row = conn.execute("SELECT ostatnio FROM zadania WHERE nazwa = ?", (name,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    checks: list[Check] = []
    importing = _import_running(conn, now)
    if running:
        loop = job_time(RECEIVE_HEARTBEAT_JOB)
        if loop is None or now - loop > LOOP_STALE:
            checks.append(Check(CRITICAL, f"brak udanego odbierania wiadomości z Telegrama ({_age(now, loop)}) – "
                                          "pętla zawieszona, brak sieci albo nieważny token; sprawdź logi"))
        else:
            checks.append(Check(OK, f"odbieranie wiadomości z Telegrama działa ({_age(now, loop)})"))
        worker = job_time(HEARTBEAT_JOB)
        if importing:
            checks.append(Check(OK, "wątek zadań: import trwa"))
        elif worker is None or now - worker > LOOP_STALE:
            checks.append(Check(CRITICAL, f"wątek zadań nie odpowiada ({_age(now, worker)})"))
        else:
            checks.append(Check(OK, f"wątek zadań żyje ({_age(now, worker)})"))
    conflict = job_time(CONFLICT_JOB)
    if conflict is not None and now - conflict < CONFLICT_RECENT:
        checks.append(Check(CRITICAL, "inny proces odbiera wiadomości tym samym tokenem – wysyłki wstrzymane"))
    last = job_time(LAST_IMPORT_JOB)
    status = conn.execute("SELECT stan, opis FROM zadania WHERE nazwa = ?", (IMPORT_JOB,)).fetchone()
    if last is None:
        checks.append(Check(WARNING, "jeszcze nie było pełnego importu GUNB"))
    else:
        level = CRITICAL if now - last > IMPORT_CRITICAL else WARNING if now - last > IMPORT_WARN else OK
        checks.append(Check(level, f"ostatni pełny import GUNB {_age(now, last)}"))
    if status and status[0] == "blad":
        checks.append(Check(WARNING, f"ostatni import nieudany: {status[1] or '?'}"))
    if "wysylki" in tables:
        stuck = conn.execute("SELECT COUNT(*) FROM wysylki WHERE stan IN ('oczekuje', 'wysylanie') AND nastepna_proba < ?",
                             (_iso(now - SEND_STUCK),)).fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM wysylki WHERE stan = 'blad' AND zmieniono >= ?",
                              (_iso(now - timedelta(hours=24)),)).fetchone()[0]
        if stuck:
            checks.append(Check(WARNING, f"zalegające wysyłki: {stuck}"))
        if failed:
            checks.append(Check(WARNING, f"nieudane wysyłki z ostatniej doby: {failed}"))
        if not stuck and not failed:
            checks.append(Check(OK, "wysyłki bez zaległości i błędów"))
    return checks


def _import_running(conn: sqlite3.Connection, now: datetime) -> bool:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "blokady" not in tables:
        return False
    query = "SELECT 1 FROM blokady WHERE nazwa = ? AND wygasa > ?"
    return conn.execute(query, (IMPORT_LEASE, _iso(now))).fetchone() is not None


def _disk(directory: Path) -> list[Check]:
    target = directory if directory.exists() else directory.parent
    usage = shutil.disk_usage(target)
    free_text = f"{usage.free / 2**30:.1f} GB wolne"
    if usage.free < DISK_CRITICAL_BYTES:
        return [Check(CRITICAL, f"prawie pełny dysk ({free_text})")]
    if usage.free < DISK_WARN_BYTES or (usage.total and usage.free / usage.total < DISK_WARN_RATIO):
        return [Check(WARNING, f"mało miejsca na dysku ({free_text})")]
    return [Check(OK, f"dysk: {free_text}")]


def _age(now: datetime, moment: datetime | None) -> str:
    if moment is None:
        return "brak danych"
    seconds = int((now - moment).total_seconds())
    if seconds < 120:
        return f"{seconds} s temu"
    if seconds < 7200:
        return f"{seconds // 60} min temu"
    return f"{seconds // 3600} h temu"


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")
