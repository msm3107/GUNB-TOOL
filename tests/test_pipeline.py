from datetime import date

import pytest

from gunb_tool.config import FilterConfig, GunbConfig
from gunb_tool.data_filter import LeadFilter
from gunb_tool.exporter import NotificationError, SheetsSyncResult
from gunb_tool.geocoding_uldk import GeocodeResult, GeoPrecision
from gunb_tool.gunb_scraper import FetchQuery, Page
from gunb_tool.models import GunbCase, Parcel, Source, Status
from gunb_tool.pipeline import LeadPipeline, build_query
from gunb_tool.storage import LeadRepository


def gunb_case(id_sprawy: str, description: str = "Budowa budynku mieszkalnego jednorodzinnego",
              category: str = "I", **overrides) -> GunbCase:
    base = dict(
        id_sprawy=id_sprawy,
        source=Source.POZWOLENIA,
        status=Status.DECYZJA,
        data_wplywu=date(2026, 8, 1),
        data_decyzji=date(2026, 9, 20),
        numer_decyzji="1/2026",
        organ="Starosta Powiatu Testowego",
        miasto="Nysa",
        terc="1607054",
        ulica="ul. Testowa",
        nr_domu="5",
        kategoria_obiektu=category,
        rodzaj_robot="budowa nowego/nowych obiektów budowlanych",
        nazwa_zamierzenia=description,
        kubatura=600.0,
        inwestor_raw="Firma Testowa Sp. z o.o.",
        projektant_imie="JAN",
        projektant_nazwisko="TESTOWY",
        projektant_uprawnienia="OPL/1/20",
        parcels=[Parcel("160705_4", "0005", "13"), Parcel("160705_4", "0005", "14")],
    )
    base.update(overrides)
    return GunbCase(**base)


class FakeScraper:
    def __init__(self, cases, page_size_seen=None):
        self.cases = cases
        self.queries = []

    def fetch_pages(self, query, page_size=200):
        self.queries.append((query, page_size))
        for start in range(0, len(self.cases), page_size):
            chunk = self.cases[start:start + page_size]
            yield Page(Source.POZWOLENIA, "test", start // page_size + 1,
                       -(-len(self.cases) // page_size), len(self.cases), chunk)


class FakeGeocoder:
    def __init__(self):
        self.calls = []

    def geocode(self, parcels):
        self.calls.append([p.uldk_id for p in parcels])
        return GeocodeResult(
            lat=50.47, lon=17.33, precision=GeoPrecision.PARCEL, parcel_id=parcels[0].uldk_id,
            region_id=parcels[0].obreb_id, voivodeship="opolskie", county="powiat nyski", commune="Nysa",
            google_maps_url="https://maps.test/50.47,17.33", geoportal_url="https://geoportal.test/p",
        )


class FakeNotifier:
    """Notyfikator zapamiętujący wiadomości; ``fail_on`` = numery prób, które mają się nie udać ("*" = wszystkie)."""

    def __init__(self, channel="telegram", fail_on=()):
        self.channel = channel
        self.sent = []
        self.attempts = 0
        self.fail_on = set(fail_on)

    def send(self, text):
        attempt, self.attempts = self.attempts, self.attempts + 1
        if "*" in self.fail_on or attempt in self.fail_on:
            raise NotificationError("odrzucone")
        self.sent.append(text)


@pytest.fixture
def repo():
    repository = LeadRepository(":memory:")
    yield repository
    repository.close()


def make_pipeline(repo, cases, geocoder=None, sleeps=None):
    return LeadPipeline(
        repo,
        scraper=FakeScraper(cases),
        lead_filter=LeadFilter(FilterConfig()),
        geocoder=geocoder,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


QUERY = FetchQuery(voivodeships=("16",))


# --- fetch --------------------------------------------------------------------------

def test_fetch_stores_kept_leads_and_counts_dropped_noise(repo):
    geocoder = FakeGeocoder()
    cases = [gunb_case("A/1"), gunb_case("B/1", "Budowa ogrodzenia działki", "VIII"), gunb_case("C/1")]

    report = make_pipeline(repo, cases, geocoder).fetch(QUERY, page_size=2)

    assert (report.pages, report.cases, report.kept, report.new) == (2, 3, 2, 2)
    assert sum(report.dropped.values()) == 1
    assert report.geocoded == 2
    stored = repo.get("A/1")
    assert stored.status == "decyzja"
    assert stored.data_decyzji == "2026-09-20"
    assert stored.data_aktualizacji == "2026-09-20"
    assert stored.kategoria == "mieszkaniowa-jednorodzinna"
    assert stored.projektant == "Jan Testowy"
    assert stored.inwestor == "Firma Testowa Sp. z o.o."
    assert stored.adres_opisowy == "ul. Testowa 5, Nysa"
    assert stored.teryt_dzialki == "160705_4.0005.13"
    assert stored.dzialki == ["160705_4.0005.13", "160705_4.0005.14"]
    assert (stored.lat, stored.lon, stored.gmina) == (50.47, 17.33, "Nysa")
    assert stored.google_maps_url == "https://www.google.com/maps?q=50.470000,17.330000"
    assert repo.get("B/1") is None


def test_refetch_reuses_stored_location_instead_of_geocoding_again(repo):
    geocoder = FakeGeocoder()
    make_pipeline(repo, [gunb_case("A/1")], geocoder).fetch(QUERY, page_size=10)

    report = make_pipeline(repo, [gunb_case("A/1")], geocoder).fetch(QUERY, page_size=10)

    assert report.unchanged == 1
    assert report.geocode_reused == 1
    assert len(geocoder.calls) == 1


def test_status_change_is_reported(repo):
    make_pipeline(repo, [gunb_case("A/1", status=Status.WNIOSEK, data_decyzji=None)]).fetch(QUERY, page_size=10)
    report = make_pipeline(repo, [gunb_case("A/1")]).fetch(QUERY, page_size=10)
    assert report.status_changed == 1


def test_designer_studio_is_stored_as_projektant_and_pracownia(repo):
    case = gunb_case("A/1", projektant_imie=None, projektant_nazwisko="Pracownia Projektowa Testowa")
    make_pipeline(repo, [case]).fetch(QUERY, page_size=10)
    stored = repo.get("A/1")
    assert stored.projektant == stored.pracownia == "Pracownia Projektowa Testowa"


def test_limit_stops_processing(repo):
    report = make_pipeline(repo, [gunb_case(f"X/{i}") for i in range(5)]).fetch(QUERY, page_size=2, limit=3)
    assert report.cases == 3
    assert repo.stats()["razem"] == 3


# --- notify ------------------------------------------------------------------------------

def seed(repo, count=3):
    make_pipeline(repo, [gunb_case(f"L/{i}") for i in range(count)]).fetch(QUERY, page_size=10)


def test_notify_sends_marks_and_throttles(repo):
    seed(repo)
    sleeps = []
    notifier = FakeNotifier()
    report = make_pipeline(repo, [], sleeps=sleeps).notify(notifier, limit=10, max_age_days=14, delay=1.5)

    assert (report.sent, report.failed) == (3, 0)
    assert all(text.startswith("🏗️ *NOWY LEAD*") for text in notifier.sent)
    assert sleeps == [1.5, 1.5]
    assert repo.pending_notifications("telegram", limit=10) == []
    assert repo.get("L/0").czy_wyslano is True


def test_notify_dry_run_prints_without_marking(repo):
    seed(repo, count=1)
    printed = []
    report = make_pipeline(repo, []).notify(
        FakeNotifier("discord"), limit=10, max_age_days=14, delay=0, dry_run=True, output=printed.append
    )
    assert report.sent == 1 and report.dry_run
    assert printed[0].startswith("🏗️ **NOWY LEAD**")
    assert len(repo.pending_notifications("discord", limit=10)) == 1


def test_notify_respects_limit(repo):
    seed(repo, count=3)
    notifier = FakeNotifier()
    make_pipeline(repo, []).notify(notifier, limit=2, max_age_days=14, delay=0)
    assert len(notifier.sent) == 2
    assert len(repo.pending_notifications("telegram", limit=10)) == 1


def test_notify_skips_single_failure_and_aborts_after_consecutive_failures(repo):
    seed(repo, count=3)
    flaky = FakeNotifier(fail_on={0})
    report = make_pipeline(repo, []).notify(flaky, limit=10, max_age_days=14, delay=0)
    assert (report.sent, report.failed) == (2, 1)

    seed(repo, count=0)
    broken = FakeNotifier("discord", fail_on={"*"})
    report = make_pipeline(repo, []).notify(broken, limit=10, max_age_days=14, delay=0, max_failures=2)
    assert (report.sent, report.failed) == (0, 2)
    assert report.aborted


# --- sheets i zapytanie ----------------------------------------------------------------------

class FakeExporter:
    def __init__(self):
        self.batches = []

    def export(self, investments):
        self.batches.append([i.id_sprawy for i in investments])
        return SheetsSyncResult(0, len(investments))


def test_sync_sheets_exports_pending_and_marks_synced(repo):
    seed(repo, count=2)
    exporter = FakeExporter()
    pipeline = make_pipeline(repo, [])

    assert pipeline.sync_sheets(exporter) == SheetsSyncResult(0, 2)
    assert pipeline.sync_sheets(exporter) == SheetsSyncResult(0, 0)
    assert len(exporter.batches) == 1


def test_build_query_uses_lookback_window_or_explicit_dates():
    cfg = GunbConfig(voivodeships=("16",), powiats=("1607",), lookback_days=60, date_field="wplyw",
                     sources=(Source.POZWOLENIA, Source.ZGLOSZENIA))
    today = date(2026, 9, 28)

    default = build_query(cfg, today=today)
    assert default.date_from == date(2026, 7, 30)
    assert default.date_to is None
    assert default.powiats == frozenset({"1607"})
    assert default.date_field == "wplyw"
    assert default.sources == (Source.POZWOLENIA, Source.ZGLOSZENIA)

    assert build_query(cfg, today=today, days=7).date_from == date(2026, 9, 21)
    explicit = build_query(cfg, today=today, since=date(2026, 1, 1), until=date(2026, 2, 1))
    assert (explicit.date_from, explicit.date_to) == (date(2026, 1, 1), date(2026, 2, 1))
