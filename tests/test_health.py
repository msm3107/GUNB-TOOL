"""Kontrola zdrowia (``main.py --zdrowie``): proces, pętle, import, wysyłki, baza i dysk – tylko do odczytu."""

import sqlite3
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import pytest

import gunb_tool.health as health
from gunb_tool.bot_store import BotStore
from gunb_tool.config import load_config
from gunb_tool.health import CRITICAL, OK, WARNING, check_health, ping
from gunb_tool.instance import instance_lock
from gunb_tool.storage import SCHEMA_VERSION, LeadRepository
from tests.test_storage import legacy_database

NOW = datetime(2026, 9, 30, 10, 0, tzinfo=timezone.utc)
CONFIG = "gunb:\n  voivodeships: ['12']\nstorage:\n  db_path: data/gunb_leads.sqlite\nbot:\n  fetch_times: ['06:30']\n"
Usage = namedtuple("Usage", "total used free")


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Zdrowy stan: świeże bicie serca obu pętli, udany import dziś rano, nic nie zalega; dużo miejsca."""
    monkeypatch.setattr(health.shutil, "disk_usage", lambda path: Usage(100 * 2**30, 10 * 2**30, 90 * 2**30))
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    repo = LeadRepository(tmp_path / "data" / "gunb_leads.sqlite", now=lambda: NOW)
    store = BotStore(repo)
    store.set_job_time("petla_odbioru", NOW - timedelta(seconds=40))
    store.set_job_time("watek_zadan", NOW - timedelta(seconds=20))
    store.set_job_time("import_udany", NOW - timedelta(hours=3))
    store.job_finished("import", "ok", "nowe 3")
    repo.close()
    return tmp_path


def run(state, **kwargs):
    config = load_config(state / "config.yaml")
    return check_health(config, now=NOW, **kwargs)


def change(state, action):
    repo = LeadRepository(state / "data" / "gunb_leads.sqlite", now=lambda: NOW)
    try:
        action(BotStore(repo), repo)
    finally:
        repo.close()


def text_of(checks):
    return "\n".join(check.text for check in checks)


def test_healthy_bot(state):
    with instance_lock(state / "data" / "gunb-bot.lock"):
        level, checks = run(state)
    assert level == OK, text_of(checks)
    assert "proces bota działa" in text_of(checks) and "ostatni pełny import" in text_of(checks)


def test_stopped_bot_is_critical(state):
    level, checks = run(state)
    assert level == CRITICAL and "nie działa" in text_of(checks)


def test_hung_receiving_loop_is_critical(state):
    change(state, lambda store, repo: store.set_job_time("petla_odbioru", NOW - timedelta(minutes=20)))
    with instance_lock(state / "data" / "gunb-bot.lock"):
        level, checks = run(state)
    assert level == CRITICAL and "odbierania" in text_of(checks)


def test_hung_jobs_thread_is_critical(state):
    change(state, lambda store, repo: store.set_job_time("watek_zadan", NOW - timedelta(minutes=20)))
    with instance_lock(state / "data" / "gunb-bot.lock"):
        level, checks = run(state)
    assert level == CRITICAL and "wątek zadań nie odpowiada" in text_of(checks)


def test_long_import_keeps_the_jobs_thread_counted_as_alive(state):
    def importing(store, repo):
        store.set_job_time("watek_zadan", NOW - timedelta(minutes=40))
        repo.acquire_lease("import", "watek", timedelta(minutes=30))
        store.job_started("import")

    change(state, importing)
    with instance_lock(state / "data" / "gunb-bot.lock"):
        level, checks = run(state)
    assert level == OK, text_of(checks)
    assert "import trwa" in text_of(checks)


@pytest.mark.parametrize("hours, expected", [(40, WARNING), (100, CRITICAL)])
def test_old_import_is_reported(state, hours, expected):
    change(state, lambda store, repo: store.set_job_time("import_udany", NOW - timedelta(hours=hours)))
    with instance_lock(state / "data" / "gunb-bot.lock"):
        level, checks = run(state)
    assert level == expected and "import" in text_of(checks)


def test_stuck_and_failed_sends_are_warnings(state):
    def sends(store, repo):
        store.enqueue_sends("raport_rano:2026-09-30", [1, 2])
        store.finish_send("raport_rano:2026-09-30", 2, "blad", blad="HTTP 502")
        repo.connection.execute("UPDATE wysylki SET nastepna_proba = ? WHERE chat_id = 1",
                                ((NOW - timedelta(hours=2)).isoformat(),))

    change(state, sends)
    with instance_lock(state / "data" / "gunb-bot.lock"):
        level, checks = run(state)
    assert level == WARNING
    assert "zalega" in text_of(checks) and "nieudane" in text_of(checks)


def test_token_conflict_is_critical(state):
    change(state, lambda store, repo: store.set_job_time("konflikt_telegrama", NOW - timedelta(minutes=2)))
    with instance_lock(state / "data" / "gunb-bot.lock"):
        level, checks = run(state)
    assert level == CRITICAL and "token" in text_of(checks)


@pytest.mark.parametrize("total_gb, free_mb, expected", [
    (10, 1500, WARNING),   # mniej niż 2 GB, choć to 15% dysku
    (100, 5000, WARNING),  # 5 GB, ale tylko 5% dysku
    (10, 100, CRITICAL),
])
def test_low_disk_space(state, monkeypatch, total_gb, free_mb, expected):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda path: Usage(total_gb * 2**30, 0, free_mb * 2**20))
    with instance_lock(state / "data" / "gunb-bot.lock"):
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
