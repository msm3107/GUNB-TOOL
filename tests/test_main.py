import threading
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import main
from gunb_tool.config import FilterConfig
from gunb_tool.data_filter import LeadFilter
from gunb_tool.models import Investment
from gunb_tool.pipeline import IMPORT_LEASE, ImportSkipped, LeadPipeline
from gunb_tool.storage import LeadRepository
from tests.test_pipeline import FakeScraper, gunb_case


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
    assert "NOWY LEAD" in capsys.readouterr().out


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
