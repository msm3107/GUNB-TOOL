"""Blokady procesów pracujących na jednej bazie SQLite.

Blokadę trzyma system operacyjny i zwalnia ją po zakończeniu procesu, także po awarii. Linux używa
``flock``, a Windows ``msvcrt.locking``. ``gunb-bot.lock`` pozostaje jako blokada zgodności ze starszymi
wydaniami, które korzystały z tej nazwy.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

_WINDOWS_LOCK_OFFSET = 1 << 20
ENGINE_LOCK_NAME = "gunb-engine.lock"
MAINTENANCE_LOCK_NAME = "gunb-maintenance.lock"
LEGACY_BOT_LOCK_NAME = "gunb-bot.lock"


class AlreadyRunning(RuntimeError):
    """Inny proces korzysta już z tych danych."""


def engine_lock_path(db_path: str | Path) -> Path:
    """Blokada wszystkich procesów silnika korzystających z tej bazy."""
    return Path(db_path).parent / ENGINE_LOCK_NAME


def maintenance_lock_path(db_path: str | Path) -> Path:
    """Blokada wzajemna dla eksportu, kopii, importu i odtwarzania bazy."""
    return Path(db_path).parent / MAINTENANCE_LOCK_NAME


def bot_lock_path(db_path: str | Path) -> Path:
    """Ścieżka blokady używanej przez starsze wydania aplikacji."""
    return Path(db_path).parent / LEGACY_BOT_LOCK_NAME


@contextmanager
def engine_process_lock(db_path: str | Path) -> Iterator[None]:
    """Chroni operacje silnika i zachowuje wyłączność ze starszymi wydaniami."""
    with instance_lock(engine_lock_path(db_path)), instance_lock(bot_lock_path(db_path)):
        yield


@contextmanager
def instance_lock(path: str | Path) -> Iterator[None]:
    """Trzyma blokadę przez cały blok; drugi proces dostaje :class:`AlreadyRunning` bez czekania."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        try:
            _lock(handle)
        except OSError:
            holder = holder_pid(lock_path)
            raise AlreadyRunning(
                f"na tych danych działa już inny proces (PID {holder or '?'}, blokada {lock_path})"
            ) from None
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            yield
        finally:
            _unlock(handle)


def lock_is_free(path: str | Path) -> bool:
    """Czy nikt nie trzyma blokady."""
    try:
        with instance_lock(path):
            return True
    except AlreadyRunning:
        return False


def holder_pid(path: str | Path) -> int | None:
    """PID zapisany przez proces trzymający blokadę (``None`` – nie da się odczytać)."""
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[str]) -> None:
        handle.seek(_WINDOWS_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[str]) -> None:
        handle.seek(_WINDOWS_LOCK_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[str]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
