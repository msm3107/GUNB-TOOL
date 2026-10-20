"""Przeniesienie stanu: komputer źródłowy → pakiet → serwer (drugi katalog). Tylko tymczasowe katalogi i bazy."""

import json
import sqlite3
import stat
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

import core.migration as migration_module
from core.instance import instance_lock, lock_is_free
from core.migration import (
    CONFIG_ENTRY,
    ENV_ENTRY,
    MANIFEST,
    MigrationError,
    backup_now,
    export_state,
    import_state,
    main,
    restore_backup,
    verify_package,
)
from core.models import Investment
from core.storage import SCHEMA_VERSION, LeadRepository

TOKEN = "123456789:AAH-fake-token_" + "x" * 24
WEBHOOK = "https://discord.com/api/webhooks/42/sekretny-webhook-xyz"
CONFIG = """\
gunb:
  voivodeships: ['28']
  powiats: ['2862', '2814']
storage:
  db_path: data/gunb_leads.sqlite
telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN}
"""
ENV = f"TELEGRAM_BOT_TOKEN={TOKEN}\n"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)


def make_state(root: Path, clock) -> Path:
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (root / ".env").write_text(ENV, encoding="utf-8")
    with LeadRepository(root / "data" / "gunb_leads.sqlite", now=clock.now_utc) as repo:
        repo.upsert(Investment(
            id_sprawy="A/1", zrodlo="pozwolenia", status="wniosek", nazwa_zamierzenia="Dom z historią"
        ))
        clock.advance(hours=1)
        repo.upsert(Investment(
            id_sprawy="A/1", zrodlo="pozwolenia", status="decyzja", nazwa_zamierzenia="Dom z historią"
        ))
        repo.upsert(Investment(
            id_sprawy="B/1", zrodlo="pozwolenia", status="decyzja", nazwa_zamierzenia="Hala", lat=53.78, lon=20.49
        ))
    return root / "config.yaml"


def db_of(config: Path) -> Path:
    return config.parent / "data" / "gunb_leads.sqlite"


def dump(path: Path) -> dict[str, list]:
    conn = sqlite3.connect(path)
    try:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {t: sorted(conn.execute(f'SELECT * FROM "{t}"').fetchall(), key=repr) for t in tables}
    finally:
        conn.close()


@pytest.fixture
def source(tmp_path, clock):
    return make_state(tmp_path / "komputer", clock)


@pytest.fixture
def package(tmp_path, source):
    return export_state(source, tmp_path / "przenoszone")


def test_export_import_keeps_every_row(tmp_path, source, package):
    result = import_state(package, tmp_path / "serwer" / "config.yaml")

    assert dump(result.database) == dump(db_of(source))
    assert result.schema_version == SCHEMA_VERSION
    assert result.counts["investments"] == 2
    assert result.counts["status_history"] == 3


def test_export_import_keeps_b2b_delivery_memory(tmp_path, clock):
    source = make_state(tmp_path / "komputer", clock)
    with sqlite3.connect(db_of(source)) as conn:
        conn.execute(
            "INSERT INTO b2b_customers (customer_id, email, status_subskrypcji) "
            "VALUES (42, 'biuro@example.pl', 'active')"
        )
        conn.execute(
            "INSERT INTO b2b_orders (order_id, customer_id, filtry_json, typ_pakietu, status_platnosci) "
            "VALUES (84, 42, '{\"powiaty\":[\"1234\"]}', 'jednorazowy', 'paid')"
        )
        conn.execute(
            "INSERT INTO b2b_deliveries (delivery_id, order_id, typ_dostawy, doreczono_utc, plik_xlsx) "
            "VALUES (126, 84, 'pelna', '2026-10-01T00:00:00Z', 'delivery.xlsx')"
        )
        conn.execute(
            "INSERT INTO b2b_delivery_items (delivery_id, id_sprawy, rewizja, kategoria_dostawy) "
            "VALUES (126, 'A/1', 'rev-1', 'full')"
        )

    package = export_state(source, tmp_path / "pakiety")
    result = import_state(package, tmp_path / "serwer" / "config.yaml")

    assert result.schema_version == SCHEMA_VERSION == 16
    assert dump(result.database) == dump(db_of(source))


def test_command_line_round_trip(tmp_path, source, capsys):
    assert main(["eksport", "--config", str(source), "--do", str(tmp_path / "out")]) == 0
    (package,) = (tmp_path / "out").glob("gunb-migracja-*.zip")
    assert main(["sprawdz", str(package)]) == 0
    assert main(["importuj", str(package), "--config", str(tmp_path / "serwer" / "config.yaml")]) == 0
    out = capsys.readouterr().out
    assert "SHA-256" in out and "Zaimportowano" in out and "investments" in out


def test_package_carries_no_secrets(package):
    with zipfile.ZipFile(package) as archive:
        everything = b"".join(archive.read(name) for name in archive.namelist())
        env = archive.read(ENV_ENTRY).decode("utf-8")
    assert TOKEN.encode() not in everything
    assert "\nTELEGRAM_BOT_TOKEN=\n" in env


def test_config_with_an_inline_token_is_not_exported(tmp_path, source):
    source.write_text(CONFIG.replace("${TELEGRAM_BOT_TOKEN}", TOKEN), encoding="utf-8")
    with pytest.raises(MigrationError, match="sekret"):
        export_state(source, tmp_path / "out")


def test_imported_state_gets_env_without_secrets_and_keeps_an_existing_one(tmp_path, package):
    target = tmp_path / "serwer" / "config.yaml"
    import_state(package, target)
    env = (target.parent / ".env").read_text(encoding="utf-8")
    assert "TELEGRAM_BOT_TOKEN=\n" in env and TOKEN not in env

    other = tmp_path / "serwer2"
    other.mkdir()
    (other / ".env").write_text("TELEGRAM_BOT_TOKEN=juz-ustawiony\n", encoding="utf-8")
    import_state(package, other / "config.yaml")
    assert (other / ".env").read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=juz-ustawiony\n"


def test_final_export_and_import_refuse_while_process_locks_are_held(tmp_path, source, package):
    with instance_lock(source.parent / "data" / "gunb-bot.lock"):
        with pytest.raises(MigrationError, match="Silnik działa"):
            export_state(source, tmp_path / "out")
        assert export_state(source, tmp_path / "proba", live=True).is_file()

    target = tmp_path / "serwer" / "config.yaml"
    target.parent.mkdir()
    target.write_text(CONFIG, encoding="utf-8")
    with instance_lock(target.parent / "data" / "gunb-bot.lock"), pytest.raises(
        MigrationError, match="Silnik działa"
    ):
        import_state(package, target)
    assert not db_of(target).exists()


def test_import_waits_for_a_running_data_import(tmp_path, package, clock):
    target = tmp_path / "serwer" / "config.yaml"
    import_state(package, target)
    repo = LeadRepository(db_of(target))
    repo.acquire_lease("import", "cron", timedelta(minutes=30))
    repo.close()
    with pytest.raises(MigrationError, match="Trwa import"):
        import_state(package, target, replace=True)


def test_import_refuses_an_active_engine_and_preserves_the_database(tmp_path, package, clock):
    target = make_state(tmp_path / "serwer", clock)
    db_path = db_of(target)
    before = dump(db_path)

    with instance_lock(db_path.parent / "gunb-engine.lock"), pytest.raises(
        MigrationError, match="Silnik działa"
    ):
        import_state(package, target, replace=True)

    assert dump(db_path) == before


@pytest.mark.parametrize("operation", ["import", "restore"])
def test_database_replacement_holds_engine_and_maintenance_locks(
    tmp_path, package, clock, monkeypatch, operation
):
    target = tmp_path / operation
    config_path = make_state(target, clock)
    db_path = db_of(config_path)
    backup = backup_now(config_path) if operation == "restore" else None
    replace_database = migration_module._replace_database

    def check_locks_then_replace(incoming, destination):
        assert destination == db_path
        for name in ("gunb-maintenance.lock", "gunb-engine.lock", "gunb-bot.lock"):
            assert not lock_is_free(destination.parent / name)
        return replace_database(incoming, destination)

    monkeypatch.setattr(migration_module, "_replace_database", check_locks_then_replace)

    if operation == "import":
        import_state(package, config_path, replace=True)
    else:
        assert backup is not None
        restore_backup(backup, config_path)


def rebuild(
    package: Path,
    target: Path,
    *,
    drop=(),
    replace=None,
    add=None,
    manifest_change=None,
    symlink: str | None = None,
) -> Path:
    with zipfile.ZipFile(package) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    for name in drop:
        entries.pop(name)
    for name, data in (replace or {}).items():
        entries[name] = data
    if manifest_change:
        manifest = json.loads(entries[MANIFEST])
        manifest_change(manifest)
        entries[MANIFEST] = json.dumps(manifest).encode()
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in {**entries, **(add or {})}.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "/etc/passwd")
    return target


def db_entry_of(package: Path) -> str:
    with zipfile.ZipFile(package) as archive:
        return next(n for n in archive.namelist() if n.startswith("baza/"))


def flip_byte(data: bytes) -> bytes:
    middle = len(data) // 2
    return data[:middle] + bytes([data[middle] ^ 0xFF]) + data[middle + 1 :]


DAMAGE = {
    "zmieniony bajt bazy": lambda p, t: rebuild(
        p, t, replace={db_entry_of(p): flip_byte(zipfile.ZipFile(p).read(db_entry_of(p)))}
    ),
    "brak bazy": lambda p, t: rebuild(p, t, drop=[db_entry_of(p)]),
    "brak konfiguracji": lambda p, t: rebuild(p, t, drop=[CONFIG_ENTRY]),
    "obcy plik poza katalogiem": lambda p, t: rebuild(p, t, add={"../../zlosliwy.txt": b"x"}),
    "ukryty plik": lambda p, t: rebuild(p, t, add={".env": b"TELEGRAM_BOT_TOKEN=cudzy"}),
    "dowiązanie": lambda p, t: rebuild(p, t, symlink="baza/link.sqlite"),
    "nieznany format": lambda p, t: rebuild(p, t, manifest_change=lambda m: m.update(format_version=99)),
    "nieznana wersja schematu": lambda p, t: rebuild(
        p, t, manifest_change=lambda m: m.update(schema_version=SCHEMA_VERSION + 1)
    ),
    "błędny hash bazy": lambda p, t: rebuild(
        p,
        t,
        manifest_change=lambda m: [f.update(sha256="0" * 64) for f in m["files"] if f.get("role") == "baza"],
    ),
    "brak pliku w manifeście": lambda p, t: rebuild(
        p,
        t,
        manifest_change=lambda m: m.update(files=[f for f in m["files"] if f.get("role") != "baza"]),
    ),
}


@pytest.mark.parametrize("kind", list(DAMAGE))
def test_damaged_packages_are_rejected_before_any_change(tmp_path, package, kind):
    bad = DAMAGE[kind](package, tmp_path / "bad.zip")
    with pytest.raises(MigrationError):
        verify_package(bad)
    target = tmp_path / "cel" / "config.yaml"
    with pytest.raises(MigrationError):
        import_state(bad, target)
    assert not target.exists()


def test_restore_replaces_database_and_saves_previous_one(tmp_path, clock):
    target = tmp_path / "live"
    make_state(target, clock)
    backup = backup_now(target / "config.yaml", label="przed-testem")
    with LeadRepository(db_of(target / "config.yaml")) as repo:
        repo.upsert(Investment(
            id_sprawy="NOWY/1", zrodlo="pozwolenia", status="decyzja", nazwa_zamierzenia="Nowa po kopii"
        ))
        assert repo.get("NOWY/1") is not None

    previous = restore_backup(backup, target / "config.yaml")

    with LeadRepository(db_of(target / "config.yaml")) as repo:
        assert repo.get("NOWY/1") is None
    with LeadRepository(previous) as old_repo:
        assert old_repo.get("NOWY/1") is not None
