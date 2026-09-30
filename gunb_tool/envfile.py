"""Zmiana wybranych zmiennych w pliku ``.env`` (sekrety na serwerze) bez ruszania pozostałych linii.

Z tego korzystają ``gunb-admin sekrety`` (token bota, ID administratora) i ``gunb-admin monitor``.
Wartości przychodzą w zmiennych środowiskowych ``GUNB_SET_<NAZWA>``, a nie w argumentach programu,
które widać na liście procesów (``ps``). Plik ma prawa 600 i jest podmieniany w jednym kroku.

Użycie::

    GUNB_SET_TELEGRAM_BOT_TOKEN=... python -m gunb_tool.envfile ustaw /var/lib/gunb-tool/.env
    python -m gunb_tool.envfile jest /var/lib/gunb-tool/.env TELEGRAM_BOT_TOKEN   # kod 0 = ustawiona
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping, Sequence

from dotenv import dotenv_values

PREFIX = "GUNB_SET_"
_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PLAIN = re.compile(r"^[A-Za-z0-9_:/.@+=,-]*$")


def set_values(path: str | Path, updates: Mapping[str, str]) -> None:
    """Ustawia ``updates`` (podmienia istniejące linie, brakujące dopisuje); reszta pliku zostaje."""
    for name, value in updates.items():
        if not _NAME.match(name):
            raise ValueError(f"niepoprawna nazwa zmiennej: {name!r}")
        if any(char in value for char in "\r\n'\"\\"):
            raise ValueError(f"{name}: wartość nie może zawierać nowej linii, cudzysłowu ani ukośnika wstecznego")
    target = Path(path)
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    result: list[str] = []
    written: set[str] = set()
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if name in updates:
            if name not in written:
                result.append(_line(name, updates[name]))
                written.add(name)
            continue
        result.append(line)
    result.extend(_line(name, value) for name, value in updates.items() if name not in written)
    partial = target.with_name(target.name + ".nowy")
    handle = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(result) + "\n")
    os.chmod(partial, 0o600)  # plik mógł już istnieć z innymi prawami
    os.replace(partial, target)


def is_set(path: str | Path, name: str) -> bool:
    """Czy zmienna ma niepustą wartość (bez ujawniania jej)."""
    target = Path(path)
    return target.is_file() and bool(dotenv_values(target).get(name))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "ustaw":
        updates = {key[len(PREFIX):]: value for key, value in os.environ.items() if key.startswith(PREFIX)}
        try:
            set_values(args[1], updates)
        except (OSError, ValueError) as exc:
            print(f"Błąd: {exc}", file=sys.stderr)
            return 1
        print(f"Zapisano w {args[1]}: {', '.join(sorted(updates)) or 'nic'}")
        return 0
    if len(args) == 3 and args[0] == "jest":
        return 0 if is_set(args[1], args[2]) else 1
    print("Użycie: python -m gunb_tool.envfile ustaw PLIK  (wartości w GUNB_SET_<NAZWA>)\n"
          "        python -m gunb_tool.envfile jest PLIK NAZWA", file=sys.stderr)
    return 2


def _line(name: str, value: str) -> str:
    return f"{name}={value}" if _PLAIN.match(value) else f"{name}='{value}'"


if __name__ == "__main__":
    sys.exit(main())
