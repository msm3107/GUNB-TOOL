"""Kolejność według zastosowania osoby (okno etapu branży), miejsca (odległość) i daty – nie według skali."""

from datetime import date, timedelta

from gunb_tool.bot_store import BotUser, UserFilters
from gunb_tool.ranking import match_reasons, ranked, stage_note
from gunb_tool.stages import get_trade
from tests.bot_helpers import lead

TODAY = date(2026, 9, 29)
BASE = (53.7784, 20.4801)
NEAR, FAR = dict(lat=53.80, lon=20.49), dict(lat=54.05, lon=20.48)  # ok. 2 km i 30 km od bazy


def decided(days_ago: int) -> dict:
    day = (TODAY - timedelta(days=days_ago)).isoformat()
    return dict(data_decyzji=day, data_aktualizacji=day)


def person(**values) -> BotUser:
    return BotUser(chat_id=1, imie=None, username=None, status="aktywny", tryb="rano", tylko_hot=False, **values)


def names(leads):
    return [inv.nazwa_zamierzenia for inv in leads]


def test_later_trade_sees_buildings_in_its_window_first():
    user = person(branza="dach", filtry=UserFilters(baza=BASE, promien_km=50))
    leads = [lead("1", nazwa_zamierzenia="świeży wielki blok", priorytet="hot", kubatura=30000.0, **NEAR, **decided(10)),
             lead("2", nazwa_zamierzenia="daleko w oknie", **FAR, **decided(150)),
             lead("3", nazwa_zamierzenia="blisko w oknie, starszy", **NEAR, **decided(170)),
             lead("4", nazwa_zamierzenia="blisko w oknie, nowszy", **NEAR, **decided(135)),
             lead("5", nazwa_zamierzenia="okno za 2 tygodnie", **NEAR, **decided(108))]
    assert names(ranked(user, leads, TODAY)) == ["blisko w oknie, nowszy", "blisko w oknie, starszy",
                                                 "daleko w oknie", "okno za 2 tygodnie", "świeży wielki blok"]


def test_early_trade_and_materials_see_the_newest_first_size_does_not_matter():
    user = person(branza="materialy")
    leads = [lead("1", nazwa_zamierzenia="stary wielki", priorytet="hot", kubatura=30000.0, **decided(20)),
             lead("2", nazwa_zamierzenia="nowy mały dom", priorytet="low", kubatura=300.0, **decided(2)),
             lead("3", nazwa_zamierzenia="średni", **decided(9))]
    assert names(ranked(user, leads, TODAY)) == ["nowy mały dom", "średni", "stary wielki"]


def test_reasons_name_the_users_own_settings():
    filters = UserFilters(miejsca=("Dywity", "Barczewo"), kategorie=("mieszkaniowa-jednorodzinna",), min_kubatura=500)
    inv = lead("1", miejscowosc="Dywity", gmina="Dywity", adres_opisowy="Dywity", **decided(150))
    reasons = match_reasons(person(filtry=filters, branza="dach"), inv, {}, TODAY)
    assert reasons == ["Dywity", "domy jednorodzinne", "kubatura od 500 m³", "okno etapu dachu (szacunek)"]
    assert match_reasons(person(), inv, {}, TODAY) == ["cały monitorowany obszar"]
    assert match_reasons(person(filtry=UserFilters(baza=BASE, promien_km=15)), inv, {}, TODAY) == \
        ["do 15 km od Twojej bazy"]


def test_stage_note_says_when_and_that_it_must_be_checked():
    trade = get_trade("dach")
    assert stage_note(trade, lead("1", **decided(150)), TODAY) == (
        "⏳ Etap dachu: orientacyjnie 01.09–01.11.2026 (od daty decyzji) – teraz; trzeba sprawdzić na miejscu")
    assert "– za ok. 2 tyg.;" in stage_note(trade, lead("1", **decided(108)), TODAY)
    assert "– minęło;" in stage_note(trade, lead("1", **decided(300)), TODAY)
    assert "orientacyjnie 30.11.2026–30.01.2027 (od daty decyzji)" in stage_note(trade, lead("1", **decided(60)), TODAY)
    assert stage_note(get_trade("materialy"), lead("1", **decided(5)), TODAY) is None
