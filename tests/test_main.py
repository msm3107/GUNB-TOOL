import logging
from pathlib import Path

import pytest

import main
from core.config import FilterConfig, LoggingConfig
from core.data_filter import LeadFilter
from core.instance import (
    database_replacement_lock,
    engine_process_lock,
    instance_lock,
    lock_is_free,
)
from core.models import Investment
from core.pipeline import LeadPipeline
from core.storage import LeadRepository
from tests.test_pipeline import FakeScraper, gunb_case


def test_log_output_never_contains_the_token_even_in_tracebacks(tmp_path):
    token = "SECRET_PASSWORD_123"
    log_file = tmp_path / "engine.log"
    main.setup_logging(LoggingConfig(level="INFO", file=log_file), secrets=(token,))
    try:
        try:
            try:
                raise ConnectionError("Connection refused with user auth SECRET_PASSWORD_123")
            except ConnectionError as exc:
                raise RuntimeError(f"wysyłka nie powiodła się (hasło {token})") from exc
        except RuntimeError:
            logging.getLogger("core.test").exception("Błąd wysyłki")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = log_file.read_text(encoding="utf-8")
    finally:
        for handler in [h for h in logging.getLogger().handlers if getattr(h, "_core", False)]:
            logging.getLogger().removeHandler(handler)
            handler.close()

    assert "Błąd wysyłki" in text and "Traceback" in text
    assert "SECRET" not in text and "<token>" in text


@pytest.fixture
def workdir(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "gunb:\n  voivodeships: ['16']\n  powiats: ['1607']\n"
        "storage:\n  db_path: data/test.sqlite\n"
        "logging:\n  level: INFO\n",
        encoding="utf-8",
    )
    return tmp_path


def run(workdir: Path, *args: str) -> int:
    return main.main(["--config", str(workdir / "config.yaml"), *args])


def seed(workdir: Path) -> None:
    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        repo.upsert(Investment(
            id_sprawy="A/1",
            zrodlo="pozwolenia",
            status="decyzja",
            nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego",
            kategoria="mieszkaniowa-jednorodzinna",
        ))


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


def test_engine_actions_hold_process_locks_for_the_database(workdir, monkeypatch):
    monkeypatch.setattr(main, "setup_logging", lambda *args, **kwargs: None)

    def check_locks(args, config):
        lock_dir = config.storage.db_path.parent
        assert not lock_is_free(lock_dir / "gunb-engine.lock")
        assert not lock_is_free(lock_dir / "gunb-bot.lock")
        return main.EXIT_OK

    monkeypatch.setattr(main, "_run_actions", check_locks)

    assert run(workdir, "--stats") == main.EXIT_OK


def test_health_check_holds_database_replacement_lock(workdir, monkeypatch):
    def check_locks(config, *, ping_url):
        lock_dir = config.storage.db_path.parent
        assert not lock_is_free(lock_dir / "gunb-database-replacement.lock")
        assert lock_is_free(lock_dir / "gunb-engine.lock")
        return main.EXIT_OK

    monkeypatch.setattr(main, "_run_health", check_locks)

    assert run(workdir, "--zdrowie") == main.EXIT_OK


def test_health_check_runs_and_pings_during_an_active_wal_write(workdir, monkeypatch):
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(main.health, "ping", lambda url, level: calls.append((url, level)))
    db_path = workdir / "data" / "test.sqlite"

    with LeadRepository(db_path) as repo:
        assert repo.journal_mode == "wal"
        repo.connection.execute("BEGIN IMMEDIATE")
        try:
            with engine_process_lock(db_path):
                result = run(workdir, "--zdrowie", "--ping", "https://monitor.example/check")
            assert result in (main.health.OK, main.health.WARNING)
        finally:
            repo.connection.rollback()

    assert calls == [("https://monitor.example/check", result)]


def test_health_check_waits_out_database_file_replacement(workdir, monkeypatch, capsys):
    ran_health = False

    def mark_health_ran(config, *, ping_url):
        nonlocal ran_health
        ran_health = True
        return main.EXIT_OK

    monkeypatch.setattr(main, "_run_health", mark_health_ran)
    db_path = workdir / "data" / "test.sqlite"

    with database_replacement_lock(db_path):
        assert run(workdir, "--zdrowie") == main.EXIT_ALREADY_RUNNING

    assert not ran_health
    assert "Kontrola zdrowia" in capsys.readouterr().err


@pytest.mark.parametrize("lock_name", ["gunb-engine.lock", "gunb-bot.lock"])
def test_engine_actions_refuse_to_run_when_another_process_owns_the_database(
    workdir, monkeypatch, lock_name
):
    monkeypatch.setattr(main, "setup_logging", lambda *args, **kwargs: None)
    ran_actions = False

    def mark_actions_ran(args, config):
        nonlocal ran_actions
        ran_actions = True
        return main.EXIT_OK

    monkeypatch.setattr(main, "_run_actions", mark_actions_ran)
    db_path = workdir / "data" / "test.sqlite"
    with instance_lock(db_path.parent / lock_name):
        assert run(workdir, "--stats") == main.EXIT_ALREADY_RUNNING

    assert not ran_actions
    assert lock_is_free(db_path.parent / "gunb-engine.lock")


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


def test_mark_sent_marks_leads(workdir, capsys):
    seed(workdir)
    assert run(workdir, "--mark-sent") == 0
    assert "Oznaczono jako wysłane: 1" in capsys.readouterr().out
    with LeadRepository(workdir / "data" / "test.sqlite") as repo:
        assert repo.get("A/1").czy_wyslano is True
