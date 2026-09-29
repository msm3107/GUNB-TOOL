"""GUNB Lead Tool – uruchamianie z linii poleceń.

Przykłady::

    python main.py --fetch                          # pobierz i przetwórz dane (okno z config.yaml)
    python main.py --fetch --days 7 --limit 50      # szybki test konfiguracji
    python main.py --fetch --mark-sent              # pierwsze uruchomienie: zbuduj bazę bez zalewu powiadomień
    python main.py --notify-telegram --dry-run      # podgląd wiadomości bez wysyłania
    python main.py --fetch --notify-telegram --sync-sheets   # typowe uruchomienie z harmonogramu
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

from gunb_tool.config import AppConfig, ConfigError, LoggingConfig, load_config, override_scope
from gunb_tool.exporter import (
    DiscordNotifier,
    ExportError,
    GoogleSheetsExporter,
    NotificationError,
    Notifier,
    TelegramNotifier,
)
from gunb_tool.gunb_scraper import FetchQuery, GunbFormatError
from gunb_tool.http_client import HttpError
from gunb_tool.models import Source
from gunb_tool.pipeline import (
    FetchReport,
    LeadPipeline,
    build_formatter,
    build_query,
    create_pipeline,
    notification_http_client,
)
from gunb_tool.storage import LeadRepository

log = logging.getLogger("gunb_tool.main")

CHANNELS = ("telegram", "discord")
EXIT_OK, EXIT_PARTIAL_FAILURE, EXIT_USAGE = 0, 1, 2


def build_parser() -> argparse.ArgumentParser:
    """Definicja parametrów CLI."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Pozyskiwanie i wzbogacanie leadów inwestycyjnych z rejestru GUNB (RWDZ).",
        epilog="Akcje można łączyć; wykonywane są w kolejności: fetch → mark-sent → notify → sync-sheets → stats.",
    )
    parser.add_argument("-c", "--config", type=Path, default=Path("config.yaml"),
                        help="ścieżka do pliku konfiguracyjnego (domyślnie config.yaml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="szczegółowe logi (DEBUG)")

    actions = parser.add_argument_group("akcje")
    actions.add_argument("--fetch", action="store_true", help="pobierz paczki GUNB, przefiltruj, zgeokoduj i zapisz")
    actions.add_argument("--notify-telegram", action="store_true", help="wyślij nowe/zmienione leady na Telegram")
    actions.add_argument("--notify-discord", action="store_true", help="wyślij nowe/zmienione leady na Discord")
    actions.add_argument("--sync-sheets", action="store_true", help="wyeksportuj nowe/zmienione leady do Google Sheets")
    actions.add_argument("--mark-sent", action="store_true",
                         help="oznacz wszystkie oczekujące leady jako wysłane, bez wysyłania")
    actions.add_argument("--stats", action="store_true", help="pokaż podsumowanie bazy")

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

    notify = parser.add_argument_group("opcje powiadomień")
    notify.add_argument("--dry-run", action="store_true", help="wypisz wiadomości zamiast je wysyłać")
    notify.add_argument("--max-leads", "--max-messages", dest="max_leads", type=int, metavar="N",
                        help="najwięcej leadów obsłużonych na kanał (nadpisuje notifications.max_leads_per_run)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Punkt wejścia; zwraca kod wyjścia (0 – OK, 1 – część operacji nie powiodła się, 2 – błąd użycia)."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (args.fetch or args.notify_telegram or args.notify_discord or args.sync_sheets
            or args.mark_sent or args.stats):
        parser.print_help()
        return EXIT_USAGE

    _configure_console()
    try:
        config = load_config(args.config)
        config = override_scope(config, voivodeships=args.voivodeship, powiats=args.powiat, sources=args.source)
    except ConfigError as exc:
        print(f"Błąd konfiguracji: {exc}", file=sys.stderr)
        return EXIT_USAGE
    setup_logging(config.logging, verbose=args.verbose)

    exit_code = EXIT_OK
    with LeadRepository(config.storage.db_path, negative_cache_days=config.geocoding.negative_cache_days) as repo:
        pipeline = LeadPipeline(repo, formatter=build_formatter(config))
        if args.fetch:
            pipeline = create_pipeline(config, repo, geocode=not args.no_geocode)
            exit_code = max(exit_code, _run_fetch(pipeline, config, args))
        if args.mark_sent:
            print(f"Oznaczono jako wysłane: {pipeline.mark_all_sent(CHANNELS)}")
        for channel in CHANNELS:
            if getattr(args, f"notify_{channel}"):
                exit_code = max(exit_code, _run_notify(pipeline, config, channel, args))
        if args.sync_sheets:
            exit_code = max(exit_code, _run_sync_sheets(pipeline, config))
        if args.stats:
            _print_stats(repo.stats())
    return exit_code


def setup_logging(config: LoggingConfig, *, verbose: bool = False) -> None:
    """Konfiguruje logi na stderr i (opcjonalnie) do pliku rotowanego; podmienia tylko własne handlery."""
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_gunb_tool", False)]:
        root.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if config.file is not None:
        config.file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            config.file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        ))
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._gunb_tool = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else config.level)
    # urllib3 na poziomie DEBUG loguje pełne adresy – razem z tokenami bota/webhooka.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --- Akcje ------------------------------------------------------------------------

def _run_fetch(pipeline: LeadPipeline, config: AppConfig, args: argparse.Namespace) -> int:
    query = build_query(config.gunb, today=date.today(), since=args.since, until=args.until, days=args.days)
    log.info(
        "Pobieranie: źródła %s, województwa %s, powiaty %s, %s od %s%s",
        ",".join(s.value for s in query.sources), ",".join(query.voivodeships),
        ",".join(sorted(query.powiats)) or "wszystkie", _date_basis(query), query.date_from,
        f" do {query.date_to}" if query.date_to else "",
    )
    try:
        report = pipeline.fetch(query, page_size=config.gunb.page_size, limit=args.limit)
    except (HttpError, GunbFormatError) as exc:
        log.error("Pobieranie danych GUNB nie powiodło się: %s", exc)
        return EXIT_PARTIAL_FAILURE
    _print_fetch_report(report)
    return EXIT_OK


def _date_basis(query: FetchQuery) -> str:
    """Opis daty filtrowania – zgłoszenia mają wyłącznie datę wpływu."""
    if query.date_field == "wplyw" or Source.POZWOLENIA not in query.sources:
        return "data wpływu"
    if Source.ZGLOSZENIA in query.sources:
        return "data decyzji (zgłoszenia: data wpływu)"
    return "data decyzji"


def _run_notify(pipeline: LeadPipeline, config: AppConfig, channel: str, args: argparse.Namespace) -> int:
    segment_chats = _segment_chats(config) if channel == "telegram" else {}
    try:
        notifier = (_DryRunNotifier(channel, segment_chats) if args.dry_run
                    else _make_notifier(channel, config, segment_chats))
    except NotificationError as exc:
        log.error("%s: %s", channel, exc)
        return EXIT_PARTIAL_FAILURE
    report = pipeline.notify(
        notifier,
        limit=args.max_leads or config.notifications.max_leads_per_run,
        max_age_days=config.notifications.max_age_days,
        digest_threshold=config.notifications.digest_threshold,
        dry_run=args.dry_run,
    )
    mode = " (dry-run – nic nie wysłano)" if args.dry_run else ""
    digests = f", raporty zbiorcze {report.digests}" if report.digests else ""
    print(f"{channel}: wiadomości {report.messages}, leady {report.leads}{digests}, błędy {report.failed}{mode}")
    return EXIT_PARTIAL_FAILURE if report.failed else EXIT_OK


def _run_sync_sheets(pipeline: LeadPipeline, config: AppConfig) -> int:
    try:
        exporter = GoogleSheetsExporter.from_service_account(
            config.sheets.service_account_file, config.sheets.spreadsheet_id, config.sheets.worksheet
        )
        result = pipeline.sync_sheets(exporter)
    except ExportError as exc:
        log.error("Google Sheets: %s", exc)
        return EXIT_PARTIAL_FAILURE
    except Exception as exc:  # błędy gspread / google-auth / sieci – nie przerywają pozostałych akcji
        log.error("Google Sheets: synchronizacja nie powiodła się: %s", exc)
        log.debug("Szczegóły błędu Google Sheets", exc_info=True)
        return EXIT_PARTIAL_FAILURE
    print(f"Google Sheets: zaktualizowano {result.updated}, dopisano {result.appended}")
    return EXIT_OK


def _segment_chats(config: AppConfig) -> dict[str, str]:
    return {segment.name: segment.telegram_chat_id for segment in config.segments if segment.telegram_chat_id}


def _make_notifier(channel: str, config: AppConfig, segment_chats: dict[str, str]) -> Notifier:
    http = notification_http_client(config)
    if channel == "telegram":
        return TelegramNotifier(http, config.telegram.bot_token, config.telegram.chat_id,
                                segment_chats=segment_chats, min_interval=config.telegram.delay_seconds)
    return DiscordNotifier(http, config.discord.webhook_url, min_interval=config.discord.delay_seconds)


class _DryRunNotifier:
    """Zaślepka kanału w trybie ``--dry-run`` (nie wymaga tokenów, nic nie wysyła, pokazuje routing)."""

    def __init__(self, channel: str, segment_chats: dict[str, str] | None = None) -> None:
        self.channel = channel
        self.segment_chats = segment_chats or {}

    def destination(self, segment: str | None) -> str:
        return self.segment_chats.get(segment or "", "czat domyślny")

    def send(self, message: object, destination: str | None = None) -> None:  # pragma: no cover
        raise NotificationError("dry-run")


# --- Wyjście ---------------------------------------------------------------------------

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
    print(f"  niewysłane: {stats['niewyslane']} · do arkusza: {stats['do_arkusza']} · "
          f"zmiany statusu: {stats['zmiany_statusu']}")


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"niepoprawna data {value!r} – użyj formatu RRRR-MM-DD") from None


def _configure_console() -> None:
    """Wymusza UTF-8 na konsoli (polskie znaki i emoji w podglądzie wiadomości)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


if __name__ == "__main__":
    sys.exit(main())
