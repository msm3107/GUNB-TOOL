import logging
import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import main
from gunb_tool.config import FilterConfig, LoggingConfig
from gunb_tool.data_filter import LeadFilter
from gunb_tool.models import Investment
from gunb_tool.bot_store import BotStore
from gunb_tool.http_client import HttpError
from gunb_tool.pipeline import IMPORT_LEASE, ImportSkipped, LeadPipeline
from gunb_tool.storage import LeadRepository
from tests.test_pipeline import FakeScraper, gunb_case


def test_log_output_never_contains_the_token_even_in_tracebacks(tmp_path):
    token = "123456:SECRET-token_x"
    log_file = tmp_path / "bot.log"
    main.setup_logging(LoggingConfig(level="INFO", file=log_file), secrets=(token,))
    try:
        try:
            try:
                raise ConnectionError("Max retries exceeded with url: /bot123456:SECRET-token_x/sendMessage")
            except ConnectionError as exc:
                raise RuntimeError(f"wysyłka nie powiodła się (token {token})") from exc
        except RuntimeError:
            logging.getLogger("gunb_tool.test").exception("Błąd wysyłki")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
    finally:
        for handler in [h for h in logging.getLogger().handlers if getattr(h, "_gunb_tool", False)]:
            logging.getLogger().removeHandler(handler)
            handler.close()

    assert "Błąd wysyłki" in text and "Traceback" in text
    assert "SECRET" not in text and "<token>" in text


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Katalog z minimalną konfiguracją; bez dostępu do prawdziwych sekretów z otoczenia."""
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_ADMIN_CHAT_ID", "DISCORD_WEBHOOK_URL",
                 "GOOGLE_SHEET_ID"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(
        "gunb:\n  voivodeships: ['16']\n  powiats: ['1607']\n"
        "storage:\n  db_path: data/test.sqlite\n"
        "telegram:\n  bot_token: ${TELEGRAM_BOT_TOKEN}\n  chat_id: ${TELEGRAM_CHAT_ID}\n"
        "  admin_chat_id: ${TELEGRAM_ADMIN_CHAT_ID}\n"
        "logging:\n  level: INFO\n",
        encoding="utf-8",
    )
    return tmp_path


def run(workdir: Path, *args: str) -> int:
    return main.main(["--config", str(workdir / "config.yaml"), *args])


def seed(workdir: Path) -> None:
    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        repo.upsert(Investment(id_sprawy="A/1", zrodlo="pozwolenia", status="decyzja",
                               nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego",
                               kategoria="mieszkaniowa-jednorodzinna"))


def test_without_actions_prints_help_and_returns_usage_error(workdir, capsys):
    assert run(workdir) == 2
    assert "--fetch" in capsys.readouterr().out


def test_missing_config_file_is_reported(tmp_path, capsys):
    assert main.main(["--config", str(tmp_path / "brak.yaml"), "--stats"]) == 2
    assert "Brak pliku konfiguracyjnego" in capsys.readouterr().err


def test_invalid_cli_scope_is_a_config_error(workdir, capsys):
    assert run(workdir, "--fetch", "--voivodeship", "99") == 2
    assert "województwo" in capsys.readouterr().err


def test_stats_on_empty_database(workdir, capsys):
    assert run(workdir, "--stats") == 0
    assert "Leady w bazie: 0" in capsys.readouterr().out


def test_fetch_uses_cli_scope_and_prints_summary(workdir, monkeypatch, capsys):
    scraper = FakeScraper([gunb_case("A/1"), gunb_case("B/1", "Budowa ogrodzenia", "VIII")])

    def fake_create_pipeline(config, repo, *, geocode=True):
        assert geocode is False
        assert config.gunb.powiats == ("1661",)
        return LeadPipeline(repo, scraper=scraper, lead_filter=LeadFilter(FilterConfig()))

    monkeypatch.setattr(main, "create_pipeline", fake_create_pipeline)

    code = run(workdir, "--fetch", "--no-geocode", "--powiat", "1661", "--since", "2026-09-01", "--limit", "10")

    assert code == 0
    query, _ = scraper.queries[0]
    assert str(query.date_from) == "2026-09-01"
    out = capsys.readouterr().out
    assert "nowe 1" in out
    assert "Odrzucono 1" in out


def test_dry_run_notification_works_without_credentials(workdir, capsys):
    seed(workdir)
    assert run(workdir, "--notify-telegram", "--dry-run") == 0
    assert "NOWA INWESTYCJA" in capsys.readouterr().out


def test_real_notification_without_credentials_fails(workdir, caplog):
    seed(workdir)
    assert run(workdir, "--notify-telegram") == 1
    assert "TELEGRAM_BOT_TOKEN" in caplog.text


def test_mark_sent_baselines_queue(workdir, capsys):
    seed(workdir)
    assert run(workdir, "--mark-sent") == 0
    assert "Oznaczono jako wysłane: 1" in capsys.readouterr().out
    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        assert repo.pending_notifications("telegram", limit=10) == []


def test_sync_sheets_without_configuration_fails_cleanly(workdir, caplog):
    seed(workdir)
    assert run(workdir, "--sync-sheets") == 1
    assert "GOOGLE_SHEET_ID" in caplog.text


def test_max_leads_option_and_legacy_alias_limit_dry_run(workdir, capsys):
    seed(workdir)
    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        repo.upsert(Investment(id_sprawy="B/2", zrodlo="pozwolenia", status="decyzja", kategoria="komercyjna",
                               nazwa_zamierzenia="Budowa hali magazynowej"))
    assert run(workdir, "--notify-telegram", "--dry-run", "--max-leads", "1") == 0
    assert "wiadomości 1, leady 1" in capsys.readouterr().out
    assert run(workdir, "--notify-discord", "--dry-run", "--max-messages", "2") == 0
    assert "wiadomości 2, leady 2" in capsys.readouterr().out


# --- Bot ---------------------------------------------------------------------------------------

def test_bot_requires_token(workdir, caplog):
    assert run(workdir, "--bot-once") == 1
    assert "TELEGRAM_BOT_TOKEN" in caplog.text


def test_bot_once_runs_single_cycle(workdir, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    calls = {}

    class Api:
        def __init__(self, http, token, **kwargs):
            calls["token"] = token

        def set_my_commands(self, commands):
            calls["commands"] = len(commands)

        def get_updates(self, offset, timeout):
            calls["timeout"] = timeout
            return []

    monkeypatch.setattr(main, "TelegramApi", Api)
    assert run(workdir, "--bot-once") == 0
    assert calls == {"token": "123:ABC", "commands": 5, "timeout": 0}
    assert "Bot:" in capsys.readouterr().out


def test_bot_starts_with_a_broken_offer_and_tells_the_admin(workdir, monkeypatch, caplog):
    """Literówka w cenie nie zatrzymuje bota (systemd nie wznawia kodu 2) – oferta jest wyłączona, a błąd
    (poziom ERROR = alert na czat admina) mówi, którą zmienną poprawić."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    config = workdir / "config.yaml"
    config.write_text(config.read_text(encoding="utf-8") + "bot:\n  offer:\n    price: \"99 zł\"\n",
                      encoding="utf-8")

    class Api:
        def __init__(self, *args, **kwargs):
            pass

        def set_my_commands(self, commands):
            pass

        def get_updates(self, offset, timeout):
            return []

    monkeypatch.setattr(main, "TelegramApi", Api)
    assert run(workdir, "--bot-once") == 0
    assert any(r.levelname == "ERROR" and "OFERTA_CENA='99 zł'" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("mode", ["--bot", "--bot-once"])
def test_second_bot_on_the_same_data_exits_without_touching_telegram(workdir, monkeypatch, caplog, mode):
    from gunb_tool.instance import instance_lock

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")

    class Api:
        def __init__(self, *args, **kwargs):
            raise AssertionError("drugi proces nie może łączyć się z Telegramem")

    monkeypatch.setattr(main, "TelegramApi", Api)
    with instance_lock(workdir / "data" / "gunb-bot.lock"):  # pierwszy bot już działa
        code = run(workdir, mode)

    assert code == main.EXIT_ALREADY_RUNNING == 3
    assert "już działa" in caplog.text


# --- Utwardzenie: kopia bazy, alerty admina, awarie ------------------------------------------------

def fake_pipeline_factory(cases):
    def factory(config, repo, *, geocode=True):
        return LeadPipeline(repo, scraper=FakeScraper(cases), lead_filter=LeadFilter(FilterConfig()))
    return factory


def test_fetch_makes_weekly_database_backup_first(workdir, monkeypatch):
    seed(workdir)
    monkeypatch.setattr(main, "create_pipeline", fake_pipeline_factory([gunb_case("B/1")]))

    assert run(workdir, "--fetch", "--no-geocode") == 0
    assert run(workdir, "--fetch", "--no-geocode") == 0  # druga tego samego tygodnia – bez nowej kopii

    backups = list((workdir / "data" / "backups").glob("test-*.sqlite"))
    assert len(backups) == 1


def test_cli_fetch_does_not_run_alongside_another_import(workdir, monkeypatch, caplog):
    seed(workdir)
    monkeypatch.setattr(main, "create_pipeline", fake_pipeline_factory([gunb_case("B/1")]))
    monkeypatch.setattr(main, "CLI_IMPORT_WAIT", timedelta(0))
    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        assert repo.acquire_lease(IMPORT_LEASE, "bot-na-serwerze", timedelta(minutes=30))

    assert run(workdir, "--fetch", "--no-geocode") == 1

    assert "trwa inny import" in caplog.text
    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        assert repo.get("B/1") is None


def test_bot_import_stops_between_pages_and_frees_the_lock(workdir):
    config = main.load_config(workdir / "config.yaml")
    config = replace(config, gunb=replace(config.gunb, page_size=1))
    stop = threading.Event()

    class StopDuringImport(FakeScraper):
        def fetch_pages(self, query, page_size=200):
            for page in super().fetch_pages(query, page_size):
                yield page
                stop.set()  # zamykanie programu w trakcie importu

    with LeadRepository(config.storage.db_path) as repo:
        pipeline = LeadPipeline(repo, scraper=StopDuringImport([gunb_case(f"A/{n}") for n in range(3)]),
                                lead_filter=LeadFilter(FilterConfig()))
        fetcher = main.bot_fetcher(pipeline, config, should_stop=stop.is_set)

        with pytest.raises(ImportSkipped) as excinfo:
            fetcher()

        assert excinfo.value.retry_in == timedelta(0)  # dokończy zaraz po ponownym starcie
        assert repo.get("A/1") is not None and repo.get("A/2") is None
        assert repo.lease_holder(IMPORT_LEASE) is None


def test_test_alert_requires_admin_chat(workdir, caplog):
    assert run(workdir, "--test-alert") == 1
    assert "TELEGRAM_ADMIN_CHAT_ID" in caplog.text


def test_test_alert_is_sent_to_admin_chat(workdir, monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "42")
    sent = []
    monkeypatch.setattr(main, "telegram_sender", lambda token, chat_id, **kwargs: sent.append)

    assert run(workdir, "--test-alert") == 0

    assert len(sent) == 1 and "Test kanału admina" in sent[0]
    assert "wysłany" in capsys.readouterr().out


def test_errors_during_run_reach_admin_chat(workdir, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "42")
    sent = []
    monkeypatch.setattr(main, "telegram_sender", lambda token, chat_id, **kwargs: sent.append)

    assert run(workdir, "--notify-discord") == 1  # brak webhooka → błąd w logu

    assert len(sent) == 1 and sent[0].startswith("🚨 BŁĄD:")


def test_unexpected_crash_is_logged_as_critical(workdir, monkeypatch, caplog):
    def crash(*args, **kwargs):
        raise RuntimeError("nieoczekiwany błąd")

    monkeypatch.setattr(main, "_run_fetch", crash)
    assert run(workdir, "--fetch") == 1
    assert any(r.levelname == "CRITICAL" and r.exc_info for r in caplog.records)


def test_cli_fetch_records_the_import_for_reports_and_status(workdir, monkeypatch):
    monkeypatch.setattr(main, "create_pipeline", fake_pipeline_factory([gunb_case("B/1")]))

    assert run(workdir, "--fetch", "--no-geocode") == 0

    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        status = BotStore(repo).job_status("import")
        assert status.stan == "ok" and "nowe 1" in status.opis
        assert BotStore(repo).job_time("import_udany") is not None  # „🕒 Rejestr GUNB sprawdzony: …”


def test_cli_fetch_failure_is_recorded(workdir, monkeypatch):
    class BrokenScraper(FakeScraper):
        def fetch_pages(self, query, page_size=200):
            raise HttpError("GUNB: HTTP 503", status_code=503)

    def factory(config, repo, *, geocode=True):
        return LeadPipeline(repo, scraper=BrokenScraper([]), lead_filter=LeadFilter(FilterConfig()))

    monkeypatch.setattr(main, "create_pipeline", factory)

    assert run(workdir, "--fetch", "--no-geocode") == 1

    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        status = BotStore(repo).job_status("import")
        assert status.stan == "blad" and "503" in status.opis
        assert BotStore(repo).job_time("import_udany") is None


def test_import_without_any_case_is_not_counted_as_a_checked_registry(workdir, monkeypatch):
    monkeypatch.setattr(main, "create_pipeline", fake_pipeline_factory([]))  # paczka bez spraw w oknie

    assert run(workdir, "--fetch", "--no-geocode") == 1

    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        status = BotStore(repo).job_status("import")
        assert status.stan == "blad" and "brak spraw" in status.opis
        assert BotStore(repo).job_time("import_udany") is None


# --- Kontrola zdrowia (``--zdrowie``) ------------------------------------------------------------------

def test_health_check_reports_a_stopped_bot_with_exit_code_2(workdir, capsys):
    seed(workdir)
    assert run(workdir, "--zdrowie") == 2
    assert "proces bota nie działa" in capsys.readouterr().out


def test_health_check_never_upgrades_an_old_database(workdir, capsys):
    import sqlite3

    from gunb_tool.instance import instance_lock
    from tests.test_storage import legacy_database

    (workdir / "data").mkdir()
    legacy_database(workdir / "data" / "test.sqlite", 6).close()
    with instance_lock(workdir / "data" / "gunb-bot.lock"):
        run(workdir, "--zdrowie")

    conn = sqlite3.connect(workdir / "data" / "test.sqlite")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    conn.close()
    assert "przy najbliższym starcie" in capsys.readouterr().out
    assert not list((workdir / "data").glob("backups/*"))  # nie było migracji, więc i kopii przed nią


def test_health_check_pings_the_external_monitor(workdir, monkeypatch):
    import gunb_tool.health as health

    calls = []
    monkeypatch.setattr(health.requests, "get", lambda url, timeout: calls.append(url))
    seed(workdir)
    run(workdir, "--zdrowie", "--ping", "https://hc-ping.com/abc")
    (workdir / ".env").write_text("HEALTHCHECK_PING_URL=https://hc-ping.com/z-env\n", encoding="utf-8")
    monkeypatch.delenv("HEALTHCHECK_PING_URL", raising=False)
    run(workdir, "--zdrowie")
    monkeypatch.delenv("HEALTHCHECK_PING_URL", raising=False)  # ustawiła ją konfiguracja z .env

    assert calls == ["https://hc-ping.com/abc/fail", "https://hc-ping.com/z-env/fail"]  # bot nie działa


def test_health_check_does_not_write_the_log_file(workdir):
    seed(workdir)
    (workdir / "config.yaml").write_text(
        (workdir / "config.yaml").read_text(encoding="utf-8") + "  file: logs/bot.log\n", encoding="utf-8")
    run(workdir, "--zdrowie")
    assert not (workdir / "logs").exists()  # uruchomiona z innego konta nie przejmie pliku logu bota


# --- Nocna kopia bazy w bocie -----------------------------------------------------------------------------

def test_jobs_bot_backs_up_the_database_every_night(workdir, monkeypatch):
    from gunb_tool.config import load_config

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    seed(workdir)
    config = load_config(workdir / "config.yaml")
    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        jobs_bot = main._make_jobs_bot(config, repo, threading.Event())
        first = jobs_bot.maintenance()
        assert jobs_bot.maintenance() is None  # tego dnia kopia już jest
        assert jobs_bot.fetcher is not None

    assert first is not None and first.parent == workdir / "data" / "backups"
    with LeadRepository(first) as copy:
        assert copy.get("A/1") is not None


def test_old_code_on_a_newer_database_stops_without_a_restart_loop(workdir, caplog):
    import sqlite3

    from gunb_tool.storage import SCHEMA_VERSION

    seed(workdir)
    conn = sqlite3.connect(workdir / "data" / "test.sqlite")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()

    assert run(workdir, "--stats") == main.EXIT_USAGE  # systemd: RestartPreventExitStatus=2 3
    assert "nowszy" in caplog.text
