"""Przeniesienie stanu aplikacji na inny komputer lub serwer: eksport → sprawdzenie → import; kopia i odtworzenie.

Cały trwały stan bota jest w bazie SQLite (``storage.db_path``): inwestycje i historia statusów, użytkownicy
z filtrami i pinezką bazy, test/abonament/dostęp, zapisane, notatki, obserwowane, przypomnienia, doręczenia,
kolejka wysyłek, harmonogram i offset Telegrama. Obok są ``config.yaml`` i ``.env``.

Pakiet (ZIP):

* ``baza/<plik>.sqlite`` – spójna kopia bazy (API kopii SQLite – także dane jeszcze w pliku ``-wal``),
* ``konfiguracja/config.yaml`` – konfiguracja (sekrety są w niej tylko jako odwołania ``${...}``),
* ``konfiguracja/env.bez-sekretow`` – zmienne z ``.env``: identyfikatory czatów i kontakt zostają,
  token bota, webhooki i klucze są puste – ustawia się je osobno na serwerze,
* ``manifest.json`` – wersja formatu, aplikacji i schematu, czas utworzenia, sumy SHA-256, liczności tabel.

Nie ma w nim cache paczek GUNB (bot pobierze je ponownie), logów, starych kopii ani klucza Google.
Pakiet zawiera dane użytkowników – przechowuj go jak kopię bazy i nie dodawaj do repozytorium.

Użycie (``python -m gunb_tool.migration …``)::

    eksport  --config config.yaml [--do KATALOG] [--na-zywo]
    sprawdz  PAKIET
    importuj PAKIET --config /var/lib/gunb-tool/config.yaml [--zastap] [--konfiguracja-z-pakietu]
    kopia    --config config.yaml
    odtworz  KOPIA.sqlite --config config.yaml

Import i odtworzenie wymagają zatrzymanego bota i braku innych procesów zapisujących; nic nie jest
nadpisywane po cichu – istniejąca baza najpierw trafia do kopii.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .config import AppConfig, ConfigError, load_config
from .instance import bot_lock_path, lock_is_free
from .storage import SCHEMA_VERSION

FORMAT = "gunb-tool-migracja"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
DB_DIR = "baza"
CONFIG_ENTRY = "konfiguracja/config.yaml"
ENV_ENTRY = "konfiguracja/env.bez-sekretow"
MAX_PACKAGE_BYTES = 50 * 1024 ** 3
SERVICE = os.environ.get("GUNB_SERVICE", "gunb-bot")

ENV_KEEP = frozenset({
    "TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID_DOMKI", "TELEGRAM_CHAT_ID_DUZE", "TELEGRAM_ADMIN_CHAT_ID",
    "ADMIN_CHAT_ID", "ADMIN_CONTACT", "GOOGLE_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_FILE",
})
"""Zmienne ``.env`` przenoszone z wartością; każda inna trafia do pakietu pusta (jak sekret)."""
_SECRET_VALUES = (
    re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{30,}\b"),  # token bota Telegram
    re.compile(r"(?:discord(?:app)?\.com)/api/webhooks/\S+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


class MigrationError(RuntimeError):
    """Pakiet, baza albo stan systemu nie pozwalają bezpiecznie wykonać operacji (opis dla człowieka)."""


@dataclass(frozen=True)
class ImportResult:
    database: Path
    replaced_backup: Path | None
    schema_version: int
    counts: dict[str, int]


# === Eksport ====================================================================================

def export_state(config_path: Path, output_dir: Path | None = None, *, live: bool = False,
                 now: Callable[[], datetime] | None = None) -> Path:
    """Tworzy pakiet migracyjny; zwraca jego ścieżkę (plik ``.zip`` z prawami 600 na Linuksie).

    Args:
        live: pozwala na eksport przy działającym bocie (kopia jest spójna, ale zmiany po eksporcie nie
            trafią do pakietu – do przeniesienia bota zawsze eksportuj po jego zatrzymaniu).
    """
    config = _config(config_path)
    db_path = config.storage.db_path
    if not db_path.is_file():
        raise MigrationError(f"Nie ma bazy {db_path} – sprawdź storage.db_path w {config_path}")
    if not live:
        _require_stopped(config, db_path, action="końcowy eksport")
    moment = (now or (lambda: datetime.now(timezone.utc)))()
    output_dir = Path(output_dir) if output_dir else db_path.parent / "eksport"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    package = output_dir / f"gunb-migracja-{stamp}.zip"
    config_text = Path(config_path).read_text(encoding="utf-8")
    _refuse_secrets(config_text, "config.yaml")
    env_file = Path(config_path).resolve().parent / ".env"
    env_text = sanitize_env(env_file.read_text(encoding="utf-8")) if env_file.is_file() else sanitize_env("")

    with tempfile.TemporaryDirectory(dir=output_dir, prefix=".eksport-") as work:
        snapshot = Path(work) / db_path.name
        _backup_file(db_path, snapshot)
        schema, counts = _check_database(snapshot)
        db_entry = f"{DB_DIR}/{db_path.name}"
        files = {db_entry: snapshot}
        texts = {CONFIG_ENTRY: config_text, ENV_ENTRY: env_text}
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "created_at": moment.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "app": _app_version(),
            "schema_version": schema,
            "source": {"hostname": socket.gethostname(), "platform": platform.platform(), "db_file": db_path.name},
            "secrets_included": False,
            "counts": counts,
            "files": [_file_entry(db_entry, snapshot, "baza")] + [
                {"path": name, "role": "konfiguracja", "size": len(text.encode("utf-8")),
                 "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()} for name, text in texts.items()
            ],
        }
        partial = package.with_name(package.name + ".part")
        _private_file(partial)
        with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name, path in files.items():
                archive.write(path, name)
            for name, text in texts.items():
                archive.writestr(name, text)
            archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
        verify_package(partial)  # zanim oddamy pakiet – sprawdzamy go tak, jak zrobi to import
        os.replace(partial, package)
    return package


def sanitize_env(text: str) -> str:
    """Zmienne z ``.env`` bez sekretów: wartości tylko z ``ENV_KEEP`` i tylko gdy nie wyglądają na sekret."""
    lines = ["# Zmienne z pliku .env komputera źródłowego – BEZ sekretów.",
             "# Sekrety (token bota, webhooki, klucze) ustaw na serwerze: sudo gunb-admin sekrety", ""]
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if not re.fullmatch(r"[A-Z0-9_]+", key) or key in seen:
            continue
        seen.add(key)
        value = value.strip().strip('"').strip("'")
        if key in ENV_KEEP and not _looks_secret(value):
            lines.append(f"{key}={value}")
        else:
            lines += [f"# {key}: sekret – nie ma go w pakiecie, ustaw na serwerze", f"{key}="]
    if "TELEGRAM_BOT_TOKEN" not in seen:
        lines += ["# TELEGRAM_BOT_TOKEN: sekret – ustaw na serwerze", "TELEGRAM_BOT_TOKEN="]
    return "\n".join(lines) + "\n"


# === Sprawdzenie ===================================================================================

def verify_package(package: Path) -> dict:
    """Sprawdza pakiet bez rozpakowywania na dysk docelowy; zwraca manifest albo rzuca :class:`MigrationError`.

    Kontrole: bezpieczne nazwy i tylko oczekiwane pliki (bez dowiązań i szyfrowania), wersja formatu,
    rozmiary i sumy SHA-256, zgodność schematu z kodem, ``integrity_check`` bazy i liczności tabel.
    """
    package = Path(package)
    try:
        archive = zipfile.ZipFile(package)
    except (OSError, zipfile.BadZipFile) as exc:
        raise MigrationError(f"{package.name}: to nie jest poprawne archiwum ZIP ({exc})") from None
    with archive:
        infos = {info.filename: info for info in archive.infolist()}
        for info in infos.values():
            _check_member(info)
        if sum(info.file_size for info in infos.values()) > MAX_PACKAGE_BYTES:
            raise MigrationError(f"{package.name}: rozpakowany pakiet byłby za duży")
        manifest = _read_manifest(archive, infos)
        expected = {entry["path"] for entry in manifest["files"]} | {MANIFEST}
        unexpected = sorted(set(infos) - expected)
        if unexpected:
            raise MigrationError(f"{package.name}: nieoczekiwane pliki w pakiecie: {', '.join(unexpected)}")
        for entry in manifest["files"]:
            info = infos.get(entry["path"])
            if info is None:
                raise MigrationError(f"{package.name}: brakuje pliku {entry['path']} – pakiet niekompletny")
            if info.file_size != entry["size"]:
                raise MigrationError(f"{package.name}: {entry['path']} ma zły rozmiar – pakiet uszkodzony")
            if _sha256_member(archive, info) != entry["sha256"]:
                raise MigrationError(f"{package.name}: {entry['path']} ma złą sumę SHA-256 – pakiet uszkodzony")
        if manifest["schema_version"] > SCHEMA_VERSION:
            raise MigrationError(
                f"Pakiet ma schemat bazy v{manifest['schema_version']}, a ten kod zna tylko v{SCHEMA_VERSION} – "
                "zainstaluj na serwerze wersję aplikacji co najmniej taką, jak na komputerze źródłowym")
        db_entry = _db_entry(manifest)
        with tempfile.TemporaryDirectory(prefix="gunb-sprawdz-") as work:
            copy = Path(work) / "baza.sqlite"
            _extract(archive, infos[db_entry], copy)
            schema, counts = _check_database(copy)
    if schema != manifest["schema_version"] or counts != manifest["counts"]:
        raise MigrationError(f"{package.name}: baza nie zgadza się z manifestem (schemat lub liczba wierszy)")
    return manifest


# === Import ===========================================================================================

def import_state(package: Path, config_path: Path, *, replace: bool = False,
                 config_from_package: bool = False, now: Callable[[], datetime] | None = None) -> ImportResult:
    """Wczytuje pakiet na tym komputerze/serwerze (bot musi być zatrzymany).

    Najpierw wszystkie kontrole (pakiet, zatrzymany bot, istniejąca baza, miejsce na dysku), dopiero potem
    zapisy:

    * ``config.yaml`` z pakietu trafia na miejsce, gdy tu go jeszcze nie ma (albo z ``config_from_package`` –
      obecny zostaje wtedy zachowany jako kopia),
    * ``.env`` powstaje z ``env.bez-sekretow`` tylko, gdy go nie ma (token trzeba potem ustawić),
    * istniejąca baza jest zastępowana wyłącznie z ``replace`` – najpierw trafia do kopii zapasowej.

    Baza nie jest migrowana w trakcie importu; schemat podniesie pierwszy start bota (z własną kopią).
    """
    manifest = verify_package(package)
    stamp = (now or (lambda: datetime.now(timezone.utc)))().strftime("%Y%m%dT%H%M%SZ")
    config_path = Path(config_path)
    state_dir = config_path.resolve().parent
    state_dir.mkdir(parents=True, exist_ok=True)
    use_package_config = config_from_package or not config_path.exists()
    probe = state_dir / f".config-z-pakietu-{stamp}.yaml" if use_package_config else config_path
    committed = False
    try:
        with zipfile.ZipFile(package) as archive:
            package_env = archive.read(ENV_ENTRY).decode("utf-8")
            if use_package_config:
                _write_private_text(probe, archive.read(CONFIG_ENTRY).decode("utf-8"))
            config = _config(probe)
            db_path = config.storage.db_path
            _require_stopped(config, db_path, action="import")
            if db_path.exists() and not replace:
                raise MigrationError(
                    f"Baza {db_path} już istnieje – nie nadpisuję jej po cichu. Jeśli chcesz ją zastąpić danymi "
                    "z pakietu, dodaj --zastap (obecna baza trafi najpierw do kopii).")
            info = archive.getinfo(_db_entry(manifest))
            db_path.parent.mkdir(parents=True, exist_ok=True)
            _check_free_space(db_path.parent, info.file_size)
            # od tej chwili zmieniamy pliki – kontrole za nami
            replaced_backup = _backup_existing(config, db_path, f"przed-importem-{stamp}") if db_path.exists() else None
            incoming = db_path.with_name(db_path.name + ".import.part")
            _extract(archive, info, incoming)
        if use_package_config:
            if config_path.exists():
                shutil.copy2(config_path, config_path.with_name(f"{config_path.name}.przed-importem-{stamp}"))
            os.replace(probe, config_path)
        committed = True
        env_file = state_dir / ".env"
        if not env_file.exists():
            _write_private_text(env_file, package_env)
        _replace_database(incoming, db_path)
    finally:
        if use_package_config and not committed:
            probe.unlink(missing_ok=True)
    schema, counts = _check_database(db_path)
    if schema != manifest["schema_version"] or counts != manifest["counts"]:
        raise MigrationError("Po imporcie baza nie zgadza się z manifestem – przywróć kopię "
                             f"{replaced_backup or '(brak – baza była nowa)'}")
    return ImportResult(db_path, replaced_backup, schema, counts)


# === Kopia i odtworzenie ==============================================================================

def backup_now(config_path: Path, *, label: str = "reczna", now: Callable[[], datetime] | None = None) -> Path:
    """Spójna kopia bazy na żądanie (np. przed aktualizacją) – działa także przy pracującym bocie."""
    config = _config(config_path)
    if not config.storage.db_path.is_file():
        raise MigrationError(f"Nie ma bazy {config.storage.db_path}")
    stamp = (now or (lambda: datetime.now(timezone.utc)))().strftime("%Y%m%dT%H%M%SZ")
    return _backup_existing(config, config.storage.db_path, f"{label}-{stamp}")


def restore_backup(backup: Path, config_path: Path, *, now: Callable[[], datetime] | None = None) -> Path | None:
    """Zastępuje bazę kopią (bot zatrzymany); obecna baza najpierw trafia do kopii – zwraca jej ścieżkę.

    Zmiany zapisane w bazie po wykonaniu odtwarzanej kopii przepadają (zostają tylko w tej nowej kopii).
    """
    backup = Path(backup)
    schema, _ = _check_database(backup)
    if schema > SCHEMA_VERSION:
        raise MigrationError(f"Kopia ma schemat v{schema}, a ten kod zna tylko v{SCHEMA_VERSION} – najpierw "
                             "wróć do wersji aplikacji, która go obsługuje")
    config = _config(config_path)
    db_path = config.storage.db_path
    _require_stopped(config, db_path, action="odtworzenie kopii")
    stamp = (now or (lambda: datetime.now(timezone.utc)))().strftime("%Y%m%dT%H%M%SZ")
    replaced = _backup_existing(config, db_path, f"przed-odtworzeniem-{stamp}") if db_path.exists() else None
    incoming = db_path.with_name(db_path.name + ".odtworzenie.part")
    _backup_file(backup, incoming)
    _replace_database(incoming, db_path)
    _check_database(db_path)
    return replaced


# === Wewnętrzne =========================================================================================

def _config(config_path: Path) -> AppConfig:
    try:
        return load_config(Path(config_path))
    except ConfigError as exc:
        raise MigrationError(f"Konfiguracja {config_path}: {exc}") from None


def _require_stopped(config: AppConfig, db_path: Path, *, action: str) -> None:
    """Bot i inne procesy zapisujące muszą stać: blokada instancji, usługa systemd, import, zapis do bazy."""
    if not lock_is_free(bot_lock_path(db_path)):
        raise MigrationError(f"Bot działa na tych danych – zatrzymaj go przed operacją: {action}")
    if shutil.which("systemctl"):
        active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE], check=False)
        if active.returncode == 0:
            raise MigrationError(f"Usługa {SERVICE} działa – najpierw: sudo systemctl stop {SERVICE}")
    if not db_path.exists():
        return
    conn = sqlite3.connect(db_path, timeout=1.0, isolation_level=None)
    try:
        has_leases = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'blokady'").fetchone()
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if has_leases and conn.execute("SELECT 1 FROM blokady WHERE wygasa > ?", (now_iso,)).fetchone():
            raise MigrationError(f"Trwa import danych GUNB (blokada w bazie) – poczekaj na koniec przed: {action}")
        try:
            conn.execute("BEGIN EXCLUSIVE")
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            raise MigrationError(f"Inny proces zapisuje do bazy {db_path} – zatrzymaj go przed: {action}") from None
    finally:
        conn.close()


def _backup_file(source: Path, target: Path) -> None:
    """Spójna kopia pliku bazy przez API kopii SQLite (bez zapisu do źródła)."""
    if target.exists():
        target.unlink()
    src = sqlite3.connect(source)
    try:
        src.execute("PRAGMA query_only = ON")
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode = DELETE")  # kopia to jeden samodzielny plik
        finally:
            dst.close()
    finally:
        src.close()
    _chmod_private(target)


def _backup_existing(config: AppConfig, db_path: Path, label: str) -> Path:
    backup_dir = config.storage.backup_dir or db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{db_path.stem}-{label}.sqlite"
    _backup_file(db_path, target)
    _check_database(target)
    return target


def _check_database(path: Path) -> tuple[int, dict[str, int]]:
    """``integrity_check`` i ``foreign_key_check``; zwraca wersję schematu i liczby wierszy w tabelach.

    Połączenie tylko do zapytań (``query_only``) – działa też dla kopii w trybie WAL bez pliku ``-shm``.
    """
    try:
        conn = sqlite3.connect(Path(path), timeout=5.0)
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"{Path(path).name}: nie da się otworzyć bazy ({exc})") from None
    try:
        conn.execute("PRAGMA query_only = ON")
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise MigrationError(f"{Path(path).name}: baza jest uszkodzona (integrity_check)")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise MigrationError(f"{Path(path).name}: baza ma zerwane powiązania (foreign_key_check)")
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        if "investments" not in tables:
            raise MigrationError(f"{Path(path).name}: to nie jest baza GUNB Lead Tool")
        counts = {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
        return conn.execute("PRAGMA user_version").fetchone()[0], counts
    except sqlite3.DatabaseError as exc:
        raise MigrationError(f"{Path(path).name}: nie da się odczytać bazy ({exc})") from None
    finally:
        conn.close()


def _replace_database(incoming: Path, db_path: Path) -> None:
    """Podmienia plik bazy; stare ``-wal``/``-shm`` usuwa – należą do poprzedniej bazy i zepsułyby nową."""
    for suffix in ("-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    os.replace(incoming, db_path)
    _chmod_private(db_path)


def _check_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    parts = name.split("/")
    if (name.startswith("/") or "\\" in name or ":" in name or ".." in parts or "" in parts
            or any(part.startswith(".") for part in parts)):
        raise MigrationError(f"Niebezpieczna nazwa w pakiecie: {name!r}")
    if stat.S_ISLNK(info.external_attr >> 16) or info.is_dir():
        raise MigrationError(f"Pakiet zawiera dowiązanie albo katalog: {name!r}")
    if info.flag_bits & 0x1:
        raise MigrationError(f"Pakiet zawiera zaszyfrowany plik: {name!r}")


def _read_manifest(archive: zipfile.ZipFile, infos: dict[str, zipfile.ZipInfo]) -> dict:
    info = infos.get(MANIFEST)
    if info is None or info.file_size > 1_000_000:
        raise MigrationError("Pakiet nie ma manifestu – to nie jest pakiet migracyjny GUNB Lead Tool")
    try:
        manifest = json.loads(archive.read(info).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise MigrationError("Manifest pakietu jest uszkodzony") from None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise MigrationError("To nie jest pakiet migracyjny GUNB Lead Tool")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise MigrationError(f"Nieznana wersja formatu pakietu: {manifest.get('format_version')} "
                             f"(ten kod obsługuje {FORMAT_VERSION})")
    files = manifest.get("files")
    if (not isinstance(manifest.get("schema_version"), int) or not isinstance(manifest.get("counts"), dict)
            or not isinstance(files, list) or not files
            or not all(isinstance(f, dict) and isinstance(f.get("path"), str) and isinstance(f.get("size"), int)
                       and isinstance(f.get("sha256"), str) for f in files)):
        raise MigrationError("Manifest pakietu jest niekompletny")
    required = {CONFIG_ENTRY, ENV_ENTRY}
    missing = required - {f["path"] for f in files}
    if missing or len([f for f in files if f.get("role") == "baza"]) != 1:
        raise MigrationError("Pakiet niekompletny: brakuje bazy albo konfiguracji")
    return manifest


def _db_entry(manifest: dict) -> str:
    entry = next(f["path"] for f in manifest["files"] if f.get("role") == "baza")
    if not re.fullmatch(rf"{DB_DIR}/[A-Za-z0-9_.-]+\.sqlite", entry):
        raise MigrationError(f"Niebezpieczna nazwa bazy w manifeście: {entry!r}")
    return entry


def _extract(archive: zipfile.ZipFile, info: zipfile.ZipInfo, target: Path) -> None:
    """Rozpakowuje jeden, już sprawdzony plik pod wskazaną przez nas ścieżkę (nie z nazwy w archiwum)."""
    _private_file(target)
    with archive.open(info) as source, open(target, "wb") as destination:
        shutil.copyfileobj(source, destination, 1 << 20)
        destination.flush()
        os.fsync(destination.fileno())


def _sha256_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    digest = hashlib.sha256()
    try:
        with archive.open(info) as source:
            for chunk in iter(lambda: source.read(1 << 20), b""):
                digest.update(chunk)
    except zipfile.BadZipFile as exc:  # np. zła suma CRC
        raise MigrationError(f"{info.filename}: uszkodzony plik w pakiecie ({exc})") from None
    return digest.hexdigest()


def _file_entry(name: str, path: Path, role: str) -> dict:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return {"path": name, "role": role, "size": path.stat().st_size, "sha256": digest.hexdigest()}


def _check_free_space(directory: Path, needed: int) -> None:
    """Miejsce na rozpakowaną bazę z zapasem (kopia obecnej bazy, pliki WAL)."""
    free = shutil.disk_usage(directory).free
    if free < needed * 2 + 50_000_000:
        raise MigrationError(f"Za mało miejsca na dysku w {directory}: potrzeba ok. {needed * 2 // 1_000_000 + 50} MB, "
                             f"wolne {free // 1_000_000} MB")


def _looks_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_VALUES)


def _refuse_secrets(text: str, name: str) -> None:
    if _looks_secret(text):
        raise MigrationError(f"{name} zawiera sekret (token bota, webhook albo klucz) – przenieś go do .env; "
                             "pakiet migracyjny nie może zawierać sekretów")


def _app_version() -> dict:
    """Wersja aplikacji: z pyproject.toml i – gdy kod jest z Gita – numer commita."""
    root = Path(__file__).resolve().parent.parent
    version = "?"
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.M)
        version = match.group(1) if match else version
    commit = None
    if shutil.which("git") and (root / ".git").exists():
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                                check=False)
        if result.returncode == 0 and result.stdout.strip():
            commit = result.stdout.strip()
    release = root / "WERSJA"
    if commit is None and release.is_file():  # kopia bez .git (wydanie z instalatora)
        commit = release.read_text(encoding="utf-8").strip() or None
    return {"version": version, "commit": commit, "schema_version": SCHEMA_VERSION}


def _private_file(path: Path) -> None:
    """Tworzy pusty plik z prawami 600 (Linux) – pakiet i kopie zawierają dane użytkowników."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    _chmod_private(path)


def _write_private_text(path: Path, text: str) -> None:
    _private_file(path)
    path.write_bytes(text.encode("utf-8"))


def _chmod_private(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, 0o600)


# === Linia poleceń =======================================================================================

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gunb_tool.migration",
                                     description="Eksport, sprawdzenie i import stanu bota; kopia i odtworzenie bazy.")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("eksport", help="pakiet migracyjny (bot zatrzymany)")
    export.add_argument("--config", type=Path, default=Path("config.yaml"))
    export.add_argument("--do", dest="output", type=Path, help="katalog na pakiet (domyślnie data/eksport)")
    export.add_argument("--na-zywo", dest="live", action="store_true",
                        help="eksport przy działającym bocie (tylko do prób – zmiany po eksporcie nie przejdą)")
    check = commands.add_parser("sprawdz", help="sprawdza pakiet (sumy, schemat, integralność)")
    check.add_argument("package", type=Path)
    load = commands.add_parser("importuj", help="wczytuje pakiet (bot zatrzymany)")
    load.add_argument("package", type=Path)
    load.add_argument("--config", type=Path, required=True)
    load.add_argument("--zastap", dest="replace", action="store_true",
                      help="zastąp istniejącą bazę (najpierw trafi do kopii)")
    load.add_argument("--konfiguracja-z-pakietu", dest="config_from_package", action="store_true",
                      help="użyj config.yaml z pakietu (obecny zostanie zachowany jako kopia)")
    backup = commands.add_parser("kopia", help="spójna kopia bazy teraz")
    backup.add_argument("--config", type=Path, default=Path("config.yaml"))
    backup.add_argument("--etykieta", default="reczna")
    restore = commands.add_parser("odtworz", help="zastępuje bazę kopią (bot zatrzymany)")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--config", type=Path, default=Path("config.yaml"))
    args = parser.parse_args(argv)
    try:
        if args.command == "eksport":
            package = export_state(args.config, args.output, live=args.live)
            manifest = verify_package(package)
            print(f"✔ Pakiet: {package}\n  schemat v{manifest['schema_version']}, "
                  f"inwestycje: {manifest['counts'].get('investments', 0)}, "
                  f"użytkownicy: {manifest['counts'].get('bot_users', 0)}\n"
                  f"  SHA-256 pliku: {_sha256_file(package)}\n"
                  "  Pakiet zawiera dane użytkowników (bez sekretów) – przenieś go bezpiecznie i usuń po imporcie.")
        elif args.command == "sprawdz":
            manifest = verify_package(args.package)
            print(f"✔ Pakiet poprawny: schemat v{manifest['schema_version']} (kod: v{SCHEMA_VERSION}), "
                  f"utworzony {manifest['created_at']}, aplikacja {manifest['app'].get('commit') or '?'}")
            for table, count in sorted(manifest["counts"].items()):
                print(f"  {table}: {count}")
        elif args.command == "importuj":
            result = import_state(args.package, args.config, replace=args.replace,
                                  config_from_package=args.config_from_package)
            print(f"✔ Zaimportowano bazę {result.database} (schemat v{result.schema_version}).")
            if result.replaced_backup:
                print(f"  Poprzednia baza: {result.replaced_backup}")
            if result.schema_version < SCHEMA_VERSION:
                print(f"  Pierwszy start podniesie schemat do v{SCHEMA_VERSION} (z kopią sprzed zmiany).")
            print("  Dalej: ustaw token bota (sudo gunb-admin sekrety) i uruchom jedną instancję.")
        elif args.command == "kopia":
            print(f"✔ Kopia: {backup_now(args.config, label=args.etykieta)}")
        elif args.command == "odtworz":
            replaced = restore_backup(args.backup, args.config)
            print(f"✔ Odtworzono bazę z {args.backup}." + (f" Poprzednia baza: {replaced}" if replaced else ""))
    except MigrationError as exc:
        print(f"✘ {exc}", file=sys.stderr)
        return 1
    return 0


def _sha256_file(path: Path) -> str:
    return _file_entry("", path, "")["sha256"]


if __name__ == "__main__":
    sys.exit(main())
