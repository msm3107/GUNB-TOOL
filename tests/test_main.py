import logging
from pathlib import Path
import pytest

import main
from core.config import FilterConfig, LoggingConfig
from core.data_filter import LeadFilter
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
