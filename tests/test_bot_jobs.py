"""Zadania w tle: kolejka wysyłek z ponowieniami (P0.1) i harmonogram w czasie polskim (P0.2).

Zegar jest sterowany z testu (UTC); bot sam przelicza go na czas Europe/Warsaw.
"""

import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from gunb_tool.bot import JobsWorker
from gunb_tool.bot_store import BotStore
from gunb_tool.http_client import HttpError
from gunb_tool.pipeline import ImportSkipped
from gunb_tool.storage import LeadRepository
from gunb_tool.telegram_api import TelegramApiError
from tests.bot_helpers import ADMIN, MIETEK, OBCY, FakeApi, activate, click, lead, make_bot, message

TODAY_0701 = datetime(2026, 9, 29, 5, 1, tzinfo=timezone.utc)  # 07:01 czasu polskiego (CEST, UTC+2)
MORNING_JOB = "raport_rano:2026-09-29"
TRZECI = 4004


class Crash(BaseException):
    """Proces pada w trakcie wysyłki (np. restart serwera) – nie jest zwykłym błędem wysyłki."""


def server_error():
    return TelegramApiError("sendMessage", 502, "Bad Gateway")


def reports_to(api, chat_id):
    return [m for m in api.to(chat_id) if "📊 <b>Raport" in m["text"]]


@pytest.fixture
def bot(repo, api, clock):
    bot = make_bot(repo, api, clock)
    activate(bot, api, MIETEK)
    activate(bot, api, OBCY)
    repo.upsert(lead("A/1"))
    return bot


# --- P0.1: raport po nieudanej wysyłce ---------------------------------------------------------------

def test_failed_report_is_retried_and_other_recipients_are_not_held_up(bot, api, clock):
    api.fail(MIETEK, server_error())
    clock.utc = TODAY_0701

    bot.run_due_jobs()
    assert reports_to(api, MIETEK) == []
    assert len(reports_to(api, OBCY)) == 1  # błąd jednej osoby nie zatrzymuje pozostałych

    clock.advance(seconds=30)
    bot.run_due_jobs()
    assert reports_to(api, MIETEK) == []  # ponowienie dopiero po przerwie

    clock.advance(minutes=1)
    bot.run_due_jobs()
    assert len(reports_to(api, MIETEK)) == 1
    assert len(reports_to(api, OBCY)) == 1  # obsłużony odbiorca nie dostaje raportu drugi raz


def test_each_attempt_is_recorded_separately_from_the_job_start(bot, api, repo, clock):
    api.fail(MIETEK, server_error())
    clock.utc = TODAY_0701
    bot.run_due_jobs()
    bot.run_due_jobs()  # kolejny cykl nie tworzy zadania od nowa

    store = BotStore(repo)
    waiting = store.send_row(MORNING_JOB, MIETEK)
    assert (waiting.stan, waiting.proby) == ("oczekuje", 1)
    assert "502" in waiting.ostatni_blad
    done = store.send_row(MORNING_JOB, OBCY)
    assert (done.stan, done.proby) == ("wyslano", 1)


def test_report_retries_are_bounded_and_the_last_failure_is_reported(bot, api, repo, clock, caplog):
    api.fail(MIETEK, *[server_error() for _ in range(10)])
    clock.utc = TODAY_0701

    with caplog.at_level(logging.ERROR):
        for _ in range(12):
            bot.run_due_jobs()
            clock.advance(minutes=31)

    assert reports_to(api, MIETEK) == []
    assert len(api.failures[MIETEK]) == 5  # dokładnie 5 prób z 10 zaplanowanych awarii
    assert BotStore(repo).send_row(MORNING_JOB, MIETEK).stan == "blad"
    assert any(str(MIETEK) in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


def test_blocked_recipient_is_not_retried(bot, api, repo, clock):
    api.blocked.add(MIETEK)
    clock.utc = TODAY_0701
    bot.run_due_jobs()

    api.blocked.clear()
    clock.advance(minutes=20)
    bot.run_due_jobs()

    assert reports_to(api, MIETEK) == []
    assert BotStore(repo).send_row(MORNING_JOB, MIETEK).stan == "zablokowany"
    assert BotStore(repo).get_user(MIETEK).status == "zablokowany"


def test_restart_during_reports_sends_the_rest_exactly_once(repo, api, clock):
    bot = make_bot(repo, api, clock)
    for chat in (MIETEK, OBCY, TRZECI):
        activate(bot, api, chat)
    repo.upsert(lead("A/1"))
    api.fail(OBCY, Crash())
    clock.utc = TODAY_0701

    with pytest.raises(Crash):
        bot.run_due_jobs()  # MIETEK obsłużony, proces pada przy OBCY, TRZECI jeszcze czeka

    restarted = make_bot(repo, api, clock)
    clock.advance(minutes=6)
    restarted.run_due_jobs()

    assert [len(reports_to(api, chat)) for chat in (MIETEK, OBCY, TRZECI)] == [1, 1, 1]


def test_timeout_after_telegram_accepted_the_message_repeats_it_at_most_once(bot, api, repo, clock):
    """Timeout po przyjęciu wiadomości: bot nie wie, czy doszła. Woli ją powtórzyć (raport może przyjść
    dwa razy) niż ryzykować, że klient nie dostanie go wcale – „dokładnie raz” nie jest możliwe."""
    api.fail(MIETEK, TelegramApiError("sendMessage", None, "Read timed out"), delivered=True)
    clock.utc = TODAY_0701
    bot.run_due_jobs()
    clock.advance(minutes=2)
    bot.run_due_jobs()
    clock.advance(hours=1)
    bot.run_due_jobs()

    assert len(reports_to(api, MIETEK)) == 2
    row = BotStore(repo).send_row(MORNING_JOB, MIETEK)
    assert (row.stan, row.proby) == ("wyslano", 2)


def test_report_missed_during_a_short_outage_goes_out_when_the_bot_is_back(bot, api, clock):
    clock.utc = datetime(2026, 9, 29, 7, 0, tzinfo=timezone.utc)  # 09:00 – bot wrócił po awarii
    bot.run_due_jobs()
    assert len(reports_to(api, MIETEK)) == 1


def test_no_catch_up_report_at_night(bot, api, clock):
    clock.utc = datetime(2026, 9, 29, 20, 30, tzinfo=timezone.utc)  # 22:30 – bot wrócił po całodziennej awarii
    bot.run_due_jobs()
    assert reports_to(api, MIETEK) == []

    clock.utc = datetime(2026, 9, 30, 5, 1, tzinfo=timezone.utc)  # 07:01 następnego dnia
    bot.run_due_jobs()
    assert len(reports_to(api, MIETEK)) == 1


def test_retries_stop_before_the_night(bot, api, repo, clock):
    api.fail(MIETEK, *[server_error() for _ in range(3)])
    bot_evening = make_bot(repo, api, clock, evening_time="21:50")
    BotStore(repo).set_mode(MIETEK, "wieczor")
    clock.utc = datetime(2026, 9, 29, 19, 51, tzinfo=timezone.utc)  # 21:51
    bot_evening.run_due_jobs()
    clock.utc = datetime(2026, 9, 29, 20, 30, tzinfo=timezone.utc)  # 22:30 – cisza nocna
    bot_evening.run_due_jobs()
    assert reports_to(api, MIETEK) == []
    assert BotStore(repo).send_row("raport_wieczor:2026-09-29", MIETEK).stan == "pominieto"


# --- P0.2: harmonogram w czasie polskim, restart, zmiana czasu ----------------------------------------

def test_restart_does_not_repeat_todays_report(bot, api, repo, clock):
    clock.utc = TODAY_0701
    bot.run_due_jobs()
    repo.upsert(lead("B/1"))

    restarted = make_bot(repo, api, clock)
    clock.advance(minutes=30)
    restarted.run_due_jobs()

    assert len(reports_to(api, MIETEK)) == 1


@pytest.mark.parametrize("day, too_early, on_time", [
    # 25.10.2026: z czasu letniego (UTC+2) na zimowy (UTC+1) – 07:00 to 06:00 UTC
    ("2026-10-25", datetime(2026, 10, 25, 5, 30, tzinfo=timezone.utc), datetime(2026, 10, 25, 6, 1, tzinfo=timezone.utc)),
    # 28.03.2027: z zimowego na letni – 07:00 to 05:00 UTC
    ("2027-03-28", datetime(2027, 3, 28, 4, 30, tzinfo=timezone.utc), datetime(2027, 3, 28, 5, 1, tzinfo=timezone.utc)),
])
def test_morning_report_follows_polish_time_across_the_clock_change(repo, api, clock, day, too_early, on_time):
    bot = make_bot(repo, api, clock)
    activate(bot, api, ADMIN)  # admin ma dostęp bez abonamentu – test może przeskoczyć o miesiące
    clock.utc = too_early
    repo.upsert(lead("A/1"))
    bot.run_due_jobs()
    assert reports_to(api, ADMIN) == []

    clock.utc = on_time
    bot.run_due_jobs()
    clock.advance(hours=2)
    bot.run_due_jobs()
    assert len(reports_to(api, ADMIN)) == 1
    assert BotStore(repo).send_row(f"raport_rano:{day}", ADMIN).stan == "wyslano"


# --- P0.2: import w tle, stan widoczny dla admina ----------------------------------------------------

AT_0631 = datetime(2026, 9, 29, 4, 31, tzinfo=timezone.utc)  # 06:31 – pora importu z fetch_times


def test_buttons_are_answered_while_import_runs_in_the_background(tmp_path, api, clock):
    path = tmp_path / "bot.sqlite"
    ui_repo = LeadRepository(path, now=clock.now_utc)
    ui_bot = make_bot(ui_repo, api, clock)
    activate(ui_bot, api, MIETEK)
    ui_repo.upsert(lead("A/1"))
    import_started, finish_import = threading.Event(), threading.Event()

    def slow_fetcher():
        import_started.set()
        assert finish_import.wait(10)
        return "nowe 0"

    def make_jobs_bot():  # osobne połączenie z bazą w wątku zadań
        jobs_repo = LeadRepository(path, now=clock.now_utc)
        jobs_bot = make_bot(jobs_repo, api, clock, fetch_times=("06:30",))
        jobs_bot.fetcher = slow_fetcher
        return jobs_bot, jobs_repo.close

    clock.utc = AT_0631
    stop = threading.Event()
    worker = JobsWorker(make_jobs_bot, stop, every=0.05)
    worker.start()
    try:
        assert import_started.wait(5)
        ui_bot.handle_update(click(MIETEK, f"s1:{ui_repo.get('A/1').nr}"))  # kliknięcie w trakcie importu

        assert api.answers[-1].startswith("⭐ Zapisano")
        assert BotStore(ui_repo).lead_flags(MIETEK, "A/1").saved
        assert BotStore(ui_repo).job_status("import").stan == "trwa"
    finally:
        finish_import.set()
        stop.set()
        worker.join(5)
    assert not worker.is_alive()
    assert BotStore(ui_repo).job_status("import").stan == "ok"
    ui_repo.close()


def test_admin_status_shows_import_and_sends(repo, api, clock):
    bot = make_bot(repo, api, clock, fetch_times=("06:30",))
    bot.fetcher = lambda: "nowe 3, zmiany statusu 1, sprawy 40"
    activate(bot, api, MIETEK)
    activate(bot, api, OBCY)
    repo.upsert(lead("A/1"))
    api.fail(MIETEK, server_error())
    clock.utc = TODAY_0701
    bot.run_due_jobs()

    bot.handle_update(message(ADMIN, "/status"))

    text = api.last_to(ADMIN)["text"]
    assert "Import danych GUNB: ✅" in text and "nowe 3" in text
    assert "Raport poranny 29.09" in text and "wysłano 1" in text and "czeka na ponowienie 1" in text
    assert "Wątek zadań" in text


def test_failed_import_is_visible_to_admin_with_retry_time(repo, api, clock):
    bot = make_bot(repo, api, clock, fetch_times=("06:30",))

    def failing_fetcher():
        raise HttpError("GUNB: HTTP 503", status_code=503)

    bot.fetcher = failing_fetcher
    clock.utc = AT_0631
    bot.run_due_jobs()

    bot.handle_update(message(ADMIN, "/status"))

    text = api.last_to(ADMIN)["text"]
    assert "Import danych GUNB: ❌" in text and "HTTP 503" in text
    assert "ponowienie o 07:31" in text


def test_import_skipped_while_another_runs_is_retried_later(repo, api, clock):
    bot = make_bot(repo, api, clock, fetch_times=("06:30",))
    calls = []

    def busy_then_ok():
        calls.append(1)
        if len(calls) == 1:
            raise ImportSkipped("trwa inny import danych (import historii)")
        return "nowe 1"

    bot.fetcher = busy_then_ok
    clock.utc = AT_0631
    bot.run_due_jobs()
    assert BotStore(repo).job_status("import").stan == "pominieto"

    clock.advance(hours=1, minutes=1)
    bot.run_due_jobs()
    assert len(calls) == 2 and BotStore(repo).job_status("import").stan == "ok"


def test_status_is_only_for_admin(bot, api):
    bot.handle_update(message(MIETEK, "/status"))
    assert "Stan bota" not in api.last_to(MIETEK)["text"]


def test_import_killed_midway_is_retried_right_after_the_restart(repo, api, clock):
    calls = []

    def killed():  # proces ginie w trakcie importu (kill -9, brak prądu)
        calls.append(1)
        raise Crash()

    bot = make_bot(repo, api, clock, fetch_times=("06:30",))
    bot.fetcher = killed
    clock.utc = AT_0631
    with pytest.raises(Crash):
        bot.run_due_jobs()

    restarted = make_bot(repo, api, clock, fetch_times=("06:30",))
    restarted.fetcher = lambda: calls.append(1) or "nowe 0"
    restarted.recover_interrupted_import()
    restarted.run_due_jobs()

    assert len(calls) == 2
    assert BotStore(repo).job_status("import").stan == "ok"


def test_import_killed_while_its_lock_is_still_valid_is_retried_within_an_hour(repo, api, clock):
    calls = []
    clock.utc = AT_0631

    def killed():  # import wziął blokadę (jak fetch_with_maintenance) i proces zginął – blokada zostaje
        calls.append(1)
        repo.acquire_lease("import", "zabity-proces", timedelta(minutes=30))
        raise Crash()

    bot = make_bot(repo, api, clock, fetch_times=("06:30",))
    bot.fetcher = killed
    with pytest.raises(Crash):
        bot.run_due_jobs()

    restarted = make_bot(repo, api, clock, fetch_times=("06:30",))
    restarted.fetcher = lambda: calls.append(1) or "nowe 0"
    restarted.recover_interrupted_import()  # blokada jeszcze ważna – nie wiadomo, czy tamten proces żyje
    clock.advance(minutes=40)
    restarted.run_due_jobs()
    assert len(calls) == 1
    clock.advance(minutes=21)  # godzina po starcie przerwanego importu, blokada dawno wygasła
    restarted.run_due_jobs()
    assert len(calls) == 2


def test_scheduled_import_leaves_another_running_import_alone(repo, api, clock):
    store = BotStore(repo)
    repo.acquire_lease("import", "import-historii", timedelta(hours=1))
    store.job_started("import")  # stan zapisany przez tamten proces
    started = store.job_status("import").start
    calls = []
    bot = make_bot(repo, api, clock, fetch_times=("06:30",))
    bot.fetcher = lambda: calls.append(1) or "nowe 0"
    clock.utc = AT_0631

    bot.run_due_jobs()

    assert calls == []
    assert (store.job_status("import").stan, store.job_status("import").start) == ("trwa", started)
    assert store.job_time("pobieranie_ponow") is not None


# --- Odbieranie wiadomości a chwilowo zajęta baza (np. VACUUM w wątku zadań) --------------------------------

class PollingApi(FakeApi):
    """Jak Telegram: oddaje aktualizacje od ``offset`` – nieprzesunięty offset = ta sama aktualizacja jeszcze raz."""

    def __init__(self, updates):
        super().__init__()
        self.updates = updates

    def get_updates(self, offset, timeout):
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]


def numbered(update_id, chat_id, text):
    update = message(chat_id, text)
    update["update_id"] = update_id
    return update


def test_update_hit_by_a_locked_database_is_handled_again(repo, clock):
    api = PollingApi([numbered(1, MIETEK, "/start"), numbered(2, MIETEK, "/pomoc")])
    bot = make_bot(repo, api, clock)
    real_handle = bot.handle_update
    locked_once = [2]

    def flaky(update):
        if update["update_id"] in locked_once:
            locked_once.remove(update["update_id"])
            raise sqlite3.OperationalError("database is locked")
        real_handle(update)

    bot.handle_update = flaky
    bot.poll_once(0)
    bot.poll_once(0)

    assert "Jak to działa" in api.last_to(MIETEK)["text"]  # druga aktualizacja nie przepadła
    assert bot.poll_once(0) == 0  # i nie jest obsługiwana w kółko


def test_receiving_loop_survives_a_locked_database(repo, clock, monkeypatch):
    api = PollingApi([numbered(1, MIETEK, "/start"), numbered(2, MIETEK, "/pomoc")])
    bot = make_bot(repo, api, clock)
    real_mark = bot.store.mark_job
    failures = [sqlite3.OperationalError("database is locked")]

    def flaky_mark(name, value):
        if failures:
            raise failures.pop()
        real_mark(name, value)

    monkeypatch.setattr(bot.store, "mark_job", flaky_mark)
    polls = []

    bot.run_forever(should_stop=lambda: len(polls) >= 3 or bool(polls.append(1)), sleep=lambda seconds: None)

    assert "Jak to działa" in api.last_to(MIETEK)["text"]
    assert BotStore(repo).job_last_run("telegram_offset") == "3"
