"""Higiena bazy SQLite: cotygodniowa kopia przed pobieraniem danych i ``VACUUM`` po dużym imporcie.

Kopia powstaje przez API kopii zapasowych SQLite (``Connection.backup``), a nie przez skopiowanie
pliku – w trybie WAL część danych bywa jeszcze w pliku ``-wal``, więc zwykła kopia pliku mogłaby
być niespójna. Nazwa kopii zawiera datę (``gunb_leads-2026-09-29.sqlite``); zostaje ``keep`` najnowszych.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .storage import LeadRepository

log = logging.getLogger(__name__)

_DATED_NAME = re.compile(r"-(\d{4}-\d{2}-\d{2})\.sqlite$")


def weekly_backup(
    repo: LeadRepository,
    backup_dir: Path,
    *,
    today: date,
    every_days: int = 7,
    keep: int = 8,
) -> Path | None:
    """Robi kopię bazy, jeśli ostatnia jest starsza niż ``every_days`` dni.

    Returns:
        Ścieżkę nowej kopii albo ``None`` (kopia niepotrzebna, baza w pamięci albo błąd – zalogowany,
        bo kopia nie może zatrzymać pobierania danych).
    """
    db_file = repo.connection.execute("PRAGMA database_list").fetchone()["file"]
    if not db_file:
        return None  # baza :memory:
    stem = Path(db_file).stem
    try:
        existing = _dated_backups(backup_dir, stem)
        if existing and today - existing[-1][0] < timedelta(days=every_days):
            return None
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"{stem}-{today.isoformat()}.sqlite"
        partial = target.with_name(target.name + ".part")
        destination = sqlite3.connect(partial)
        try:
            repo.connection.backup(destination)
        finally:
            destination.close()
        os.replace(partial, target)
        for _, old in _dated_backups(backup_dir, stem)[:-keep]:
            old.unlink()
    except (OSError, sqlite3.Error) as exc:
        log.error("Kopia bazy do %s nie powiodła się: %s", backup_dir, exc)
        return None
    log.info("Kopia bazy: %s (%.1f MB)", target, target.stat().st_size / 1e6)
    return target


def vacuum_after_import(repo: LeadRepository, *, changed: int, threshold: int = 500) -> bool:
    """Po dużym imporcie (``changed >= threshold`` nowych/zmienionych leadów) kompaktuje plik bazy.

    Returns:
        ``True``, gdy wykonano ``VACUUM``.
    """
    if changed < threshold:
        return False
    conn = repo.connection
    before = _size_mb(conn)
    try:
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # VACUUM w trybie WAL zostawia duży plik -wal
    except sqlite3.OperationalError as exc:
        log.warning("VACUUM pominięty (%s) – spróbuję po następnym dużym imporcie", exc)
        return False
    log.info("VACUUM po imporcie %d leadów: %.1f MB → %.1f MB", changed, before, _size_mb(conn))
    return True


def _dated_backups(backup_dir: Path, stem: str) -> list[tuple[date, Path]]:
    """Kopie danej bazy posortowane od najstarszej (data z nazwy pliku)."""
    if not backup_dir.is_dir():
        if backup_dir.exists():
            raise NotADirectoryError(f"{backup_dir} nie jest katalogiem")
        return []
    found = []
    for path in backup_dir.glob(f"{stem}-*.sqlite"):
        match = _DATED_NAME.search(path.name)
        if match:
            found.append((date.fromisoformat(match.group(1)), path))
    return sorted(found)


def _size_mb(conn: sqlite3.Connection) -> float:
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    return pages * page_size / 1e6
