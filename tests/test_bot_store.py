from datetime import datetime, timedelta, timezone

import pytest

from gunb_tool.bot_store import BotStore, UserFilters, investor_key, watch_match
from gunb_tool.models import Investment
from gunb_tool.storage import LeadRepository


class Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **delta) -> None:
        self.current += timedelta(**delta)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repo(clock):
    repository = LeadRepository(":memory:", now=clock)
    yield repository
    repository.close()


@pytest.fixture
def store(repo):
    return BotStore(repo)


def lead(id_sprawy="A/1", **overrides) -> Investment:
    base = dict(
        id_sprawy=id_sprawy, zrodlo="pozwolenia", status="decyzja", kategoria="mieszkaniowa-wielorodzinna",
        nazwa_zamierzenia="Budowa zespołu dwóch budynków wielorodzinnych", kubatura=26265.0,
        adres_opisowy="Warszawa", miejscowosc="Warszawa", gmina="Warszawa (miasto)", powiat="powiat Warszawa",
        powiat_teryt="1465", gmina_teryt="1465038", inwestor="Napollo 3 Sp. z o.o.", priorytet="hot", punkty=10,
    )
    base.update(overrides)
    return Investment(**base)


# --- Filtry użytkownika ------------------------------------------------------------------------

WARSZAWA_WIELORODZINNE = UserFilters(miejsca=("Warszawa",), kategorie=("mieszkaniowa-wielorodzinna",),
                                     min_kubatura=10000)


def test_example_filter_warsaw_multi_family_over_10000():
    assert WARSZAWA_WIELORODZINNE.matches(lead())
    assert not WARSZAWA_WIELORODZINNE.matches(lead(kubatura=994.0))
    assert not WARSZAWA_WIELORODZINNE.matches(lead(kategoria="komercyjna"))
    assert not WARSZAWA_WIELORODZINNE.matches(lead(gmina="Kostrzyn", miejscowosc="Wróblewo", adres_opisowy="Wróblewo",
                                                   powiat="powiat poznański", powiat_teryt="3021"))


OLSZTYNEK = dict(adres_opisowy="Olsztynek, ul. Mrongowiusza", miejscowosc="Olsztynek", gmina="Olsztynek",
                 powiat="olsztyński", powiat_teryt="2814", gmina_teryt="2814085")
DYWITY = dict(adres_opisowy="Dywity, ul. Olsztyńska", miejscowosc="Dywity", gmina="Dywity", powiat="olsztyński",
              powiat_teryt="2814", gmina_teryt="2814032")


def test_typed_town_matches_whole_words_not_similar_names():
    """„Olsztyn” to miasto – nie Olsztynek, nie cały powiat olsztyński ani ulica Olsztyńska w Dywitach."""
    olsztyn = UserFilters(miejsca=("Olsztyn",))
    assert olsztyn.matches(lead(adres_opisowy="Olsztyn, ul. Kętrzyńskiego", miejscowosc="Olsztyn", gmina="Olsztyn",
                                powiat="Olsztyn", powiat_teryt="2862"))
    assert not olsztyn.matches(lead(**OLSZTYNEK))
    assert not olsztyn.matches(lead(**DYWITY))
    assert UserFilters(miejsca=("olsztynek",)).matches(lead(**OLSZTYNEK))  # wielkość liter i ogonki bez znaczenia
    assert UserFilters(miejsca=("Nowe Kawkowo",)).matches(lead(miejscowosc="Nowe Kawkowo", gmina="Jonkowo"))


def test_known_place_uses_whole_words(store, repo):
    repo.upsert(lead("OLK/1", **OLSZTYNEK))
    assert store.place_is_known("Olsztynek") and store.place_is_known("olsztynek")
    assert not store.place_is_known("Olsztyn")


def test_min_volume_excludes_leads_with_unknown_volume():
    assert not UserFilters(min_kubatura=1000).matches(lead(kubatura=None))


def test_locations_are_alternatives():
    filters = UserFilters(powiaty=("3021",), miejsca=("Warszawa",))
    assert filters.matches(lead())
    assert filters.matches(lead(powiat_teryt="3021", gmina="Kostrzyn", miejscowosc="Wróblewo",
                                adres_opisowy="Wróblewo", powiat="powiat poznański"))


def test_investor_filter_company_only_or_name_fragment():
    assert UserFilters(inwestor="firma").matches(lead())
    assert not UserFilters(inwestor="firma").matches(lead(inwestor=None))
    assert UserFilters(inwestor="napollo").matches(lead())
    assert not UserFilters(inwestor="Budimex").matches(lead())


def test_empty_filters_match_everything_and_round_trip_json():
    assert UserFilters().matches(lead(kubatura=None, inwestor=None))
    assert UserFilters.from_json(WARSZAWA_WIELORODZINNE.to_json()) == WARSZAWA_WIELORODZINNE
    assert UserFilters.from_json("{zepsuty json") == UserFilters()


# --- Abonamenty (paywall) ------------------------------------------------------------------------------

def iso(moment) -> str:
    return moment.isoformat(timespec="seconds")


def test_subscribers_are_only_users_with_paid_access_plus_admins(store, clock):
    now = clock()
    for chat in (1, 2, 3, 4, 5):
        store.register(chat, f"Ekipa {chat}", None, status="aktywny", backlog_days=7)
    store.set_subscription(1, iso(now + timedelta(days=10)), active=True)  # opłacony
    store.set_subscription(2, iso(now - timedelta(days=1)), active=True)  # wygasł
    store.set_subscription(3, iso(now + timedelta(days=10)), active=False)  # wyłączony przez admina
    # 4 – nigdy nie płacił, 5 – administrator

    assert [u.chat_id for u in store.subscribers(iso(now), admins=(5,))] == [1, 5]
    assert [u.chat_id for u in store.subscribers(iso(now), admins=(5,), tryb="wieczor")] == []
    assert [u.chat_id for u in store.access_ended(now, admins=(5,))] == [2]  # do jednorazowej informacji o końcu


def test_new_user_starts_without_subscription(store):
    user = store.register(7, "Nowy", None, status="aktywny", backlog_days=7)
    assert (user.is_active, user.subscription_ends) == (False, None)
    assert not user.has_subscription("2026-09-29T06:00:00+00:00")


def test_subscription_is_valid_until_its_end(store, clock):
    store.register(7, "Nowy", None, status="aktywny", backlog_days=7)
    store.set_subscription(7, "2026-10-02T06:00:00+00:00", active=True)
    user = store.get_user(7)
    assert user.has_subscription("2026-10-02T05:59:59+00:00")
    assert not user.has_subscription("2026-10-02T06:00:00+00:00")


# --- „Blisko mnie”: promień od bazy firmy ------------------------------------------------------------

BAZA_OLSZTYN = (53.7784, 20.4801)
DYWITY = dict(lat=53.8285, lon=20.4867)     # ok. 5,6 km od bazy
BISKUPIEC = dict(lat=53.8649, lon=20.9569)  # ok. 33 km od bazy


def test_distance_from_base_is_measured_in_a_straight_line():
    filters = UserFilters(baza=BAZA_OLSZTYN)
    assert filters.distance_km(lead(**DYWITY)) == pytest.approx(5.6, abs=0.2)
    assert filters.distance_km(lead(**BISKUPIEC)) == pytest.approx(32.8, abs=0.5)
    assert filters.distance_km(lead()) is None  # lead bez współrzędnych
    assert UserFilters().distance_km(lead(**DYWITY)) is None  # brak bazy


def test_radius_keeps_only_leads_within_reach():
    filters = UserFilters(baza=BAZA_OLSZTYN, promien_km=15)
    assert filters.matches(lead(**DYWITY))
    assert not filters.matches(lead(**BISKUPIEC))
    assert not filters.matches(lead())  # bez lokalizacji nie wiadomo, czy blisko
    assert UserFilters(baza=BAZA_OLSZTYN, promien_km=50).matches(lead(**BISKUPIEC))


def test_radius_decides_instead_of_place_names():
    filters = UserFilters(baza=BAZA_OLSZTYN, promien_km=15, miejsca=("Gdańsk",))
    assert filters.matches(lead(**DYWITY))


def test_base_without_radius_does_not_filter():
    filters = UserFilters(baza=BAZA_OLSZTYN)
    assert filters.matches(lead(**BISKUPIEC))
    assert filters.is_empty()
    assert not UserFilters(baza=BAZA_OLSZTYN, promien_km=15).is_empty()


def test_base_and_radius_survive_saving():
    filters = UserFilters(baza=BAZA_OLSZTYN, promien_km=20, kategorie=("mieszkaniowa-jednorodzinna",))
    assert UserFilters.from_json(filters.to_json()) == filters


# --- Watchlista ----------------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name,key",
    [
        ("Napollo 3 Sp. z o.o.", "napollo"),
        ("NAPOLLO 4 SP. Z O.O.", "napollo"),
        ("Hevi MDM 1 Szafarowicz Sp.K.", "hevi mdm szafarowicz"),
        ("Budimex S.A.", "budimex"),
        ("BUDIMEX SA", "budimex"),
    ],
)
def test_investor_key_ignores_legal_form_and_spv_numbers(name, key):
    assert investor_key(name) == key


def test_investor_key_of_missing_investor_is_none():
    assert investor_key(None) is None
    assert investor_key("  ") is None


def test_watch_match_by_investor_or_gmina(store):
    store.register(1, "Mietek", None, status="aktywny", backlog_days=7)
    store.add_watch(1, "inwestor", investor_key("Napollo 3 Sp. z o.o."), "Napollo 3 Sp. z o.o.")
    store.add_watch(1, "gmina", "3021085", "Kostrzyn")
    items = store.watchlist(1)

    assert watch_match(lead(inwestor="Napollo 7 Sp. z o.o."), items).etykieta == "Napollo 3 Sp. z o.o."
    assert watch_match(lead(inwestor=None, gmina_teryt="3021085"), items).rodzaj == "gmina"
    assert watch_match(lead(inwestor="Inny Deweloper S.A."), items) is None


def test_watchlist_add_is_idempotent_and_removable(store):
    store.register(1, "Mietek", None, status="aktywny", backlog_days=7)
    assert store.add_watch(1, "gmina", "3021085", "Kostrzyn") is True
    assert store.add_watch(1, "gmina", "3021085", "Kostrzyn") is False
    (item,) = store.watchlist(1)
    assert store.remove_watch(1, item.id) is True
    assert store.watchlist(1) == []
    assert store.remove_watch(2, item.id) is False


# --- Użytkownicy ------------------------------------------------------------------------------

def test_register_creates_user_once_with_backlog_window(store, clock):
    user = store.register(10, "Mietek", "mietek_bud", status="oczekuje", backlog_days=7)
    assert (user.status, user.tryb, user.tylko_hot) == ("oczekuje", "rano", False)
    assert user.nowe_od == "2026-09-22T06:00:00+00:00"
    store.set_status(10, "aktywny")
    assert store.register(10, "Mietek", "mietek_bud", status="oczekuje", backlog_days=7).status == "aktywny"


def test_user_settings_round_trip(store):
    store.register(10, "Mietek", None, status="aktywny", backlog_days=7)
    store.set_mode(10, "natychmiast")
    store.set_hot_only(10, True)
    store.set_filters(10, WARSZAWA_WIELORODZINNE)
    store.set_awaiting(10, "miejsce")
    user = store.get_user(10)
    assert (user.tryb, user.tylko_hot, user.filtry, user.oczekuje_na) == (
        "natychmiast", True, WARSZAWA_WIELORODZINNE, "miejsce")
    assert [u.chat_id for u in store.users(tryb="natychmiast")] == [10]
    assert store.users(tryb="rano") == []


# --- Stany leadów i doręczenia -----------------------------------------------------------------

def test_saved_list_follows_lead_states(store, repo):
    store.register(1, "Mietek", None, status="aktywny", backlog_days=7)
    repo.upsert(lead("A/1"))
    repo.upsert(lead("B/1"))
    store.set_lead_state(1, "A/1", "zapisany")
    store.set_lead_state(1, "B/1", "zapisany")
    assert [i.id_sprawy for i in store.saved(1, limit=10)] == ["B/1", "A/1"]
    store.set_lead_state(1, "A/1", "przejrzany")  # przejrzenie nie zdejmuje zapisania (P0.3)
    assert store.saved_count(1) == 2
    store.set_lead_flags(1, "A/1", saved=False)
    assert [i.id_sprawy for i in store.saved(1, limit=10)] == ["B/1"]
    assert store.lead_state(1, "A/1") == "przejrzany"


def test_candidates_skip_noise_hidden_and_already_delivered(store, repo, clock):
    store.register(1, "Mietek", None, status="aktywny", backlog_days=7)
    repo.upsert(lead("A/1"))
    repo.upsert(lead("SZUM/1", is_noise=True))
    repo.upsert(lead("UKRYTY/1"))
    store.set_lead_state(1, "UKRYTY/1", "ukryty")
    since = store.get_user(1).nowe_od

    assert [i.id_sprawy for i in store.candidates(1, since)] == ["A/1"]
    store.record_delivery(1, [repo.get("A/1")], "raport")
    assert store.candidates(1, since) == []

    clock.advance(days=1)
    repo.upsert(lead("A/1", status="brak_sprzeciwu"))  # zmiana statusu = nowa rewizja
    assert [i.id_sprawy for i in store.candidates(1, since)] == ["A/1"]
    assert store.deliveries_since(1, since, "raport") == 1


def test_jobs_remember_last_run(store):
    assert store.job_last_run("raport_rano") is None
    store.mark_job("raport_rano", "2026-09-29T07:00:00")
    assert store.job_last_run("raport_rano") == "2026-09-29T07:00:00"
