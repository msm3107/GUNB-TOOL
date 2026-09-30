"""Przeniesienie stanu: komputer źródłowy → pakiet → „serwer” (drugi katalog). Tylko tymczasowe katalogi i bazy.

Testy porównują zawartość tabel wiersz po wierszu i sprawdzają stan przez kod bota (dostęp, przypomnienia,
doręczenia), a nie samo istnienie plików.
"""

import hashlib
import json
import os
import sqlite3
import stat
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from gunb_tool.bot_store import BotStore, LeadFlags
from gunb_tool.instance import instance_lock
from gunb_tool.migration import (CONFIG_ENTRY, ENV_ENTRY, MANIFEST, MigrationError, backup_now, export_state,
                                 import_state, main, restore_backup, verify_package)
from gunb_tool.storage import SCHEMA_VERSION, LeadRepository
from tests.bot_helpers import ADMIN, MIETEK, OBCY, FakeApi, activate, click, configured, lead, make_bot, message
from tests.test_storage import legacy_database

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
  admin_chat_id: ${TELEGRAM_ADMIN_CHAT_ID}
discord:
  webhook_url: ${DISCORD_WEBHOOK_URL}
bot:
  admins: ["${ADMIN_CHAT_ID}"]
  admin_contact: ${ADMIN_CONTACT}
"""
ENV = (f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_ADMIN_CHAT_ID=1001\nADMIN_CHAT_ID=1001\n"
       f"ADMIN_CONTACT=@admin_gunb\nDISCORD_WEBHOOK_URL={WEBHOOK}\n")
LEGACY, PAUSED = 4004, 5005
BASE = (53.7784, 20.4801)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    """Zmienne z testowego .env ustawione wprost – wczytanie konfiguracji nie zostawi ich w innych testach."""
    for key, value in (("TELEGRAM_BOT_TOKEN", TOKEN), ("TELEGRAM_ADMIN_CHAT_ID", "1001"), ("ADMIN_CHAT_ID", "1001"),
                       ("ADMIN_CONTACT", "@admin_gunb"), ("DISCORD_WEBHOOK_URL", WEBHOOK), ("TELEGRAM_CHAT_ID", "")):
        monkeypatch.setenv(key, value)


def make_state(root: Path, clock) -> Path:
    """Katalog stanu jak na komputerze: config.yaml, .env i baza zbudowana prawdziwymi przepływami bota."""
    root.mkdir(parents=True)
    (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (root / ".env").write_text(ENV, encoding="utf-8")
    repo = LeadRepository(root / "data" / "gunb_leads.sqlite", now=clock.now_utc)
    api = FakeApi()
    bot = make_bot(repo, api, clock)
    store = BotStore(repo)
    activate(bot, api, MIETEK)  # abonament 30 dni
    bot.handle_update(message(OBCY, "/start"))
    configured(bot, OBCY)
    bot.handle_update(click(ADMIN, f"adm:trial:{OBCY}"))
    bot.handle_update(click(OBCY, "ts"))  # test 7 dni trwa
    store.register(LEGACY, "Dotychczasowy", None, status="aktywny", backlog_days=7)
    repo.connection.execute("UPDATE bot_users SET dostep_bez_limitu = 1, konfiguracja = 'gotowe' WHERE chat_id = ?",
                            (LEGACY,))
    store.register(PAUSED, "Pauza", None, status="aktywny", backlog_days=7)
    store.set_paused(PAUSED, True)
    repo.upsert(lead("A/1", nazwa_zamierzenia="Dom z historią", status="wniosek"))
    clock.advance(hours=1)
    repo.upsert(lead("A/1", nazwa_zamierzenia="Dom z historią", status="decyzja"))  # zmiana statusu = historia
    repo.upsert(lead("B/1", nazwa_zamierzenia="Hala", lat=53.78, lon=20.49))
    nr = repo.get("A/1").nr
    location = message(MIETEK, "")
    location["message"]["location"] = {"latitude": BASE[0], "longitude": BASE[1]}
    bot.handle_update(location)  # pinezka bazy + promień
    store.set_trade(MIETEK, "dach")
    for data in (f"s1:{nr}", f"r1:{nr}", f"pr:{nr}:7", f"nt:{nr}"):
        bot.handle_update(click(MIETEK, data))
    bot.handle_update(message(MIETEK, "Kowalski & syn – oddzwonić"))
    store.add_watch(MIETEK, "gmina", "3021085", "Kostrzyn")
    bot.deliver_reports("rano")  # doręczenia i kolejka wysyłek
    bot.run_due_jobs()  # harmonogram i stan zadań
    store.mark_job("telegram_offset", "987654")
    repo.close()
    return root / "config.yaml"


def db_of(config: Path) -> Path:
    return config.parent / "data" / "gunb_leads.sqlite"


def dump(path: Path) -> dict[str, list]:
    """Cała zawartość bazy: tabela → posortowane wiersze."""
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {t: sorted(conn.execute(f'SELECT * FROM "{t}"').fetchall(), key=repr) for t in tables}
    finally:
        conn.close()


@pytest.fixture
def source(tmp_path, clock):
    return make_state(tmp_path / "komputer", clock)


@pytest.fixture
def package(tmp_path, source):
    return export_state(source, tmp_path / "przenoszone")


# --- Eksport → import: te same dane, uprawnienia i stan ------------------------------------------------------------

def test_export_import_keeps_every_row(tmp_path, source, package):
    result = import_state(package, tmp_path / "serwer" / "config.yaml")

    assert dump(result.database) == dump(db_of(source))
    assert result.schema_version == SCHEMA_VERSION
    assert {"deliveries", "wysylki", "zadania", "przypomnienia", "notatki", "zdarzenia"} <= set(result.counts)
    assert all(result.counts[t] > 0 for t in ("investments", "status_history", "bot_users", "deliveries",
                                               "wysylki", "zadania", "przypomnienia", "notatki", "watchlist"))


def test_access_marks_notes_and_schedule_survive_the_move(tmp_path, source, package, clock):
    result = import_state(package, tmp_path / "serwer" / "config.yaml")

    repo = LeadRepository(result.database, now=clock.now_utc)
    try:
        bot = make_bot(repo, FakeApi(), clock)
        store = BotStore(repo)
        users = {chat: store.get_user(chat) for chat in (MIETEK, OBCY, LEGACY, PAUSED)}
        assert bot._has_access(users[MIETEK]) and not users[MIETEK].on_trial
        assert users[OBCY].on_trial and users[OBCY].test_start is not None and bot._has_access(users[OBCY])
        assert users[LEGACY].bez_limitu and bot._has_access(users[LEGACY])
        assert users[PAUSED].wstrzymane and not bot._has_access(users[PAUSED])
        assert users[MIETEK].filtry.baza == BASE and users[MIETEK].filtry.promien_km == 15
        assert users[MIETEK].branza == "dach" and users[MIETEK].setup_done
        assert store.lead_flags(MIETEK, "A/1") == LeadFlags(saved=True, reviewed=True)
        assert store.note(MIETEK, "A/1") == "Kowalski & syn – oddzwonić"
        assert store.reminder(MIETEK, "A/1") is not None
        assert [w.etykieta for w in store.watchlist(MIETEK)] == ["Kostrzyn"]
        assert store.job_last_run("telegram_offset") == "987654"
        assert store.job_time("raport_rano") is not None
        assert len(repo.status_history("A/1")) == 2
    finally:
        repo.close()


def test_moved_bot_sends_only_what_is_new(tmp_path, source, package, clock):
    """Po przeniesieniu nie ma powtórki: raport ma tylko to, co przyszło po eksporcie."""
    result = import_state(package, tmp_path / "serwer" / "config.yaml")
    repo = LeadRepository(result.database, now=clock.now_utc)
    try:
        api = FakeApi()
        bot = make_bot(repo, api, clock)
        clock.utc = datetime(2026, 9, 30, 5, 1, tzinfo=timezone.utc)  # następny ranek, 07:01
        repo.upsert(lead("C/1", nazwa_zamierzenia="Nowa budowa po migracji", lat=53.779, lon=20.481))
        bot.run_due_jobs()

        texts = " ".join(m["text"] for m in api.to(MIETEK))
        assert "Nowa budowa po migracji" in texts
        assert "Dom z historią" not in texts and "Hala" not in texts
        assert api.to(PAUSED) == []
    finally:
        repo.close()


def test_personal_reminder_still_comes_after_the_move(tmp_path, source, package, clock):
    result = import_state(package, tmp_path / "serwer" / "config.yaml")
    repo = LeadRepository(result.database, now=clock.now_utc)
    try:
        api = FakeApi()
        bot = make_bot(repo, api, clock)
        clock.utc = datetime(2026, 10, 6, 5, 1, tzinfo=timezone.utc)  # 7 dni później, 07:01
        bot.run_due_jobs()
        assert any(m["text"].startswith("⏰ <b>Przypomnienie") for m in api.to(MIETEK))
    finally:
        repo.close()


def test_command_line_round_trip(tmp_path, source, capsys):
    assert main(["eksport", "--config", str(source), "--do", str(tmp_path / "out")]) == 0
    (package,) = (tmp_path / "out").glob("gunb-migracja-*.zip")
    assert main(["sprawdz", str(package)]) == 0
    assert main(["importuj", str(package), "--config", str(tmp_path / "serwer" / "config.yaml")]) == 0
    out = capsys.readouterr().out
    assert "SHA-256" in out and "Zaimportowano" in out and "investments" in out


# --- Bez sekretów, z ograniczonymi prawami -----------------------------------------------------------------------

def test_package_carries_no_secrets(package):
    with zipfile.ZipFile(package) as archive:
        everything = b"".join(archive.read(name) for name in archive.namelist())
        env = archive.read(ENV_ENTRY).decode("utf-8")
    assert TOKEN.encode() not in everything and b"sekretny-webhook" not in everything
    assert "ADMIN_CHAT_ID=1001" in env and "ADMIN_CONTACT=@admin_gunb" in env
    assert "\nTELEGRAM_BOT_TOKEN=\n" in env and "\nDISCORD_WEBHOOK_URL=\n" in env
    if os.name == "posix":
        assert stat.S_IMODE(package.stat().st_mode) == 0o600


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


# --- Działający bot ------------------------------------------------------------------------------------------------

def test_final_export_and_import_refuse_while_the_bot_runs(tmp_path, source, package):
    with instance_lock(source.parent / "data" / "gunb-bot.lock"):
        with pytest.raises(MigrationError, match="Bot działa"):
            export_state(source, tmp_path / "out")
        assert export_state(source, tmp_path / "proba", live=True).is_file()  # świadoma kopia „na żywo”

    target = tmp_path / "serwer" / "config.yaml"
    target.parent.mkdir()
    target.write_text(CONFIG, encoding="utf-8")
    with instance_lock(target.parent / "data" / "gunb-bot.lock"):
        with pytest.raises(MigrationError, match="Bot działa"):
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


# --- Uszkodzone i obce pakiety ---------------------------------------------------------------------------------------

def rebuild(package: Path, target: Path, *, drop=(), replace=None, add=None, manifest_change=None,
            symlink: str | None = None) -> Path:
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
    return data[:middle] + bytes([data[middle] ^ 0xFF]) + data[middle + 1:]


DAMAGE = {
    "zmieniony bajt bazy": lambda p, t: rebuild(p, t, replace={db_entry_of(p): flip_byte(
        zipfile.ZipFile(p).read(db_entry_of(p)))}),
    "brak bazy": lambda p, t: rebuild(p, t, drop=[db_entry_of(p)]),
    "brak konfiguracji": lambda p, t: rebuild(p, t, drop=[CONFIG_ENTRY]),
    "obcy plik poza katalogiem": lambda p, t: rebuild(p, t, add={"../../zlosliwy.txt": b"x"}),
    "ukryty plik": lambda p, t: rebuild(p, t, add={".env": b"TELEGRAM_BOT_TOKEN=cudzy"}),
    "dowiązanie": lambda p, t: rebuild(p, t, symlink="baza/link.sqlite"),
    "nieznany format": lambda p, t: rebuild(p, t, manifest_change=lambda m: m.update(format_version=99)),
    "nowszy schemat": lambda p, t: rebuild(p, t, manifest_change=lambda m: m.update(schema_version=SCHEMA_VERSION + 1)),
    "liczności nie pasują": lambda p, t: rebuild(p, t, manifest_change=lambda m: m["counts"].update(bot_users=999)),
    "brak manifestu": lambda p, t: rebuild(p, t, drop=[MANIFEST]),
}


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_damaged_or_foreign_package_is_rejected_before_anything_is_written(tmp_path, package, damage):
    bad = DAMAGE[damage](package, tmp_path / "zly.zip")
    target = tmp_path / "serwer" / "config.yaml"

    with pytest.raises(MigrationError):
        verify_package(bad)
    with pytest.raises(MigrationError):
        import_state(bad, target)

    assert not target.exists() and not db_of(target).exists()
    assert not (tmp_path / "zlosliwy.txt").exists() and not (tmp_path.parent / "zlosliwy.txt").exists()


def test_not_a_zip_is_rejected(tmp_path):
    fake = tmp_path / "pakiet.zip"
    fake.write_bytes(b"to nie jest zip")
    with pytest.raises(MigrationError, match="ZIP"):
        verify_package(fake)


# --- Istniejąca baza na serwerze -----------------------------------------------------------------------------------

def test_existing_database_is_replaced_only_on_request_and_backed_up_first(tmp_path, package, clock):
    target = make_state(tmp_path / "serwer", clock)  # na „serwerze” już są jakieś dane
    repo = LeadRepository(db_of(target))
    repo.upsert(lead("TYLKO/SERWER"))
    repo.close()
    before = dump(db_of(target))
    Path(str(db_of(target)) + "-wal").write_bytes(b"stary WAL innej bazy")  # musi zniknąć przy podmianie

    with pytest.raises(MigrationError, match="--zastap"):
        import_state(package, target)
    assert dump(db_of(target)) == before

    result = import_state(package, target, replace=True)

    assert dump(result.replaced_backup) == before  # poprzednia baza zachowana w kopii
    assert "TYLKO/SERWER" not in str(dump(result.database)["investments"])
    assert not Path(str(db_of(target)) + "-wal").exists()  # stary WAL innej bazy usunięty


def test_existing_config_is_kept_unless_asked(tmp_path, package):
    target = tmp_path / "serwer" / "config.yaml"
    target.parent.mkdir()
    own = CONFIG.replace("powiats: ['2862', '2814']", "powiats: ['2862']")
    target.write_text(own, encoding="utf-8")

    import_state(package, target)
    assert target.read_text(encoding="utf-8") == own

    import_state(package, target, replace=True, config_from_package=True)
    assert target.read_text(encoding="utf-8") == CONFIG
    assert list(target.parent.glob("config.yaml.przed-importem-*"))


# --- Stara baza (v6 – wersja z main) ---------------------------------------------------------------------------------

def test_package_from_an_old_database_is_upgraded_on_first_start_with_a_backup(tmp_path, clock):
    source = tmp_path / "komputer"
    source.mkdir()
    (source / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (source / "data").mkdir()
    legacy = legacy_database(source / "data" / "gunb_leads.sqlite", 6)
    legacy.execute("INSERT INTO bot_users (chat_id, status, nowe_od, utworzono, zmieniono, is_active, subscription_ends)"
                   " VALUES (?, 'aktywny', 'x', 'x', 'x', 1, '2026-12-31T00:00:00+00:00')", (MIETEK,))
    legacy.commit()
    legacy.close()

    package = export_state(source / "config.yaml", tmp_path / "out")
    old = sqlite3.connect(source / "data" / "gunb_leads.sqlite")
    assert old.execute("PRAGMA user_version").fetchone()[0] == 6  # eksport nie migruje bazy źródłowej
    old.close()
    result = import_state(package, tmp_path / "serwer" / "config.yaml")
    assert result.schema_version == 6

    repo = LeadRepository(result.database, now=clock.now_utc)  # pierwszy start na serwerze
    try:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert make_bot(repo, FakeApi(), clock)._has_access(BotStore(repo).get_user(MIETEK))
    finally:
        repo.close()
    assert list((result.database.parent / "backups").glob("gunb_leads-przed-v*.sqlite"))


# --- Kopia i odtworzenie -----------------------------------------------------------------------------------------------

def test_backup_and_restore_on_a_separate_database(tmp_path, source, clock):
    backup = backup_now(source)
    saved = dump(backup)
    repo = LeadRepository(db_of(source), now=clock.now_utc)
    BotStore(repo).register(7777, "Po kopii", None, status="aktywny", backlog_days=7)
    repo.close()

    with instance_lock(source.parent / "data" / "gunb-bot.lock"):
        with pytest.raises(MigrationError, match="Bot działa"):
            restore_backup(backup, source)
    replaced = restore_backup(backup, source)

    assert dump(db_of(source)) == saved
    assert 7777 in [row[0] for row in dump(replaced)["bot_users"]]  # zmiany po kopii – w kopii „przed odtworzeniem”


def test_restoring_a_backup_from_a_newer_schema_is_refused(tmp_path, source):
    backup = backup_now(source)
    conn = sqlite3.connect(backup)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(MigrationError, match="schemat"):
        restore_backup(backup, source)


def test_manifest_describes_the_package(package):
    manifest = verify_package(package)
    assert manifest["format"] == "gunb-tool-migracja" and manifest["format_version"] == 1
    assert manifest["schema_version"] == SCHEMA_VERSION and manifest["secrets_included"] is False
    assert manifest["created_at"].endswith("+00:00") and manifest["app"]["schema_version"] == SCHEMA_VERSION
    assert {f["path"] for f in manifest["files"]} == {"baza/gunb_leads.sqlite", CONFIG_ENTRY, ENV_ENTRY}
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])


# --- Kontrole, które muszą działać same (każda ma przypadek, którego nie złapie nic innego) -------------------------

def test_same_size_change_is_caught_by_the_checksum(tmp_path, package):
    """Podmiana tej samej długości (inne województwo) – wykryje ją tylko suma SHA-256."""
    with zipfile.ZipFile(package) as archive:
        config = archive.read(CONFIG_ENTRY)
    bad = rebuild(package, tmp_path / "zly.zip", replace={CONFIG_ENTRY: config.replace(b"['28']", b"['29']")})
    with pytest.raises(MigrationError, match="SHA-256"):
        verify_package(bad)


def test_path_outside_listed_in_the_manifest_is_rejected(tmp_path, package):
    evil = b"zlosliwa zawartosc"
    entry = {"path": "../../zlosliwy.txt", "role": "inne", "size": len(evil),
             "sha256": hashlib.sha256(evil).hexdigest()}
    bad = rebuild(package, tmp_path / "zly.zip", add={"../../zlosliwy.txt": evil},
                  manifest_change=lambda m: m["files"].append(entry))
    with pytest.raises(MigrationError, match="Niebezpieczna nazwa"):
        verify_package(bad)


def test_symlink_in_place_of_the_database_is_rejected(tmp_path, package):
    name = db_entry_of(package)
    bad = rebuild(package, tmp_path / "zly.zip", drop=[name], symlink=name)
    with pytest.raises(MigrationError, match="dowiązanie"):
        verify_package(bad)


def test_stale_wal_of_another_database_never_reaches_the_new_one(tmp_path):
    """Poprawny WAL innej bazy obok podmienianego pliku zostałby „odtworzony” na nowej bazie – musi zniknąć."""
    from gunb_tool.migration import _replace_database

    incoming = tmp_path / "nowa.sqlite"
    LeadRepository(incoming).close()
    expected = dump(incoming)
    other = tmp_path / "inna.sqlite"
    writer = sqlite3.connect(other)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("PRAGMA wal_autocheckpoint = 0")
    writer.execute("CREATE TABLE obca (x TEXT)")
    writer.executemany("INSERT INTO obca VALUES (?)", [("śmieci",)] * 500)
    writer.commit()
    foreign_wal = Path(str(other) + "-wal").read_bytes()
    writer.close()
    target = tmp_path / "baza.sqlite"
    target.write_bytes(b"")  # stara baza, którą podmieniamy
    Path(str(target) + "-wal").write_bytes(foreign_wal)

    _replace_database(incoming, target)
    repo = LeadRepository(target)  # start bota: tryb WAL, ewentualne odtwarzanie WAL
    repo.close()

    assert dump(target) == expected
