"""Orkiestracja: pobieranie → filtrowanie → geokodowanie → zapis, powiadomienia i synchronizacja arkusza."""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Callable, Protocol, Sequence

from .config import AppConfig, GunbConfig
from .data_filter import FilterDecision, LeadFilter
from .exporter import MessageFormatter, NotificationError, Notifier, SheetsSyncResult
from .geocoding_uldk import GeocodeResult, UldkClient, UldkGeocoder, google_maps_url
from .gunb_scraper import FetchQuery, GunbScraper, Page
from .http_client import ResilientHttpClient
from .models import GunbCase, Investment
from .storage import GEO_FIELDS, ChangeType, LeadRepository

log = logging.getLogger(__name__)


class PageSource(Protocol):
    """Źródło stron spraw (``GunbScraper`` lub atrapa w testach)."""

    def fetch_pages(self, query: FetchQuery, page_size: int = 200) -> Any: ...


class Geocoder(Protocol):
    """Geokoder spraw (``UldkGeocoder`` lub atrapa w testach)."""

    def geocode(self, parcels: Sequence[Any]) -> GeocodeResult | None: ...


class SheetsExporter(Protocol):
    """Eksporter arkusza (``GoogleSheetsExporter`` lub atrapa w testach)."""

    def export(self, investments: Sequence[Investment]) -> SheetsSyncResult: ...


@dataclass
class FetchReport:
    """Podsumowanie etapu pobierania."""

    pages: int = 0
    cases: int = 0
    kept: int = 0
    dropped: Counter = field(default_factory=Counter)
    new: int = 0
    status_changed: int = 0
    updated: int = 0
    unchanged: int = 0
    geocoded: int = 0
    geocode_reused: int = 0
    geocode_missing: int = 0


@dataclass
class NotifyReport:
    """Podsumowanie wysyłki na jeden kanał."""

    channel: str
    sent: int = 0
    failed: int = 0
    dry_run: bool = False
    aborted: bool = False


class LeadPipeline:
    """Łączy moduły w kompletne operacje wywoływane z CLI.

    Args:
        repo: repozytorium leadów.
        scraper: źródło stron spraw GUNB (wymagane dla :meth:`fetch`).
        lead_filter: kategoryzacja i odrzucanie szumu (wymagane dla :meth:`fetch`).
        geocoder: geokoder ULDK; ``None`` = bez geokodowania.
        formatter: formater wiadomości.
        sleep: funkcja usypiająca (odstępy między wiadomościami).
    """

    def __init__(
        self,
        repo: LeadRepository,
        *,
        scraper: PageSource | None = None,
        lead_filter: LeadFilter | None = None,
        geocoder: Geocoder | None = None,
        formatter: MessageFormatter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repo = repo
        self.scraper = scraper
        self.lead_filter = lead_filter
        self.geocoder = geocoder
        self.formatter = formatter or MessageFormatter()
        self._sleep = sleep

    # --- Pobieranie ------------------------------------------------------------------

    def fetch(self, query: FetchQuery, *, page_size: int, limit: int | None = None) -> FetchReport:
        """Pobiera sprawy stronami, filtruje, geokoduje i zapisuje (jedna transakcja na stronę).

        Args:
            query: zakres danych.
            page_size: liczba spraw na stronę.
            limit: maksymalna liczba przetworzonych spraw (np. do testów konfiguracji).
        """
        if self.scraper is None or self.lead_filter is None:
            raise RuntimeError("fetch() wymaga scrapera i filtra")
        report = FetchReport()
        for page in self.scraper.fetch_pages(query, page_size):
            report.pages += 1
            batch: list[Investment] = []
            processed = 0
            for case in page.items:
                if limit is not None and report.cases >= limit:
                    break
                report.cases += 1
                processed += 1
                decision = self.lead_filter.evaluate(case)
                if not decision.keep:
                    report.dropped[decision.reason or "?"] += 1
                    continue
                investment = build_investment(case, decision)
                self._locate(investment, case, report)
                batch.append(investment)

            report.kept += len(batch)
            for result in self.repo.upsert_many(batch):
                if result.change is ChangeType.NEW:
                    report.new += 1
                elif result.change is ChangeType.STATUS_CHANGED:
                    report.status_changed += 1
                    log.info("Zmiana statusu %s: %s → %s", result.id_sprawy, result.old_status, result.new_status)
                elif result.change is ChangeType.UPDATED:
                    report.updated += 1
                else:
                    report.unchanged += 1
            _log_page(page, processed, len(batch))
            if limit is not None and report.cases >= limit:
                break
        return report

    def _locate(self, investment: Investment, case: GunbCase, report: FetchReport) -> None:
        existing = self.repo.get(investment.id_sprawy)
        if existing is not None and existing.lat is not None and existing.dzialki == investment.dzialki:
            for name in GEO_FIELDS:
                setattr(investment, name, getattr(existing, name))
            investment.google_maps_url = google_maps_url(existing.lat, existing.lon)
            report.geocode_reused += 1
            return
        result = self.geocoder.geocode(case.parcels) if self.geocoder is not None and case.parcels else None
        if result is None:
            report.geocode_missing += 1
            return
        apply_geocode(investment, result)
        report.geocoded += 1

    # --- Powiadomienia ------------------------------------------------------------------

    def notify(
        self,
        notifier: Notifier,
        *,
        limit: int,
        max_age_days: int | None,
        delay: float,
        dry_run: bool = False,
        output: Callable[[str], None] = print,
        max_failures: int = 3,
    ) -> NotifyReport:
        """Wysyła oczekujące leady na kanał ``notifier.channel`` i oznacza je jako wysłane.

        Pojedyncza odrzucona wiadomość jest pomijana (zostaje w kolejce), a po ``max_failures``
        kolejnych błędach wysyłka jest przerywana. W trybie ``dry_run`` wiadomości trafiają do
        ``output`` i nie są oznaczane jako wysłane.
        """
        channel = notifier.channel
        render = self.formatter.telegram if channel == "telegram" else self.formatter.discord
        report = NotifyReport(channel=channel, dry_run=dry_run)
        consecutive_failures = 0
        pending = self.repo.pending_notifications(channel, limit=limit, max_age_days=max_age_days)

        for index, investment in enumerate(pending):
            text = render(investment, self.repo.last_status_change(investment.id_sprawy))
            if dry_run:
                output(text)
                output("─" * 40)
                report.sent += 1
                continue
            if index and delay > 0:
                self._sleep(delay)
            try:
                notifier.send(text)
            except NotificationError as exc:
                report.failed += 1
                consecutive_failures += 1
                log.error("%s: nie wysłano %s: %s", channel, investment.id_sprawy, exc)
                if consecutive_failures >= max_failures:
                    report.aborted = True
                    log.error("%s: %d kolejnych błędów – przerywam wysyłkę", channel, consecutive_failures)
                    break
                continue
            consecutive_failures = 0
            self.repo.mark_sent(investment.id_sprawy, channel)
            report.sent += 1
        return report

    def mark_all_sent(self, channels: Sequence[str]) -> int:
        """Oznacza wszystkie oczekujące leady jako wysłane (bez wysyłania)."""
        return self.repo.mark_all_sent(channels)

    # --- Google Sheets ---------------------------------------------------------------------

    def sync_sheets(self, exporter: SheetsExporter) -> SheetsSyncResult:
        """Eksportuje leady nowe/zmienione od ostatniej synchronizacji."""
        pending = self.repo.pending_sheet_sync()
        if not pending:
            return SheetsSyncResult(0, 0)
        result = exporter.export(pending)
        self.repo.mark_synced([investment.id_sprawy for investment in pending])
        return result


# --- Funkcje pomocnicze ------------------------------------------------------------------

def build_investment(case: GunbCase, decision: FilterDecision) -> Investment:
    """Buduje rekord leada ze sprawy GUNB i wyniku filtrowania (bez lokalizacji)."""
    classification = decision.classification
    designer = decision.designer
    return Investment(
        id_sprawy=case.id_sprawy,
        zrodlo=case.source.value,
        status=case.status.value,
        status_opis=case.status_raw or case.status.label,
        data_aktualizacji=_iso(case.event_date),
        data_wplywu=_iso(case.data_wplywu),
        data_decyzji=_iso(case.data_decyzji),
        numer_urzedu=case.numer_urzedu,
        numer_decyzji=case.numer_decyzji,
        organ=case.organ,
        kategoria=classification.kategoria,
        kategoria_obiektu=case.kategoria_obiektu,
        rodzaj_robot=case.rodzaj_robot,
        nazwa_zamierzenia=case.nazwa_zamierzenia,
        adres_opisowy=case.adres_opisowy,
        miejscowosc=case.miasto,
        wojewodztwo=case.wojewodztwo,
        powiat_teryt=case.powiat_teryt,
        gmina_teryt=case.gmina_teryt,
        teryt_dzialki=case.parcels[0].full_id if case.parcels else None,
        dzialki=[parcel.full_id for parcel in case.parcels],
        inwestor=decision.investor,
        projektant=designer.display if designer else None,
        projektant_uprawnienia=designer.license_no if designer else None,
        pracownia=designer.firm if designer else None,
        kubatura=case.kubatura,
        is_residential=classification.is_residential,
        is_commercial=classification.is_commercial,
        is_noise=classification.is_noise,
    )


def apply_geocode(investment: Investment, result: GeocodeResult) -> None:
    """Uzupełnia lead o lokalizację z ULDK."""
    investment.lat = result.lat
    investment.lon = result.lon
    investment.precyzja_geo = result.precision.value
    investment.google_maps_url = google_maps_url(result.lat, result.lon)
    investment.geoportal_url = result.geoportal_url
    investment.powiat = result.county
    investment.gmina = result.commune


def build_query(
    config: GunbConfig,
    *,
    today: date,
    since: date | None = None,
    until: date | None = None,
    days: int | None = None,
) -> FetchQuery:
    """Buduje zapytanie z konfiguracji; ``since``/``days`` nadpisują ``lookback_days``."""
    date_from = since or today - timedelta(days=days if days is not None else config.lookback_days)
    return FetchQuery(
        voivodeships=config.voivodeships,
        powiats=frozenset(config.powiats),
        date_from=date_from,
        date_to=until,
        date_field=config.date_field,
        sources=config.sources,
    )


def create_pipeline(config: AppConfig, repo: LeadRepository, *, geocode: bool = True) -> LeadPipeline:
    """Składa produkcyjny pipeline z konfiguracji (klienci HTTP, scraper, filtr, geokoder)."""
    gunb_http = ResilientHttpClient(config.http)
    scraper = GunbScraper(gunb_http, config.gunb.cache_dir, config.gunb.base_url)
    geocoder = None
    if geocode and config.geocoding.enabled:
        uldk_http = ResilientHttpClient(
            replace(config.http, min_delay=config.geocoding.min_delay, max_delay=config.geocoding.max_delay)
        )
        geocoder = UldkGeocoder(
            UldkClient(uldk_http, config.geocoding.uldk_url),
            repo.geocode_cache(),
            max_parcels=config.geocoding.max_parcels_per_case,
            region_fallback=config.geocoding.region_fallback,
        )
    return LeadPipeline(repo, scraper=scraper, lead_filter=LeadFilter(config.filter), geocoder=geocoder)


def notification_http_client(config: AppConfig) -> ResilientHttpClient:
    """Klient HTTP dla powiadomień: bez losowych opóźnień (tempo wyznacza ``delay_seconds``)."""
    return ResilientHttpClient(replace(config.http, min_delay=0.0, max_delay=0.0, max_retries=3))


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _log_page(page: Page, processed: int, kept: int) -> None:
    log.info(
        "[%s] strona %d/%d: przetworzono %d z %d spraw, zachowano %d",
        page.label, page.number, page.total_pages, processed, len(page.items), kept,
    )
