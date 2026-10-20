"""GUNB Lead Tool – silnik pobierania i przetwarzania leadów inwestycyjnych z rejestru RWDZ (GUNB).

Przykłady::

    python main.py --fetch                          # pobierz i przetwórz dane (okno z config.yaml)
    python main.py --fetch --days 7 --limit 50      # szybki test konfiguracji
    python main.py --fetch --no-geocode             # szybki import bez zapytań do ULDK
    python main.py --stats                          # podsumowanie bazy
    python main.py --zdrowie                        # kontrola zdrowia bazy i dysku
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path

from core import health
from core.clock import local, utc_now
from core.config import (
    AppConfig,
    ConfigError,
    LoggingConfig,
    load_config,
    override_scope,
)
from core.gunb_scraper import FetchQuery, GunbFormatError
from core.http_client import HttpError, redact_url
from core.instance import AlreadyRunning, database_replacement_lock, engine_process_lock
from core.maintenance import backup_if_due, vacuum_after_import
from core.models import Source
from core.pipeline import (
    EmptyImport,
    FetchReport,
    ImportSkipped,
    LeadPipeline,
    build_query,
    create_pipeline,
    import_lease,
)
from core.stages import LONGEST_WINDOW_DAYS
from core.storage import LeadRepository, SchemaTooNew

log = logging.getLogger("core.main")

EXIT_OK, EXIT_PARTIAL_FAILURE, EXIT_USAGE, EXIT_ALREADY_RUNNING = 0, 1, 2, 3
CLI_IMPORT_WAIT = timedelta(minutes=45)
IMPORT_JOB = "import_gunb"
LAST_IMPORT_JOB = "ostatni_import_gunb"


def build_parser() -> argparse.ArgumentParser:
    """Definicja parametrów CLI."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Pozyskiwanie i wzbogacanie leadów inwestycyjnych z rejestru GUNB (RWDZ).",
    )
    parser.add_argument("-c", "--config", type=Path, default=Path("config.yaml"),
                        help="ścieżka do pliku konfiguracyjnego (domyślnie config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="szczegółowe logi (DEBUG)")

    actions = parser.add_argument_group("akcje")
    actions.add_argument("--fetch", action="store_true", help="pobierz paczki GUNB, przefiltruj, zgeokoduj i zapisz")
    actions.add_argument("--mark-sent", action="store_true",
                         help="oznacz wszystkie oczekujące leady jako wysłane, bez wysyłania")
    actions.add_argument("--stats", action="store_true", help="pokaż podsumowanie bazy")
    actions.add_argument("--backup", action="store_true", help="wykonaj kopię bazy danych teraz")
    actions.add_argument("--zdrowie", action="store_true",
                         help="kontrola zdrowia silnika, tylko do odczytu (kod 0 – OK, 1 – ostrzeżenie, 2 – awaria)")
    actions.add_argument("--ping", metavar="URL",
                         help="z --zdrowie: zgłoś wynik do monitora typu healthchecks.io "
                              "(domyślnie HEALTHCHECK_PING_URL z .env)")

    fetch = parser.add_argument_group("opcje --fetch")
    fetch.add_argument("--since", type=_parse_date, metavar="RRRR-MM-DD", help="data początkowa (nadpisuje okno)")
    fetch.add_argument("--until", type=_parse_date, metavar="RRRR-MM-DD", help="data końcowa (włącznie)")
    fetch.add_argument("--days", type=int, metavar="N", help="okno czasowe w dniach (nadpisuje lookback_days)")
    fetch.add_argument("--voivodeship", action="append", metavar="KOD",
                       help="województwo (TERYT/nazwa) zamiast ustawień z pliku; można powtarzać")
    fetch.add_argument("--powiat", action="append", metavar="KOD",
                       help="powiat (4 cyfry TERYT) zamiast ustawień z pliku; można powtarzać")
    fetch.add_argument("--source", action="append", choices=["pozwolenia", "zgloszenia"],
                       help="rejestr do przetworzenia; można powtarzać")
    fetch.add_argument("--no-geocode", action="store_true", help="pomiń geokodowanie ULDK")
    fetch.add_argument("--limit", type=int, metavar="N", help="przetwórz najwyżej N spraw")
    fetch.add_argument("--historical", action="store_true",
                       help="import historyczny bez oznaczania nowości; sięga do 27 miesięcy wstecz")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Punkt wejścia; zwraca kod wyjścia (0 – OK, 1 – błąd operacji, 2 – błąd użycia)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.fetch or args.mark_sent or args.stats or args.backup or args.zdrowie):
        parser.print_help()
        return EXIT_USAGE

    _configure_console()
    try:
        config = load_config(args.config)
        config = override_scope(config, voivodeships=args.voivodeship, powiats=args.powiat, sources=args.source)
    except ConfigError as exc:
        print(f"Błąd konfiguracji: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.zdrowie:
        try:
            with database_replacement_lock(config.storage.db_path):
                return _run_health(config, ping_url=args.ping or os.environ.get("HEALTHCHECK_PING_URL"))
        except AlreadyRunning as exc:
            print(f"Kontrola zdrowia nie może działać podczas podmiany bazy: {exc}", file=sys.stderr)
            return EXIT_ALREADY_RUNNING

    setup_logging(config.logging, verbose=args.verbose)

    try:
        with engine_process_lock(config.storage.db_path):
            return _run_actions(args, config)
    except AlreadyRunning as exc:
        log.error("Silnik nie wystartował: %s", exc)
        return EXIT_ALREADY_RUNNING
    except SchemaTooNew as exc:
        log.critical("%s", exc)
        return EXIT_USAGE
    except Exception:
        log.critical("Nieoczekiwany błąd – program przerwany", exc_info=True)
        return EXIT_PARTIAL_FAILURE


def _run_actions(args: argparse.Namespace, config: AppConfig) -> int:
    exit_code = EXIT_OK
    with LeadRepository(
        config.storage.db_path,
        negative_cache_days=config.geocoding.negative_cache_days,
        backup_dir=config.storage.backup_dir,
    ) as repo:
        if args.fetch:
            pipeline = create_pipeline(config, repo, geocode=not args.no_geocode)
            exit_code = max(exit_code, _run_fetch(pipeline, config, args))
        if args.mark_sent:
            print(f"Oznaczono jako wysłane: {repo.mark_all_sent(('web', 'email'))}")
        if args.backup:
            destination = scheduled_backup(repo, config)
            print(f"Kopia zapasowa bazy danych: {destination or 'pominięto'}")
        if args.stats:
            _print_stats(repo.stats())
    return exit_code


class RedactingFormatter(logging.Formatter):
    """Formatter maskujący sekrety w całym wpisie logów."""

    def __init__(self, fmt: str, *, secrets: Sequence[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    def format(self, record: logging.LogRecord) -> str:
        text = redact_url(super().format(record))
        for secret in self._secrets:
            text = text.replace(secret, "<token>")
        return text


def setup_logging(config: LoggingConfig, *, verbose: bool = False, secrets: Sequence[str] = ()) -> None:
    """Konfiguruje logi na stderr i do pliku rotowanego."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_core_app", False)]:
        root.removeHandler(handler)
        handler.close()
    formatter = RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", secrets=secrets)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if config.file is not None:
        config.file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(config.file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        )
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._core_app = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else config.level)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _run_health(config: AppConfig, *, ping_url: str | None) -> int:
    level, checks = health.check_health(config)
    for check in checks:
        print(check)
    print({health.OK: "WYNIK: OK", health.WARNING: "WYNIK: OSTRZEŻENIE", health.CRITICAL: "WYNIK: AWARIA"}[level])
    if ping_url:
        health.ping(ping_url, level)
    return level


def history_start(args: argparse.Namespace, *, today: date) -> date | None:
    if args.since is not None or not args.historical:
        return args.since
    return today - timedelta(days=LONGEST_WINDOW_DAYS)


def _run_fetch(pipeline: LeadPipeline, config: AppConfig, args: argparse.Namespace) -> int:
    today = local(utc_now()).date()
    query = build_query(
        config.gunb,
        today=today,
        since=history_start(args, today=today),
        until=args.until,
        days=args.days,
    )
    log.info(
        "Pobieranie: źródła %s, województwa %s, powiaty %s, %s od %s%s",
        ",".join(s.value for s in query.sources),
        ",".join(query.voivodeships),
        ",".join(sorted(query.powiats)) or "wszystkie",
        _date_basis(query),
        query.date_from,
        f" do {query.date_to}" if query.date_to else "",
    )
    try:
        report = fetch_with_maintenance(
            pipeline,
            config,
            query,
            limit=args.limit,
            historical=args.historical,
            wait_for_other_import=CLI_IMPORT_WAIT,
        )
    except (HttpError, GunbFormatError, EmptyImport) as exc:
        log.error("Pobieranie danych GUNB nie powiodło się: %s", exc)
        return EXIT_PARTIAL_FAILURE
    except ImportSkipped as exc:
        log.error("Pobieranie danych GUNB nie wystartowało: %s – spróbuj później", exc)
        return EXIT_PARTIAL_FAILURE
    _print_fetch_report(report)
    return EXIT_OK


def fetch_with_maintenance(
    pipeline: LeadPipeline,
    config: AppConfig,
    query: FetchQuery,
    *,
    limit: int | None = None,
    historical: bool = False,
    should_stop: Callable[[], bool] = lambda: False,
    wait_for_other_import: timedelta = timedelta(0),
) -> FetchReport:
    """Pobieranie z higieną bazy: kopia przed importem (gdy przypada pora) i VACUUM po dużym imporcie."""
    storage = config.storage
    with import_lease(pipeline.repo, wait=wait_for_other_import) as renew_lease:
        _record_job_start(pipeline.repo, IMPORT_JOB)
        try:
            scheduled_backup(pipeline.repo, config)
            report = pipeline.fetch(
                query,
                page_size=config.gunb.page_size,
                limit=limit,
                historical=historical,
                should_continue=lambda: renew_lease() and not should_stop(),
            )
            vacuum_after_import(
                pipeline.repo,
                changed=report.new + report.status_changed + report.updated,
                threshold=storage.vacuum_threshold,
            )
        except Exception as exc:
            _record_job_finish(pipeline.repo, IMPORT_JOB, "blad", f"{exc}")
            raise
        if report.interrupted:
            _record_job_finish(pipeline.repo, IMPORT_JOB, "pominieto", report.summary())
        elif report.cases == 0 and not historical and limit is None:
            problem = EmptyImport(
                f"brak spraw w paczce GUNB dla okna od {query.date_from} – podejrzana paczka "
                "albo zły zakres (gunb.voivodeships / gunb.powiats)"
            )
            _record_job_finish(pipeline.repo, IMPORT_JOB, "blad", str(problem))
            raise problem
        else:
            _record_job_finish(pipeline.repo, IMPORT_JOB, "ok", report.summary())
            _record_job_time(pipeline.repo, LAST_IMPORT_JOB, pipeline.repo.now())
    return report


def scheduled_backup(repo: LeadRepository, config: AppConfig) -> Path | None:
    """Kopia bazy, jeśli od ostatniej minęło storage.backup_every_days dni."""
    storage = config.storage
    return backup_if_due(
        repo,
        storage.backup_dir or storage.db_path.parent / "backups",
        today=local(utc_now()).date(),
        every_days=storage.backup_every_days,
        keep=storage.backup_keep,
    )


def _record_job_start(repo: LeadRepository, name: str) -> None:
    try:
        repo.connection.execute(
            "INSERT OR REPLACE INTO zadania (nazwa, ostatnio) VALUES (?, ?)",
            (name, repo.now().isoformat()),
        )
        repo.connection.commit()
    except Exception:
        pass


def _record_job_finish(repo: LeadRepository, name: str, state: str, desc: str) -> None:
    try:
        repo.connection.execute(
            "INSERT OR REPLACE INTO zadania (nazwa, ostatnio, stan, opis) VALUES (?, ?, ?, ?)",
            (name, repo.now().isoformat(), state, desc),
        )
        repo.connection.commit()
    except Exception:
        pass


def _record_job_time(repo: LeadRepository, name: str, moment: datetime) -> None:
    try:
        repo.connection.execute(
            "INSERT OR REPLACE INTO zadania (nazwa, ostatnio) VALUES (?, ?)",
            (name, moment.isoformat()),
        )
        repo.connection.commit()
    except Exception:
        pass


def _date_basis(query: FetchQuery) -> str:
    if query.date_field == "wplyw" or Source.POZWOLENIA not in query.sources:
        return "data wpływu"
    if Source.ZGLOSZENIA in query.sources:
        return "data decyzji (zgłoszenia: data wpływu)"
    return "data decyzji"


def _print_fetch_report(report: FetchReport) -> None:
    print(
        f"Strony: {report.pages} · sprawy: {report.cases} → zachowane: {report.kept} "
        f"(nowe {report.new}, zmiana statusu {report.status_changed}, "
        f"aktualizacja {report.updated}, bez zmian {report.unchanged})"
    )
    dropped = sum(report.dropped.values())
    if dropped:
        reasons = ", ".join(f"{reason} ({count})" for reason, count in report.dropped.most_common(5))
        print(f"Odrzucono {dropped}: {reasons}")
    print(
        f"Lokalizacja: zgeokodowano {report.geocoded}, z bazy {report.geocode_reused}, "
        f"bez współrzędnych {report.geocode_missing}"
    )


def _print_stats(stats: dict) -> None:
    def fmt(groups: dict) -> str:
        return ", ".join(f"{key} {value}" for key, value in groups.items()) or "—"

    print(f"Leady w bazie: {stats['razem']} (z lokalizacją {stats['z_lokalizacja']})")
    print(f"  statusy:    {fmt(stats['statusy'])}")
    print(f"  kategorie:  {fmt(stats['kategorie'])}")
    print(f"  segmenty:   {fmt(stats['segmenty'])}")
    print(f"  źródła:     {fmt(stats['zrodla'])}")
    print(f"  niewysłane: {stats['niewyslane']} · zmiany statusu: {stats['zmiany_statusu']}")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"niepoprawna data {value!r} – użyj formatu RRRR-MM-DD") from None


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    sys.exit(main())
