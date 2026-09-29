from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from gunb_tool.geocoding_uldk import GeocodeResult, GeoPrecision
from gunb_tool.models import Investment
from gunb_tool.storage import ChangeType, LeadRepository


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta) -> None:
        self.current += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def repo(clock):
    repository = LeadRepository(":memory:", now=clock, negative_cache_days=30)
    yield repository
    repository.close()


def lead(id_sprawy: str = "PL-OP/WNIOSEK/1/2026", **overrides) -> Investment:
    base = dict(
        id_sprawy=id_sprawy,
        zrodlo="pozwolenia",
        status="decyzja",
        data_aktualizacji="2026-09-25",
        kategoria="mieszkaniowa-jednorodzinna",
        nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego",
        adres_opisowy="ul. Testowa 5, Nysa",
        teryt_dzialki="160705_4.0005.13",
        dzialki=["160705_4.0005.13", "160705_4.0005.14"],
        lat=50.47,
        lon=17.33,
        precyzja_geo="dzialka",
        google_maps_url="https://www.google.com/maps?q=50.470000,17.330000",
        projektant="Jan Testowy",
        kubatura=650.5,
        is_residential=True,
    )
    base.update(overrides)
    return Investment(**base)


# --- Upsert i wykrywanie zmian ------------------------------------------------

def test_new_lead_is_inserted_with_history_entry(repo):
    result = repo.upsert(lead())

    assert result.change is ChangeType.NEW
    stored = repo.get("PL-OP/WNIOSEK/1/2026")
    assert stored.dzialki == ["160705_4.0005.13", "160705_4.0005.14"]
    assert stored.is_residential is True and stored.is_noise is False
    assert stored.czy_wyslano is False
    assert stored.utworzono == stored.status_zmieniony == "2026-09-28T08:00:00+00:00"
    history = repo.status_history("PL-OP/WNIOSEK/1/2026")
    assert [(h.stary_status, h.nowy_status) for h in history] == [(None, "decyzja")]


def test_identical_lead_is_unchanged_but_marked_as_seen(repo, clock):
    repo.upsert(lead())
    clock.advance(days=1)

    result = repo.upsert(lead())

    assert result.change is ChangeType.UNCHANGED
    stored = repo.get("PL-OP/WNIOSEK/1/2026")
    assert stored.ostatnio_widziany == "2026-09-29T08:00:00+00:00"
    assert stored.zmieniono == "2026-09-28T08:00:00+00:00"


def test_status_change_is_logged_and_resets_notification_flags(repo, clock):
    repo.upsert(lead(status="wniosek"))
    repo.mark_sent("PL-OP/WNIOSEK/1/2026", "telegram")
    clock.advance(days=3)

    result = repo.upsert(lead(status="decyzja"))

    assert result.change is ChangeType.STATUS_CHANGED
    assert (result.old_status, result.new_status) == ("wniosek", "decyzja")
    stored = repo.get("PL-OP/WNIOSEK/1/2026")
    assert stored.czy_wyslano is False
    assert stored.wyslano_kanaly == ""
    assert stored.status_zmieniony == "2026-10-01T08:00:00+00:00"
    change = repo.last_status_change("PL-OP/WNIOSEK/1/2026")
    assert (change.stary_status, change.nowy_status) == ("wniosek", "decyzja")


def test_content_update_keeps_notification_state(repo, clock):
    repo.upsert(lead())
    repo.mark_sent("PL-OP/WNIOSEK/1/2026", "telegram")
    clock.advance(hours=5)

    result = repo.upsert(lead(kubatura=700.0))

    assert result.change is ChangeType.UPDATED
    stored = repo.get("PL-OP/WNIOSEK/1/2026")
    assert stored.kubatura == 700.0
    assert stored.czy_wyslano is True
    assert stored.zmieniono == "2026-09-28T13:00:00+00:00"


def test_missing_coordinates_do_not_erase_stored_location(repo):
    repo.upsert(lead())

    result = repo.upsert(lead(lat=None, lon=None, precyzja_geo=None, google_maps_url=None))

    assert result.change is ChangeType.UNCHANGED
    assert repo.get("PL-OP/WNIOSEK/1/2026").lat == 50.47


def test_upsert_many_runs_in_one_transaction(repo):
    results = repo.upsert_many([lead("A/1"), lead("B/1"), lead("A/1", status="odmowa")])
    assert [r.change for r in results] == [ChangeType.NEW, ChangeType.NEW, ChangeType.STATUS_CHANGED]


# --- Kolejka powiadomień -------------------------------------------------------

def test_pending_notifications_are_tracked_per_channel(repo):
    repo.upsert(lead("A/1"))
    repo.mark_sent("A/1", "telegram")

    assert repo.pending_notifications("telegram", limit=10) == []
    assert [i.id_sprawy for i in repo.pending_notifications("discord", limit=10)] == ["A/1"]
    stored = repo.get("A/1")
    assert stored.czy_wyslano is True
    assert stored.wyslano_kanaly == "telegram"
    repo.mark_sent("A/1", "discord")
    repo.mark_sent("A/1", "discord")
    assert repo.get("A/1").wyslano_kanaly == "telegram,discord"


def test_pending_notifications_skip_noise_and_old_changes_and_respect_limit(repo, clock):
    repo.upsert(lead("OLD/1"))
    clock.advance(days=20)
    repo.upsert(lead("NOISE/1", is_noise=True, kategoria="szum"))
    repo.upsert(lead("NEW/1"))
    clock.advance(minutes=1)
    repo.upsert(lead("NEW/2"))

    pending = repo.pending_notifications("telegram", limit=10, max_age_days=14)
    assert [i.id_sprawy for i in pending] == ["NEW/1", "NEW/2"]
    assert [i.id_sprawy for i in repo.pending_notifications("telegram", limit=1, max_age_days=14)] == ["NEW/1"]


def test_mark_all_sent_empties_the_queue_for_all_channels(repo):
    repo.upsert_many([lead("A/1"), lead("B/1")])
    assert repo.mark_all_sent(["telegram", "discord"]) == 2
    assert repo.pending_notifications("telegram", limit=10) == []
    assert repo.pending_notifications("discord", limit=10) == []


# --- Synchronizacja arkusza ----------------------------------------------------

def test_sheet_sync_queue_contains_new_and_updated_leads(repo, clock):
    repo.upsert(lead("A/1"))
    assert [i.id_sprawy for i in repo.pending_sheet_sync()] == ["A/1"]

    repo.mark_synced(["A/1"])
    assert repo.pending_sheet_sync() == []

    clock.advance(minutes=5)
    repo.upsert(lead("A/1", projektant="Anna Testowa"))
    assert [i.id_sprawy for i in repo.pending_sheet_sync()] == ["A/1"]


# --- Cache geokodowania ----------------------------------------------------------

def geo_result() -> GeocodeResult:
    return GeocodeResult(
        lat=50.5, lon=17.3, precision=GeoPrecision.PARCEL, parcel_id="160705_4.0005.13",
        region_id="160705_4.0005", voivodeship="opolskie", county="powiat nyski", commune="Nysa",
        google_maps_url="https://www.google.com/maps?q=50.500000,17.300000", geoportal_url="https://geoportal.test",
    )


def test_geocode_cache_round_trips_results(repo):
    cache = repo.geocode_cache()
    assert cache.get("parcel:x") is None
    cache.set("parcel:x", geo_result())
    assert cache.get("parcel:x").result == geo_result()


def test_negative_geocode_entries_expire(repo, clock):
    cache = repo.geocode_cache()
    cache.set("parcel:missing", None)
    assert cache.get("parcel:missing").result is None
    clock.advance(days=31)
    assert cache.get("parcel:missing") is None


# --- Trwałość i statystyki ----------------------------------------------------------

def test_data_survives_reopening_file_database(tmp_path, clock):
    path = tmp_path / "db" / "leads.sqlite"
    with LeadRepository(path, now=clock) as first:
        first.upsert(lead())
    with LeadRepository(path, now=clock) as second:
        assert second.get("PL-OP/WNIOSEK/1/2026").projektant == "Jan Testowy"


def test_stats_summarise_database(repo):
    repo.upsert_many([lead("A/1"), lead("B/1", status="brak_sprzeciwu", zrodlo="zgloszenia"),
                      replace(lead("C/1"), kategoria="komercyjna")])
    repo.mark_sent("A/1", "telegram")
    stats = repo.stats()
    assert stats["razem"] == 3
    assert stats["statusy"] == {"decyzja": 2, "brak_sprzeciwu": 1}
    assert stats["kategorie"]["komercyjna"] == 1
    assert stats["niewyslane"] == 2
