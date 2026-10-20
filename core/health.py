"""Kontrola zdrowia silnika (``python main.py --zdrowie``) – tylko do odczytu.

Sprawdza: czy baza SQLite jest spójna i zgodna ze schematem, kiedy był ostatni pełny import GUNB,
czy import aktualnie trwa, czy jest wolne miejsce na dysku oraz stan kolejki powiadomień/e-mail.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from .config import AppConfig
from .pipeline import IMPORT_LEASE
from .storage import SCHEMA_VERSION

OK, WARNING, CRITICAL = 0, 1, 2
IMPORT_WARN, IMPORT_CRITICAL = timedelta(hours=30), timedelta(hours=72)
SEND_STUCK = timedelta(minutes=30)
DISK_CRITICAL_BYTES, DISK_WARN_BYTES, DISK_WARN_RATIO = 200 * 2**20, 2 * 2**30, 0.10
_ICONS = {OK: "✔", WARNING: "⚠", CRITICAL: "✘"}

IMPORT_JOB = "import_gunb"
LAST_IMPORT_JOB = "ostatni_import_gunb"


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
            checks += _state(conn, moment)
            if config.email.enabled:
                checks += _email_state(conn, moment)
    except sqlite3.DatabaseError as exc:
        checks.append(Check(CRITICAL, f"baza uszkodzona albo niedostępna: {exc}"))
    finally:
        conn.close()

    return max(c.level for c in checks), checks


def ping(url: str, level: int, *, timeout: float = 10.0) -> None:
    """Zgłasza wynik do zewnętrznego monitora (healthchecks.io i podobne)."""
    target = url.rstrip("/") + "/fail" if level >= CRITICAL else url
    try:
        requests.get(target, timeout=timeout)
    except requests.RequestException:
        pass


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


def _state(conn: sqlite3.Connection, now: datetime) -> list[Check]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "zadania" not in tables:
        return []

    def job_time(name: str) -> datetime | None:
        row = conn.execute("SELECT ostatnio FROM zadania WHERE nazwa = ?", (name,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    checks: list[Check] = []
    if _import_running(conn, now):
        checks.append(Check(OK, "import GUNB jest aktualnie w toku"))

    last = job_time(LAST_IMPORT_JOB)
    status = conn.execute("SELECT stan, opis FROM zadania WHERE nazwa = ?", (IMPORT_JOB,)).fetchone()
    if last is None:
        checks.append(Check(WARNING, "jeszcze nie było pełnego importu GUNB"))
    else:
        level = CRITICAL if now - last > IMPORT_CRITICAL else WARNING if now - last > IMPORT_WARN else OK
        checks.append(Check(level, f"ostatni pełny import GUNB {_age(now, last)}"))
    if status and status[0] == "blad":
        checks.append(Check(WARNING, f"ostatni import nieudany: {status[1] or '?'}"))

    return checks


def _email_state(conn: sqlite3.Connection, now: datetime) -> list[Check]:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {'zadania', 'notification_outbox', 'notification_endpoints'} <= tables:
        return [Check(WARNING, 'e-mail: kontrola dostępna po aktualizacji schematu')]
    checks = []

    row = conn.execute(
        "SELECT SUM(CASE WHEN o.state IN ('unknown', 'failed') THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN (o.state IN ('queued', 'retry') AND o.next_attempt_at < ?)"
        " OR (o.state = 'sending' AND o.claimed_at < ?) THEN 1 ELSE 0 END)"
        " FROM notification_outbox o JOIN notification_endpoints e ON e.id = o.endpoint_id WHERE e.channel = 'email'",
        (_iso(now - SEND_STUCK), _iso(now - timedelta(seconds=120)))).fetchone()
    unresolved, stuck = row[0] or 0, row[1] or 0
    if unresolved:
        checks.append(Check(WARNING, f'e-mail: niepewne lub nieudane wysyłki wymagają sprawdzenia: {unresolved}'))
    if stuck:
        checks.append(Check(WARNING, f'e-mail: zalegające wysyłki: {stuck}'))
    if not unresolved and not stuck:
        checks.append(Check(OK, 'e-mail: kolejka bez zaległości i błędów'))
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
