"""Higiena bazy: okresowa kopia (nocne porządki, przed pobieraniem) i VACUUM po dużym imporcie."""

import sqlite3
from datetime import date, timedelta

import pytest

from gunb_tool.maintenance import vacuum_after_import, backup_if_due
from gunb_tool.models import Investment
from gunb_tool.storage import LeadRepository

DAY = date(2026, 9, 29)


@pytest.fixture
def repo(tmp_path):
    repository = LeadRepository(tmp_path / "data" / "gunb_leads.sqlite")
    repository.upsert(Investment(id_sprawy="A/1", zrodlo="pozwolenia", status="decyzja"))
    yield repository
    repository.close()


def test_backup_is_a_dated_consistent_copy_of_the_database(repo, tmp_path):
    path = backup_if_due(repo, tmp_path / "backups", today=DAY)

    assert path == tmp_path / "backups" / "gunb_leads-2026-09-29.sqlite"
    copy = sqlite3.connect(path)
    try:
        assert copy.execute("SELECT id_sprawy FROM investments").fetchall() == [("A/1",)]
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        copy.close()


def test_backup_is_made_once_a_week(repo, tmp_path):
    backups = tmp_path / "backups"
    assert backup_if_due(repo, backups, today=DAY) is not None
    assert backup_if_due(repo, backups, today=DAY + timedelta(days=6)) is None
    assert backup_if_due(repo, backups, today=DAY + timedelta(days=7)) is not None


def test_only_newest_backups_are_kept(repo, tmp_path):
    backups = tmp_path / "backups"
    for week in range(5):
        backup_if_due(repo, backups, today=DAY + timedelta(weeks=week), keep=3)
    assert sorted(p.name for p in backups.glob("*.sqlite")) == [
        "gunb_leads-2026-10-13.sqlite", "gunb_leads-2026-10-20.sqlite", "gunb_leads-2026-10-27.sqlite",
    ]


def test_backup_problem_is_reported_but_does_not_stop_the_run(repo, tmp_path, caplog):
    not_a_directory = tmp_path / "backups"
    not_a_directory.write_text("plik zamiast katalogu", encoding="utf-8")
    with caplog.at_level("ERROR"):
        assert backup_if_due(repo, not_a_directory, today=DAY) is None
    assert "kopia" in caplog.text.lower()


def test_in_memory_database_is_not_backed_up(tmp_path):
    with LeadRepository(":memory:") as memory:
        assert backup_if_due(memory, tmp_path / "backups", today=DAY) is None


def test_vacuum_runs_only_after_a_big_import(repo):
    conn = repo.connection
    conn.execute("CREATE TABLE smieci (x BLOB)")
    conn.executemany("INSERT INTO smieci VALUES (randomblob(4000))", [()] * 300)
    conn.execute("DROP TABLE smieci")
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 0

    assert vacuum_after_import(repo, changed=10, threshold=500) is False
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 0

    assert vacuum_after_import(repo, changed=600, threshold=500) is True
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
    assert repo.get("A/1") is not None
