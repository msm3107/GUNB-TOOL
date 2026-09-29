"""Orkiestracja: pobieranie → filtrowanie → geokodowanie → zapis, powiadomienia i synchronizacja arkusza."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Callable, Protocol, Sequence

from .config import AppConfig, GunbConfig
from .contacts import extract_contact
from .data_filter import FilterDecision, LeadFilter
from .exporter import MessageFormatter, NotificationError, Notifier, OutgoingMessage, SheetsSyncResult
from .geocoding_uldk import GeocodeResult, GeoPrecision, UldkClient, UldkGeocoder, google_maps_url
from .gunb_scraper import FetchQuery, GunbScraper, Page
from .http_client import ResilientHttpClient
from .models import GunbCase, Investment
from .scoring import score_investment
from .storage import GEO_FIELDS, ChangeType, LeadRepository

log = logging.getLogger(__name__)


class PageSource(Protocol):
    """Źródło stron spraw (``GunbScraper`` lub atrapa w testach)."""

    def fetch_pages(self, query: FetchQuery, page_size: int = 200) -> Any: ...


class Geocoder(Protocol):
    """Geokoder spraw (``UldkGeocoder`` lub atrapa w testach)."""

    def geocode(self, parcels: Sequence[Any], *, gmina_teryt: str | None = None) -> GeocodeResult | None: ...


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
    messages: int = 0
    leads: int = 0
    digests: int = 0
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
    """

    def __init__(
        self,
        repo: LeadRepository,
        *,
        scraper: PageSource | None = None,
        lead_filter: LeadFilter | None = None,
        geocoder: Geocoder | None = None,
        formatter: MessageFormatter | None = None,
    ) -> None:
        self.repo = repo
        self.scraper = scraper
        self.lead_filter = lead_filter
        self.geocoder = geocoder
        self.formatter = formatter or MessageFormatter()

    # --- Pobieranie ------------------------------------------------------------------

    def fetch(self, query: FetchQuery, *, page_size: int, limit: int | None = None,
              historical: bool = False) -> FetchReport:
        """Pobiera sprawy stronami, filtruje, geokoduje i zapisuje (jedna transakcja na stronę).

        Args:
            query: zakres danych.
            page_size: liczba spraw na stronę.
            limit: maksymalna liczba przetworzonych spraw (np. do testów konfiguracji).
            historical: import historyczny – nowe sprawy nie są „nowościami” (patrz ``LeadRepository.upsert``).
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
            for result in self.repo.upsert_many(batch, historical=historical):
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
        result = None
        if self.geocoder is not None and case.parcels:
            try:
                result = self.geocoder.geocode(case.parcels, gmina_teryt=case.terc)
            except Exception:  # geokodowanie jest wzbogaceniem – nie może przerwać zapisu strony
                log.exception("Nieoczekiwany błąd geokodowania sprawy %s", case.id_sprawy)
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
        digest_threshold: int = 10,
        dry_run: bool = False,
        output: Callable[[str], None] = print,
        max_failures: int = 3,
    ) -> NotifyReport:
        """Wysyła oczekujące leady na kanał ``notifier.channel`` i oznacza je jako wysłane.

        Leady są grupowane według miejsca docelowego (np. czat segmentu). Gdy w grupie jest ich
        więcej niż ``digest_threshold``, zamiast pojedynczych wiadomości wysyłany jest raport zbiorczy.
        Tempo wysyłki pilnuje notyfikator (kolejka z limitem). Odrzucona wiadomość zostaje w kolejce,
        a po ``max_failures`` kolejnych błędach wysyłka jest przerywana. W trybie ``dry_run``
        wiadomości trafiają do ``output`` i nie są oznaczane jako wysłane.
        """
        channel = notifier.channel
        single = self.formatter.telegram if channel == "telegram" else self.formatter.discord
        digest = self.formatter.telegram_digest if channel == "telegram" else self.formatter.discord_digest
        report = NotifyReport(channel=channel, dry_run=dry_run)

        groups: dict[str, list[Investment]] = {}
        for investment in self.repo.pending_notifications(channel, limit=limit, max_age_days=max_age_days):
            groups.setdefault(notifier.destination(investment.segment), []).append(investment)

        consecutive_failures = 0
        for destination, leads in groups.items():
            changes = {inv.id_sprawy: self.repo.last_status_change(inv.id_sprawy) for inv in leads}
            if len(leads) > digest_threshold:
                messages = digest(leads, changes)
                report.digests += 1
            else:
                messages = [single(inv, changes[inv.id_sprawy]) for inv in leads]

            for message in messages:
                if dry_run:
                    _preview(message, channel, destination, output)
                    report.messages += 1
                    report.leads += len(message.lead_ids)
                    continue
                try:
                    notifier.send(message, destination)
                except NotificationError as exc:
                    report.failed += 1
                    consecutive_failures += 1
                    log.error("%s: nie wysłano (%s): %s", channel, ", ".join(message.lead_ids[:3]), exc)
                    if consecutive_failures >= max_failures:
                        report.aborted = True
                        log.error("%s: %d kolejnych błędów – przerywam wysyłkę", channel, consecutive_failures)
                        return report
                    continue
                consecutive_failures = 0
                log.info("%s → %s: wysłano wiadomość (leady: %d)", channel, destination, len(message.lead_ids))
                for lead_id in message.lead_ids:
                    self.repo.mark_sent(lead_id, channel)
                report.messages += 1
                report.leads += len(message.lead_ids)
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
    # Tylko pola inwestora i projektanta – w opisach zamierzeń bywają stopki urzędów (telefon starostwa).
    telefon, email = extract_contact(case.inwestor_raw, case.projektant_imie, case.projektant_nazwisko,
                                     case.projektant_uprawnienia)
    investment = Investment(
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
        segment=decision.segment,
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
        telefon=telefon,
        email=email,
    )
    score = score_investment(investment)
    investment.punkty, investment.priorytet = score.points, score.priority
    return investment


def apply_geocode(investment: Investment, result: GeocodeResult) -> None:
    """Uzupełnia lead o lokalizację z ULDK."""
    if result.precision is GeoPrecision.PARCEL and result.parcel_id:
        investment.teryt_dzialki = result.parcel_id  # identyfikator potwierdzony przez ULDK
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
            cooldown=config.http.circuit_breaker_cooldown,
        )
    return LeadPipeline(
        repo,
        scraper=scraper,
        lead_filter=LeadFilter(config.filter, config.segments),
        geocoder=geocoder,
        formatter=build_formatter(config),
    )


def build_formatter(config: AppConfig) -> MessageFormatter:
    """Formater wiadomości z etykietami segmentów z konfiguracji."""
    return MessageFormatter(segment_labels={segment.name: segment.label for segment in config.segments})


def notification_http_client(config: AppConfig) -> ResilientHttpClient:
    """Klient HTTP dla powiadomień: bez losowych opóźnień (tempo wyznacza ``delay_seconds``).

    Bez bezpiecznika – serię nieudanych wysyłek przerywa już ``LeadPipeline.notify``.
    """
    return ResilientHttpClient(replace(config.http, min_delay=0.0, max_delay=0.0, max_retries=3,
                                       circuit_breaker_failures=0))


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _preview(message: OutgoingMessage, channel: str, destination: str, output: Callable[[str], None]) -> None:
    """Podgląd wiadomości w trybie ``--dry-run``."""
    output(f"── {channel} → {destination} ({len(message.lead_ids)} lead.) ──")
    output(message.text)
    if message.buttons:
        output("[przyciski] " + " | ".join(f"{label}: {url}" for label, url in message.buttons))


def _log_page(page: Page, processed: int, kept: int) -> None:
    log.info(
        "[%s] strona %d/%d: przetworzono %d z %d spraw, zachowano %d",
        page.label, page.number, page.total_pages, processed, len(page.items), kept,
    )
