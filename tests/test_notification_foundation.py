"""Fundament nowych kanałów: prawdziwe bazy, kopie i istniejące przepływy bota."""

import logging
import sqlite3
from dataclasses import FrozenInstanceError
from contextlib import closing

import pytest

import main
from gunb_tool import storage
from gunb_tool.bot_store import BotStore
from gunb_tool.config import ConfigError, load_config
from gunb_tool.migration import backup_now, export_state, import_state, restore_backup
from gunb_tool.storage import LeadRepository, SchemaTooNew
from tests.bot_helpers import MIETEK, lead
from tests.test_migration import TOKEN, WEBHOOK, db_of, dump, make_state

TABLES = {
    "notification_endpoints", "notification_outbox", "notification_deliveries",
    "notification_webhook_events",
}
NOW = "2026-10-06T06:00:00+00:00"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for key, value in (("TELEGRAM_BOT_TOKEN", TOKEN), ("TELEGRAM_ADMIN_CHAT_ID", "1001"),
                       ("ADMIN_CHAT_ID", "1001"), ("ADMIN_CONTACT", "@admin_gunb"),
                       ("DISCORD_WEBHOOK_URL", WEBHOOK), ("TELEGRAM_CHAT_ID", "")):
        monkeypatch.setenv(key, value)
    yield
    # main.main konfiguruje logi do strumienia pytest; nie zostawiaj zamkniętego strumienia.
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_gunb_tool", False):
            root.removeHandler(handler)
            handler.close()


@pytest.fixture
def legacy_state(tmp_path, clock, monkeypatch):
    """Pełny stan bota zapisany przez prawdziwy kod schematu v12."""
    with monkeypatch.context() as legacy:
        legacy.setattr(storage, "_MIGRATIONS", storage._MIGRATIONS[:12])
        config = make_state(tmp_path / "source", clock)
        with LeadRepository(db_of(config), now=clock.now_utc) as repo:
            repo.connection.execute(
                """INSERT INTO zamowienia (chat_id, oferta, cena, waluta, podatek, do_zaplaty,
                   opis_ceny, dni, utworzono, zmieniono) VALUES (?, '{}', '100', 'PLN',
                   'brutto', '100', '100 PLN', 30, ?, ?)""", (MIETEK, NOW, NOW),
            )
            repo.connection.execute(
                "INSERT INTO wyniki (chat_id, id_sprawy, wynik, zmieniono) VALUES (?, 'A/1', 'kontakt', ?)",
                (MIETEK, NOW),
            )
    return config


@pytest.fixture
def channel_repo(repo):
    BotStore(repo).register(MIETEK, "Odbiorca", None, status="aktywny", backlog_days=7)
    repo.upsert(lead("A/1"))
    return repo


def endpoint(conn, channel="email", address="odbiorca@example.test"):
    return conn.execute(
        """INSERT INTO notification_endpoints (chat_id, channel, address, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""", (MIETEK, channel, address, NOW, NOW),
    ).lastrowid


def queued(conn, endpoint_id, part=0):
    return conn.execute(
        """INSERT INTO notification_outbox
           (endpoint_id, event_key, part, payload, next_attempt_at, created_at, updated_at)
           VALUES (?, 'report:2026-10-06:rano', ?, '{"text":"Dom & hala"}', ?, ?, ?)""",
        (endpoint_id, part, NOW, NOW, NOW),
    ).lastrowid


def processed(conn, endpoint_id, outbox_id=None):
    conn.execute(
        """INSERT INTO notification_deliveries
           (endpoint_id, id_sprawy, revision, kind, outcome, processed_at, outbox_id)
           VALUES (?, 'A/1', 'revision-1', 'report', 'accepted', ?, ?)""",
        (endpoint_id, NOW, outbox_id),
    )


def webhook(conn, endpoint_id=None, channel="whatsapp"):
    conn.execute(
        """INSERT INTO notification_webhook_events
           (channel, event_key, endpoint_id, provider_message_id, payload, received_at)
           VALUES (?, 'provider-event-1', ?, 'provider-message-1', '{}', ?)""",
        (channel, endpoint_id, NOW),
    )


def version(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def test_v12_migration_keeps_all_data_and_creates_one_restorable_backup(legacy_state, clock, monkeypatch):
    path = db_of(legacy_state)
    before = dump(path)
    assert version(path) == 12
    assert all(before[t] for t in ("deliveries", "wysylki", "bot_users", "zamowienia", "wyniki"))

    with LeadRepository(path, now=clock.now_utc) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert not repo.connection.execute("PRAGMA foreign_key_check").fetchall()
    after = dump(path)
    assert {t: after[t] for t in before} == before
    assert set(after) - set(before) == TABLES
    assert all(after[t] == [] for t in TABLES)
    backups = list((path.parent / "backups").glob("*-przed-v13-*.sqlite"))
    assert len(backups) == 1
    assert version(backups[0]) == 12 and dump(backups[0]) == before

    with LeadRepository(path, now=clock.now_utc):
        pass
    assert list((path.parent / "backups").glob("*-przed-v13-*.sqlite")) == backups
    assert dump(path) == after

    replaced = restore_backup(backups[0], legacy_state, now=clock.now_utc)
    assert replaced is not None and dump(replaced) == after
    assert version(path) == 12 and dump(path) == before
    with monkeypatch.context() as legacy:
        legacy.setattr(storage, "_MIGRATIONS", storage._MIGRATIONS[:12])
        with LeadRepository(path, now=clock.now_utc):
            pass  # kod znający v12 może otworzyć odtworzoną kopię


def test_failed_backup_does_not_start_v13_migration(legacy_state, tmp_path):
    path = db_of(legacy_state)
    before = dump(path)
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("zajęte", encoding="utf-8")
    with pytest.raises(OSError):
        LeadRepository(path, backup_dir=blocked)
    assert version(path) == 12 and dump(path) == before


def test_partial_v13_migration_rolls_back_every_new_table(legacy_state, monkeypatch):
    assert storage.SCHEMA_VERSION == 13
    path = db_of(legacy_state)
    before = dump(path)
    broken = storage._MIGRATIONS[:-1] + (storage._MIGRATIONS[-1] + "\nSELECT missing_migration_column;",)
    monkeypatch.setattr(storage, "_MIGRATIONS", broken)
    with pytest.raises(sqlite3.OperationalError, match="missing_migration_column"):
        LeadRepository(path)
    assert version(path) == 12 and dump(path) == before
    (backup,) = (path.parent / "backups").glob("*-przed-v13-*.sqlite")
    assert version(backup) == 12 and dump(backup) == before


def test_old_code_refuses_v13_without_modifying_it(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite"
    with LeadRepository(path):
        pass
    assert version(path) == 13
    before = dump(path)
    monkeypatch.setattr(storage, "_MIGRATIONS", storage._MIGRATIONS[:12])
    with pytest.raises(SchemaTooNew, match="v13"):
        LeadRepository(path)
    assert version(path) == 13 and dump(path) == before


def test_endpoint_is_disabled_until_verified_and_consented(channel_repo):
    conn = channel_repo.connection
    ident = endpoint(conn)
    row = conn.execute("SELECT * FROM notification_endpoints WHERE id = ?", (ident,)).fetchone()
    assert row["enabled"] == 0 and row["version"] == 1 and row["mode"] == "rano"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE notification_endpoints SET enabled = 1 WHERE id = ?", (ident,))
    conn.execute("UPDATE notification_endpoints SET verified_at = ? WHERE id = ?", (NOW, ident))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE notification_endpoints SET enabled = 1 WHERE id = ?", (ident,))
    conn.execute("UPDATE notification_endpoints SET consent_at = ?, enabled = 1 WHERE id = ?", (NOW, ident))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE notification_endpoints SET consent_revoked_at = ? WHERE id = ?", (NOW, ident))
    conn.execute("UPDATE notification_endpoints SET enabled = 0, consent_revoked_at = ? WHERE id = ?",
                 (NOW, ident))


def test_endpoints_are_unique_per_owner_channel_address(channel_repo):
    conn = channel_repo.connection
    email = endpoint(conn)
    with pytest.raises(sqlite3.IntegrityError):
        endpoint(conn)
    whatsapp = endpoint(conn, "whatsapp", "+48123456789")
    assert email != whatsapp
    with pytest.raises(sqlite3.IntegrityError):
        endpoint(conn, "sms", "+48123456789")
    with pytest.raises(sqlite3.IntegrityError):
        endpoint(conn, address="   ")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE notification_endpoints SET version = 0 WHERE id = ?", (email,))


def test_outbox_deduplicates_each_part_per_endpoint(channel_repo):
    conn = channel_repo.connection
    email = endpoint(conn)
    whatsapp = endpoint(conn, "whatsapp", "+48123456789")
    ident = queued(conn, email)
    row = conn.execute("SELECT * FROM notification_outbox WHERE id = ?", (ident,)).fetchone()
    assert row["state"] == "queued" and row["attempts"] == 0
    assert row["payload"] == '{"text":"Dom & hala"}' and row["next_attempt_at"] == NOW
    with pytest.raises(sqlite3.IntegrityError):
        queued(conn, email)
    queued(conn, email, part=1)
    queued(conn, whatsapp)
    with pytest.raises(sqlite3.IntegrityError):
        queued(conn, email, part=-1)
    with pytest.raises(sqlite3.IntegrityError):
        queued(conn, 999999)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE notification_outbox SET attempts = -1 WHERE id = ?", (ident,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE notification_outbox SET state = 'typo' WHERE id = ?", (ident,))


def test_delivery_history_is_independent_of_telegram_and_other_endpoints(channel_repo):
    conn = channel_repo.connection
    store = BotStore(channel_repo)
    previous = [item.id_sprawy for item in store.candidates(MIETEK, "2000-01-01")]
    email = endpoint(conn)
    processed(conn, email)
    with pytest.raises(sqlite3.IntegrityError):
        processed(conn, email)
    processed(conn, endpoint(conn, "whatsapp", "+48123456789"))
    processed(conn, endpoint(conn, address="drugi@example.test"))
    assert conn.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 3
    assert [item.id_sprawy for item in store.candidates(MIETEK, "2000-01-01")] == previous == ["A/1"]
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


def test_pruning_outbox_keeps_delivery_history(channel_repo):
    conn = channel_repo.connection
    ident = endpoint(conn)
    outbox_id = queued(conn, ident)
    processed(conn, ident, outbox_id)
    conn.execute("DELETE FROM notification_outbox WHERE id = ?", (outbox_id,))
    row = conn.execute("SELECT * FROM notification_deliveries").fetchone()
    assert row["endpoint_id"] == ident and row["outbox_id"] is None and row["outcome"] == "accepted"


@pytest.mark.parametrize("delete_owner", [False, True])
def test_removing_endpoint_or_account_removes_dependent_channel_data(channel_repo, delete_owner):
    conn = channel_repo.connection
    ident = endpoint(conn, "whatsapp", "+48123456789")
    processed(conn, ident, queued(conn, ident))
    webhook(conn, ident)
    if delete_owner:
        conn.execute("DELETE FROM bot_users WHERE chat_id = ?", (MIETEK,))
    else:
        conn.execute("DELETE FROM notification_endpoints WHERE id = ?", (ident,))
    assert all(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0 for table in TABLES)
    assert channel_repo.get("A/1") is not None
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()


def test_webhook_events_deduplicate_before_linking_to_an_endpoint(channel_repo):
    conn = channel_repo.connection
    webhook(conn)
    with pytest.raises(sqlite3.IntegrityError):
        webhook(conn)
    webhook(conn, channel="email")
    assert conn.execute("SELECT COUNT(*) FROM notification_webhook_events").fetchone()[0] == 2
    assert all(row[0] is None for row in conn.execute("SELECT processed_at FROM notification_webhook_events"))


def test_filled_notification_state_survives_export_import_and_backup(legacy_state, tmp_path, clock):
    path = db_of(legacy_state)
    with LeadRepository(path, now=clock.now_utc) as repo:
        email = endpoint(repo.connection)
        whatsapp = endpoint(repo.connection, "whatsapp", "+48123456789")
        processed(repo.connection, email, queued(repo.connection, email))
        queued(repo.connection, whatsapp)
        webhook(repo.connection, whatsapp)
    before = dump(path)
    assert all(before[table] for table in TABLES)
    package = export_state(legacy_state, tmp_path / "packages", now=clock.now_utc)
    target = tmp_path / "target" / "config.yaml"
    imported = import_state(package, target, now=clock.now_utc)
    assert imported.schema_version == 13 and dump(imported.database) == before
    assert all(imported.counts[t] == len(before[t]) for t in TABLES)
    backup = backup_now(target, now=clock.now_utc)
    with LeadRepository(imported.database) as repo:
        repo.connection.execute("DELETE FROM notification_endpoints")
    replaced = restore_backup(backup, target, now=clock.now_utc)
    assert replaced is not None
    assert dump(imported.database) == before


def config_file(tmp_path, extra=""):
    path = tmp_path / "config.yaml"
    path.write_text("gunb:\n  voivodeships: ['28']\nstorage:\n  db_path: state.sqlite\n" + extra,
                    encoding="utf-8")
    return path


def test_old_config_defaults_both_channels_off_and_keeps_cli_working(tmp_path):
    path = config_file(tmp_path)
    config = load_config(path, env={})
    assert not config.email.enabled and not config.whatsapp.enabled
    with pytest.raises(FrozenInstanceError):
        config.email.enabled = True
    assert main.main(["--config", str(path), "--stats"]) == 0
    assert version(tmp_path / "state.sqlite") == 13


@pytest.mark.parametrize("channel", ["email", "whatsapp"])
@pytest.mark.parametrize("value", ["false", "'false'", "0", "'nie'", "'off'"])
def test_explicit_disabled_channel_uses_existing_boolean_rules(tmp_path, caplog, channel, value):
    config = load_config(config_file(tmp_path, f"{channel}:\n  enabled: {value}\n"), env={})
    assert not getattr(config, channel).enabled
    assert "Nieznana sekcja" not in caplog.text


@pytest.mark.parametrize("channel", ["email", "whatsapp"])
@pytest.mark.parametrize("value", ["true", "'true'", "1", "'tak'", "'on'"])
def test_unavailable_sender_cannot_be_enabled(tmp_path, channel, value):
    with pytest.raises(ConfigError, match=rf"{channel}\.enabled.*niedostępn"):
        load_config(config_file(tmp_path, f"{channel}:\n  enabled: {value}\n"), env={})


@pytest.mark.parametrize("channel", ["email", "whatsapp"])
def test_enabling_unavailable_channel_fails_before_opening_database(tmp_path, capsys, channel):
    path = config_file(tmp_path, f"{channel}:\n  enabled: true\n")
    assert main.main(["--config", str(path), "--stats"]) == 2
    assert f"{channel}.enabled" in capsys.readouterr().err
    assert not (tmp_path / "state.sqlite").exists()


@pytest.mark.parametrize("channel", ["email", "whatsapp"])
@pytest.mark.parametrize("extra", ["enabled: perhaps", "enabled: []", "enabled: null"])
def test_invalid_channel_flag_is_rejected(tmp_path, channel, extra):
    with pytest.raises(ConfigError, match=rf"{channel}\.enabled"):
        load_config(config_file(tmp_path, f"{channel}:\n  {extra}\n"), env={})
