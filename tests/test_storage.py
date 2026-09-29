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


# --- Tryb WAL (prawdziwy plik bazy) -------------------------------------------------------

def test_file_database_runs_in_wal_mode(tmp_path, clock):
    path = tmp_path / "leads.sqlite"
    with LeadRepository(path, now=clock) as repo:
        assert repo.journal_mode == "wal"
        assert repo.busy_timeout_ms == 5000
        repo.upsert(lead())
        assert (tmp_path / "leads.sqlite-wal").exists()
    with LeadRepository(path, now=clock) as reopened:
        assert reopened.journal_mode == "wal"


def test_reader_sees_committed_state_while_writer_transaction_is_open(tmp_path, clock):
    path = tmp_path / "leads.sqlite"
    with LeadRepository(path, now=clock) as writer, LeadRepository(path, now=clock) as reader:
        writer.upsert(lead("A/1"))
        with writer.transaction():
            writer.upsert(lead("B/1"))
            assert reader.get("A/1") is not None  # odczyt nie jest blokowany przez otwartą transakcję
            assert reader.get("B/1") is None      # i widzi tylko zatwierdzone dane
        assert reader.get("B/1") is not None


def test_memory_database_has_no_wal(repo):
    assert repo.journal_mode == "memory"


# --- Migracja schematu v1 -> v2 (kolumna segment) ----------------------------------------------

def test_version_1_database_is_migrated_with_segment_column(tmp_path, clock):
    import sqlite3
    from gunb_tool import storage

    path = tmp_path / "v1.sqlite"
    legacy = sqlite3.connect(path)
    legacy.executescript(storage._SCHEMA)
    legacy.execute("PRAGMA user_version = 1")
    legacy.execute(
        "INSERT INTO investments (id_sprawy, zrodlo, status, utworzono, zmieniono, status_zmieniony, ostatnio_widziany)"
        " VALUES ('OLD/1', 'pozwolenia', 'decyzja', 'x', 'x', 'x', 'x')"
    )
    legacy.commit()
    legacy.close()

    with LeadRepository(path, now=clock) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        assert repo.get("OLD/1").segment is None
        repo.upsert(lead("NEW/1", segment="domki"))
        assert repo.get("NEW/1").segment == "domki"
        assert repo.stats()["segmenty"] == {"bez segmentu": 1, "domki": 1}


# --- Migracja v2 -> v3 (scoring, numer leada, tabele bota) ---------------------------------------

def test_version_2_database_is_migrated_to_bot_schema(tmp_path, clock):
    import sqlite3
    from gunb_tool import storage

    path = tmp_path / "v2.sqlite"
    legacy = sqlite3.connect(path)
    for script in storage._MIGRATIONS[:2]:
        legacy.executescript(script)
    legacy.execute("PRAGMA user_version = 2")
    legacy.execute(
        "INSERT INTO investments (id_sprawy, zrodlo, status, utworzono, zmieniono, status_zmieniony, ostatnio_widziany)"
        " VALUES ('OLD/1', 'pozwolenia', 'decyzja', 'x', 'x', 'x', 'x')"
    )
    legacy.commit()
    legacy.close()

    with LeadRepository(path, now=clock) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        old = repo.get("OLD/1")
        assert old.nr == 1
        assert old.priorytet is None
        tables = {row[0] for row in repo.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"bot_users", "watchlist", "user_leads", "deliveries", "bot_jobs"} <= tables


# --- Migracja v3 -> v4 (kontakty z surowych pól) -------------------------------------------------

def test_version_3_database_gets_contact_columns(tmp_path, clock):
    import sqlite3
    from gunb_tool import storage

    path = tmp_path / "v3.sqlite"
    legacy = sqlite3.connect(path)
    for script in storage._MIGRATIONS[:3]:
        legacy.executescript(script)
    legacy.execute("PRAGMA user_version = 3")
    legacy.execute(
        "INSERT INTO investments (id_sprawy, zrodlo, status, utworzono, zmieniono, status_zmieniony, ostatnio_widziany)"
        " VALUES ('OLD/1', 'pozwolenia', 'decyzja', 'x', 'x', 'x', 'x')"
    )
    legacy.commit()
    legacy.close()

    with LeadRepository(path, now=clock) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        old = repo.get("OLD/1")
        assert (old.telefon, old.email) == (None, None)
        repo.upsert(lead("NEW/1", telefon="+48600123456", email="biuro@test.pl"))
        assert (repo.get("NEW/1").telefon, repo.get("NEW/1").email) == ("+48600123456", "biuro@test.pl")


def test_version_6_lead_states_become_independent_flags(tmp_path, clock):
    import sqlite3
    from gunb_tool import storage

    path = tmp_path / "v6.sqlite"
    legacy = sqlite3.connect(path)
    for script in storage._MIGRATIONS[:6]:
        legacy.executescript(script)
    legacy.execute("PRAGMA user_version = 6")
    legacy.execute("INSERT INTO bot_users (chat_id, status, nowe_od, utworzono, zmieniono) VALUES (1, 'aktywny', 'x', 'x', 'x')")
    for n, stan in enumerate(("zapisany", "przejrzany", "ukryty"), start=1):
        legacy.execute("INSERT INTO investments (id_sprawy, zrodlo, status, utworzono, zmieniono, status_zmieniony,"
                       " ostatnio_widziany) VALUES (?, 'pozwolenia', 'decyzja', 'x', 'x', 'x', 'x')", (f"L/{n}",))
        legacy.execute("INSERT INTO user_leads (chat_id, id_sprawy, stan, zmieniono) VALUES (1, ?, ?, 'x')",
                       (f"L/{n}", stan))
    legacy.commit()
    legacy.close()

    with LeadRepository(path, now=clock) as repo:
        from gunb_tool.bot_store import BotStore, LeadFlags
        store = BotStore(repo)
        assert store.lead_flags(1, "L/1") == LeadFlags(saved=True)
        assert store.lead_flags(1, "L/2") == LeadFlags(reviewed=True)
        assert store.lead_flags(1, "L/3") == LeadFlags(hidden=True)
        store.set_lead_flags(1, "L/1", reviewed=True)
        # stary kod czyta nadal kolumnę „stan” – dostaje wartość pochodną
        assert repo.connection.execute("SELECT stan FROM user_leads WHERE id_sprawy = 'L/1'").fetchone()[0] == "zapisany"
    with LeadRepository(path, now=clock) as again:  # ponowny start po migracji
        assert again.connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION


def test_historical_import_dates_new_leads_by_their_decision(repo):
    """Import 18 miesięcy wstecz nie może zalać klientów „nowościami” – liczy się data decyzji."""
    repo.upsert_many([lead("OLD/1", data_aktualizacji="2025-06-01")], historical=True)

    assert repo.get("OLD/1").status_zmieniony.startswith("2025-06-01")
    assert repo.pending_notifications("telegram", limit=10, max_age_days=14) == []


def test_historical_import_keeps_leads_already_known(repo, clock):
    repo.upsert(lead("A/1"))
    appeared = repo.get("A/1").status_zmieniony
    clock.advance(days=3)
    repo.upsert_many([lead("A/1", data_aktualizacji="2025-06-01")], historical=True)
    assert repo.get("A/1").status_zmieniony == appeared


def test_historical_import_of_lead_without_date_uses_now(repo, clock):
    repo.upsert_many([lead("X/1", data_aktualizacji=None)], historical=True)
    assert repo.get("X/1").status_zmieniony == clock().isoformat(timespec="seconds")


def test_found_contact_updates_lead_without_sending_it_again(repo):
    repo.upsert(lead("A/1"))
    repo.mark_sent("A/1", "telegram")

    result = repo.upsert(lead("A/1", telefon="+48600123456"))

    assert result.change is ChangeType.UPDATED
    assert repo.get("A/1").telefon == "+48600123456"
    assert repo.pending_notifications("telegram", limit=10) == []  # nowa kolumna nie powtarza powiadomień


def test_leads_get_sequential_numbers_that_survive_updates(repo):
    repo.upsert(lead("A/1"))
    repo.upsert(lead("B/1"))
    repo.upsert(lead("A/1", kubatura=999.0))
    assert repo.get("A/1").nr == 1
    assert repo.get_by_nr(2).id_sprawy == "B/1"
    assert repo.get_by_nr(99) is None
