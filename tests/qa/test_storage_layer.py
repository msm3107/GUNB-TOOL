"""Warstwa zapisu (SQLite): UPSERT z cyklem życia sprawy i współbieżność scrapera z botem w trybie WAL."""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from gunb_tool.bot_store import BotStore
from gunb_tool.storage import ChangeType, LeadRepository


class Ticking:
    """Zegar bazy przesuwany ręcznie – kolejne zapisy mają różne znaczniki czasu, jak w produkcji."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, minutes: int = 5) -> None:
        self.now += timedelta(minutes=minutes)


# --- UPSERT: „Wniosek” → „Decyzja” ---------------------------------------------------------------------

def test_status_change_updates_the_same_row_and_flags_it_for_sending(make_lead):
    clock = Ticking()
    with LeadRepository(":memory:", now=clock) as repo:  # sqlite:///:memory: – czysta baza na test
        wniosek = make_lead(status="wniosek", data_decyzji=None)
        assert repo.upsert(wniosek).change is ChangeType.NEW
        repo.mark_sent(wniosek.id_sprawy, "telegram")
        assert repo.pending_notifications("telegram", limit=10) == []
        clock.tick()

        result = repo.upsert(make_lead(status="decyzja", data_decyzji="2026-09-25"))

        assert result.change is ChangeType.STATUS_CHANGED
        assert (result.old_status, result.new_status) == ("wniosek", "decyzja")
        count, status, sent, channels, nr = repo.connection.execute(
            "SELECT COUNT(*), status, czy_wyslano, wyslano_kanaly, nr FROM investments").fetchone()
        assert count == 1  # ten sam rekord – bez duplikatu
        assert (status, sent, channels, nr) == ("decyzja", 0, "", 1)  # flaga „do wysłania”, numer leada ten sam
        assert [lead.status for lead in repo.pending_notifications("telegram", limit=10)] == ["decyzja"]
        history = [(h.stary_status, h.nowy_status) for h in repo.status_history(wniosek.id_sprawy)]
        assert history == [(None, "wniosek"), ("wniosek", "decyzja")]


def test_status_change_is_offered_again_to_bot_users(make_lead):
    clock = Ticking()
    with LeadRepository(":memory:", now=clock) as repo:
        store = BotStore(repo)
        store.register(101, "Ekipa", None, status="aktywny", backlog_days=7)
        repo.upsert(make_lead(status="wniosek", data_decyzji=None))
        [first] = store.candidates(101, "2000-01-01")
        store.record_delivery(101, [first], "raport")
        assert store.candidates(101, "2000-01-01") == []
        clock.tick()

        repo.upsert(make_lead(status="decyzja", data_decyzji="2026-09-25"))

        [again] = store.candidates(101, "2000-01-01")  # nowa rewizja – klient dostaje „🔄 ZMIANA STATUSU”
        assert again.status == "decyzja"


def test_identical_reimport_changes_nothing(memory_repo, make_lead):
    memory_repo.upsert(make_lead())
    memory_repo.mark_sent(make_lead().id_sprawy, "telegram")

    result = memory_repo.upsert(make_lead())

    assert result.change is ChangeType.UNCHANGED
    assert memory_repo.pending_notifications("telegram", limit=10) == []
    assert len(memory_repo.status_history(make_lead().id_sprawy)) == 1


# --- Współbieżność: scraper zapisuje, bot czyta --------------------------------------------------------

def test_file_database_runs_in_wal_mode_with_busy_timeout(db_path):
    with LeadRepository(db_path) as repo:
        assert repo.journal_mode == "wal"
        assert repo.busy_timeout_ms >= 1000


def test_bot_reads_committed_data_while_scraper_holds_open_write(db_path, make_lead):
    with LeadRepository(db_path) as setup:
        setup.upsert(make_lead("OLD/1"))
    writing, release, errors = threading.Event(), threading.Event(), []

    def scraper() -> None:
        try:
            with LeadRepository(db_path) as writer, writer.transaction():
                for i in range(50):
                    writer.upsert(make_lead(f"NEW/{i}"))
                writing.set()
                release.wait(10)  # transakcja otwarta – bot czyta w tym samym momencie
        except Exception as exc:  # noqa: BLE001 - raportujemy do asercji w wątku głównym
            errors.append(exc)

    thread = threading.Thread(target=scraper)
    thread.start()
    assert writing.wait(10)
    with LeadRepository(db_path) as reader:
        started = time.monotonic()
        during = reader.stats()["razem"]
        read_seconds = time.monotonic() - started
    release.set()
    thread.join(10)

    assert errors == []
    assert during == 1  # tylko zatwierdzone dane – bez „brudnego” odczytu połowy strony
    assert read_seconds < 1.0  # czytelnik nie czeka na zapis
    with LeadRepository(db_path) as after:
        assert after.stats()["razem"] == 51


def test_open_bot_read_does_not_block_scraper_commit(db_path, make_lead):
    with LeadRepository(db_path) as writer, LeadRepository(db_path) as reader:
        writer.upsert(make_lead("A/1"))
        reader.connection.execute("BEGIN")
        assert reader.connection.execute("SELECT COUNT(*) FROM investments").fetchone()[0] == 1

        started = time.monotonic()
        writer.upsert_many([make_lead(f"B/{i}") for i in range(20)])  # zatwierdzenie mimo trwającego odczytu

        assert time.monotonic() - started < 1.0
        assert reader.connection.execute("SELECT COUNT(*) FROM investments").fetchone()[0] == 1  # spójny obraz
        reader.connection.execute("COMMIT")
        assert reader.connection.execute("SELECT COUNT(*) FROM investments").fetchone()[0] == 21


def test_without_wal_the_same_situation_locks_the_database(tmp_path):
    """Kontrola: w trybie rollback journal czytelnik blokuje zapis – dlatego baza działa w WAL."""
    path = tmp_path / "rollback.sqlite"
    writer = sqlite3.connect(path, timeout=0.2, isolation_level=None)
    reader = sqlite3.connect(path, timeout=0.2, isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
        writer.execute("CREATE TABLE leady (x)")
        writer.execute("INSERT INTO leady VALUES (1)")
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM leady").fetchone()

        try:
            writer.execute("INSERT INTO leady VALUES (2)")
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc)
        else:
            raise AssertionError("bez WAL zapis powinien zostać zablokowany przez trwający odczyt")
    finally:
        reader.close()
        writer.close()


def test_concurrent_scraper_and_bot_threads_see_only_whole_pages(db_path, make_lead):
    with LeadRepository(db_path):
        pass  # schemat bazy
    done, errors, counts = threading.Event(), [], []

    def scraper() -> None:
        try:
            with LeadRepository(db_path) as repo:
                for page in range(15):  # 15 stron po 20 spraw – każda strona w jednej transakcji
                    repo.upsert_many([make_lead(f"S/{page}/{i}") for i in range(20)])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            done.set()

    def bot() -> None:
        try:
            with LeadRepository(db_path) as repo:
                store = BotStore(repo)
                while True:
                    counts.append(repo.connection.execute("SELECT COUNT(*) FROM investments").fetchone()[0])
                    store.candidates(1, "2000-01-01")  # to samo zapytanie co przy raporcie
                    if done.is_set():
                        break
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=bot), threading.Thread(target=scraper)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert errors == []  # żadnego „database is locked”
    assert counts and counts == sorted(counts)  # bot nigdy nie widzi „cofnięcia” danych
    assert all(count % 20 == 0 for count in counts)  # wyłącznie całe, zatwierdzone strony
    with LeadRepository(db_path) as repo:
        assert repo.stats()["razem"] == 300
