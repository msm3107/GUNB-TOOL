"""Kontrola zdrowia (``main.py --zdrowie``): baza, dysk, stan importu – tylko do odczytu."""

import sqlite3
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

import core.health as health
from core.config import load_config
from core.health import CRITICAL, OK, WARNING, check_health, ping
from core.storage import SCHEMA_VERSION, LeadRepository
from tests.test_storage import legacy_database

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
CONFIG = "gunb:\n  voivodeships: ['12']\nstorage:\n  db_path: data/gunb_leads.sqlite\n"
Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda path: Usage(100 * 2**30, 10 * 2**30, 90 * 2**30))
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    repo = LeadRepository(tmp_path / "data" / "gunb_leads.sqlite", now=lambda: NOW)
    repo.connection.execute(
        "INSERT OR REPLACE INTO zadania (nazwa, ostatnio) VALUES (?, ?)",
        ("ostatni_import_gunb", (NOW - timedelta(hours=3)).isoformat()),
    )
    repo.connection.execute(
        "INSERT OR REPLACE INTO zadania (nazwa, ostatnio, stan, opis) VALUES (?, ?, ?, ?)",
        ("import_gunb", NOW.isoformat(), "ok", "nowe 3"),
    )
    repo.connection.commit()
    repo.close()
    return tmp_path


def run(state, **kwargs):
    config = load_config(state / "config.yaml")
    return check_health(config, now=NOW, **kwargs)


def text_of(checks):
    return "\n".join(check.text for check in checks)


def test_healthy_engine(state):
    level, checks = run(state)
    assert level == OK, text_of(checks)
    assert "ostatni pełny import" in text_of(checks)
    assert "baza cała" in text_of(checks)


@pytest.mark.parametrize("hours, expected", [(40, WARNING), (100, CRITICAL)])
def test_old_import_is_reported(state, hours, expected):
    repo = LeadRepository(state / "data" / "gunb_leads.sqlite", now=lambda: NOW)
    repo.connection.execute(
        "UPDATE zadania SET ostatnio = ? WHERE nazwa = 'ostatni_import_gunb'",
        ((NOW - timedelta(hours=hours)).isoformat(),),
    )
    repo.connection.commit()
    repo.close()

    level, checks = run(state)
    assert level == expected and "import" in text_of(checks)


@pytest.mark.parametrize("total_gb, free_mb, expected", [
    (10, 1500, WARNING),
    (100, 5000, WARNING),
    (10, 100, CRITICAL),
])
def test_low_disk_space(state, monkeypatch, total_gb, free_mb, expected):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda path: Usage(total_gb * 2**30, 0, free_mb * 2**20))
    level, checks = run(state)
    assert level == expected and "dysk" in text_of(checks)


def test_broken_database_is_critical(state):
    db = state / "data" / "gunb_leads.sqlite"
    for suffix in ("-wal", "-shm"):
        (state / "data" / f"gunb_leads.sqlite{suffix}").unlink(missing_ok=True)
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    level, checks = run(state)
    assert level == CRITICAL and "baz" in text_of(checks)


def test_health_check_never_migrates_or_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda path: Usage(100 * 2**30, 0, 90 * 2**30))
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "data").mkdir()
    legacy_database(tmp_path / "data" / "gunb_leads.sqlite", 6).close()
    before = (tmp_path / "data" / "gunb_leads.sqlite").read_bytes()

    level, checks = run(tmp_path)

    assert (tmp_path / "data" / "gunb_leads.sqlite").read_bytes() == before
    conn = sqlite3.connect(tmp_path / "data" / "gunb_leads.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    conn.close()
    assert f"v6 → v{SCHEMA_VERSION}" in text_of(checks)


def test_ping_reports_success_and_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(health.requests, "get", lambda url, timeout: calls.append(url))
    ping("https://hc-ping.com/abc", OK)
    ping("https://hc-ping.com/abc", WARNING)
    ping("https://hc-ping.com/abc", CRITICAL)
    assert calls == ["https://hc-ping.com/abc", "https://hc-ping.com/abc", "https://hc-ping.com/abc/fail"]
