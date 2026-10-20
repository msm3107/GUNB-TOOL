"""Orkiestracja: pobieranie → filtrowanie → geokodowanie → zapis, powiadomienia i synchronizacja arkusza."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any, Callable, Iterator, Protocol, Sequence

from .config import AppConfig, GunbConfig
from .contacts import extract_contact
from .data_filter import FilterDecision, LeadFilter
from .geocoding_uldk import GeocodeResult, GeoPrecision, UldkClient, UldkGeocoder, google_maps_url
from .gunb_scraper import FetchQuery, GunbScraper, Page
from .http_client import ResilientHttpClient
from .models import GunbCase, Investment
from .scoring import score_investment
from .storage import GEO_FIELDS, ChangeType, LeadRepository

log = logging.getLogger(__name__)

IMPORT_LEASE = "import"
IMPORT_LEASE_TTL = timedelta(minutes=30)
"""Blokada importu wygasa sama po awarii procesu; działający import odnawia ją po każdej stronie."""


class ImportSkipped(RuntimeError):
    """Import się nie odbył, choć nic nie jest zepsute (np. trwa inny import, program się zamyka).

    ``retry_in`` – po jakim czasie spróbować ponownie.
    """

    def __init__(self, reason: str, *, retry_in: timedelta = timedelta(hours=1)) -> None:
        super().__init__(reason)
        self.retry_in = retry_in


class EmptyImport(RuntimeError):
    """Zwykły import nie przyniósł ani jednej sprawy – to podejrzana paczka GUNB albo zły zakres,
    więc nie liczy się jako udane sprawdzenie rejestru (dane w bazie zostają nietknięte)."""


@contextmanager
def import_lease(repo: LeadRepository, *, owner: str | None = None, wait: timedelta = timedelta(0),
                 ttl: timedelta = IMPORT_LEASE_TTL,
                 sleep: Callable[[float], None] = time.sleep) -> Iterator[Callable[[], bool]]:
    """Jeden import naraz – bot, ``--fetch`` z harmonogramu systemu i import historii dzielą blokadę w bazie.

    Zwraca funkcję odnawiającą blokadę (``False`` = blokadę przejął ktoś inny – import powinien się skończyć).

    Args:
        owner: identyfikator właściciela (domyślnie host:PID:wątek).
        wait: ile czekać na zakończenie cudzego importu (np. ręczny ``--fetch``); 0 – od razu zrezygnuj.

    Raises:
        ImportSkipped: importuje inny proces.
    """
    owner = owner or f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
    waited = 0.0
    while not repo.acquire_lease(IMPORT_LEASE, owner, ttl):
        if waited >= wait.total_seconds():
            raise ImportSkipped(f"trwa inny import danych ({repo.lease_holder(IMPORT_LEASE) or 'inny proces'})")
        if not waited:
            log.info("Trwa inny import danych – czekam na jego koniec (najwyżej %d min)", wait.total_seconds() // 60)
        sleep(15.0)
        waited += 15.0
    try:
        yield lambda: repo.renew_lease(IMPORT_LEASE, owner, ttl)
    finally:
        repo.release_lease(IMPORT_LEASE, owner)


class PageSource(Protocol):
    """Źródło stron spraw (``GunbScraper`` lub atrapa w testach)."""

    def fetch_pages(self, query: FetchQuery, page_size: int = 200) -> Any: ...


class Geocoder(Protocol):
    """Geokoder spraw (``UldkGeocoder`` lub atrapa w testach)."""

    def geocode(self, parcels: Sequence[Any], *, gmina_teryt: str | None = None) -> GeocodeResult | None: ...


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
    interrupted: bool = False
    """Import przerwany między stronami (zamykanie programu albo utrata blokady) – zapisane strony zostają."""

    def summary(self) -> str:
        """Krótki opis wyniku (np. do stanu zadań widocznego dla admina)."""
        text = f"nowe {self.new}, zmiany statusu {self.status_changed}, sprawy {self.cases}"
        return text + (" – przerwany" if self.interrupted else "")


class LeadPipeline:
    """Łączy moduły w potok pobierania, filtrowania, geokodowania i zapisu.

    Args:
        repo: repozytorium leadów.
        scraper: źródło stron spraw GUNB (wymagane dla :meth:`fetch`).
        lead_filter: kategoryzacja i odrzucanie szumu (wymagane dla :meth:`fetch`).
        geocoder: geokoder ULDK; ``None`` = bez geokodowania.
    """

    def __init__(
        self,
        repo: LeadRepository,
        *,
        scraper: PageSource | None = None,
        lead_filter: LeadFilter | None = None,
        geocoder: Geocoder | None = None,
    ) -> None:
        self.repo = repo
        self.scraper = scraper
        self.lead_filter = lead_filter
        self.geocoder = geocoder

    # --- Pobieranie ------------------------------------------------------------------

    def fetch(self, query: FetchQuery, *, page_size: int, limit: int | None = None,
              historical: bool = False, should_continue: Callable[[], bool] | None = None) -> FetchReport:
        """Pobiera sprawy stronami, filtruje, geokoduje i zapisuje (jedna transakcja na stronę).

        Args:
            query: zakres danych.
            page_size: liczba spraw na stronę.
            limit: maksymalna liczba przetworzonych spraw (np. do testów konfiguracji).
            historical: import historyczny – nowe sprawy nie są „nowościami” (patrz ``LeadRepository.upsert``).
            should_continue: wywoływane po każdej zapisanej stronie; ``False`` kończy import
                (``report.interrupted``) – np. przy zamykaniu programu albo po utracie blokady importu.
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
            if should_continue is not None and not should_continue():
                report.interrupted = True
                log.warning("Import przerwany po %d stronach – zapisane strony zostają", report.pages)
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

    def mark_all_sent(self, channels: Sequence[str]) -> int:
        """Oznacza wszystkie oczekujące leady jako wysłane."""
        return self.repo.mark_all_sent(channels)


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
    )


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _log_page(page: Page, processed: int, kept: int) -> None:
    log.info(
        "[%s] strona %d/%d: przetworzono %d z %d spraw, zachowano %d",
        page.label, page.number, page.total_pages, processed, len(page.items), kept,
    )
