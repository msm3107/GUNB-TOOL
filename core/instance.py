"""Jedna instancja bota na jednych danych: blokada pliku obok bazy (``gunb-bot.lock``).

Blokadę trzyma system operacyjny – znika razem z procesem, także po ``kill -9`` czy awarii zasilania,
więc nie trzeba jej ręcznie „czyścić”. Linux: ``flock``; Windows: ``msvcrt.locking`` na bajcie daleko za
początkiem pliku (blokady Windows są obowiązkowe – PID zapisany na początku zostaje czytelny).

Blokada chroni przed drugim procesem na tym samym serwerze. Drugi bot z tym samym tokenem na innej
maszynie wykrywa ``LeadBot`` po konflikcie 409 z Telegrama.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

_WINDOWS_LOCK_OFFSET = 1 << 20
LOCK_NAME = "gunb-bot.lock"


class AlreadyRunning(RuntimeError):
    """Inny proces bota pracuje już na tych danych."""


def bot_lock_path(db_path: str | Path) -> Path:
    """Blokada bota – obok bazy, więc dotyczy tych samych danych (bot, eksport, kontrola zdrowia)."""
    return Path(db_path).parent / LOCK_NAME


@contextmanager
def instance_lock(path: str | Path) -> Iterator[None]:
    """Trzyma blokadę przez cały blok; drugi chętny dostaje :class:`AlreadyRunning` od razu (bez czekania)."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        _lock(handle)
    except OSError:
        handle.close()
        holder = holder_pid(lock_path)
        raise AlreadyRunning(f"na tych danych działa już inny proces bota (PID {holder or '?'}, blokada {lock_path})")
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        yield
    finally:
        try:
            _unlock(handle)
        finally:
            handle.close()


def lock_is_free(path: str | Path) -> bool:
    """Czy nikt nie trzyma blokady (np. przed importem danych migracji albo w kontroli zdrowia)."""
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
