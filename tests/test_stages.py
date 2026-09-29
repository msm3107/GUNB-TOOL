"""„Kiedy dzwonić”: okna czasowe etapów budowy liczone od daty pozwolenia."""

from datetime import date, timedelta

import pytest

from gunb_tool.models import Investment
from gunb_tool.stages import TRADES, get_trade, is_due, stage_window

TODAY = date(2026, 9, 29)


def decided(months: float) -> str:
    return (TODAY - timedelta(days=round(months * 30.44))).isoformat()


def house(months_ago: float | None = None, kategoria: str = "mieszkaniowa-jednorodzinna", **dates) -> Investment:
    if months_ago is not None:
        dates = {"data_decyzji": decided(months_ago), "data_aktualizacji": decided(months_ago)}
    return Investment(id_sprawy="D/1", zrodlo="pozwolenia", status="decyzja", kategoria=kategoria, **dates)


@pytest.mark.parametrize(
    "trade, months_ago, due",
    [
        ("dach", 3.5, False),  # za wcześnie – dopiero fundamenty
        ("dach", 4.2, True),
        ("dach", 5.9, True),
        ("dach", 6.3, False),  # za późno – dach już stoi
        ("okna", 6.0, True),
        ("instalacje", 7.0, True),
        ("elewacja", 10.0, True),
        ("wykonczenia", 12.0, True),
        ("ogrodzenie", 17.0, True),
        ("ogrodzenie", 9.0, False),
    ],
)
def test_single_family_house_stage_windows(trade, months_ago, due):
    assert is_due(get_trade(trade), house(months_ago), TODAY) is due


def test_bigger_buildings_take_half_as_long_again():
    dach = get_trade("dach")
    assert not is_due(dach, house(5, kategoria="mieszkaniowa-wielorodzinna"), TODAY)  # blok: 6–9 mies.
    assert is_due(dach, house(7, kategoria="mieszkaniowa-wielorodzinna"), TODAY)
    assert is_due(dach, house(8.5, kategoria="komercyjna"), TODAY)


def test_foundation_trade_has_no_reminders_because_new_leads_already_come_at_once():
    trade = get_trade("stan_surowy")
    assert trade.months is None
    assert stage_window(trade, house(1)) is None
    assert not is_due(trade, house(1), TODAY)


def test_notification_without_decision_counts_from_filing_date():
    zgloszenie = house(data_wplywu=decided(5), data_aktualizacji=decided(5))
    assert is_due(get_trade("dach"), zgloszenie, TODAY)


def test_lead_without_dates_is_never_due():
    assert stage_window(get_trade("dach"), house()) is None


def test_trades_have_unique_keys_and_unknown_key_is_none():
    keys = [t.key for t in TRADES]
    assert len(keys) == len(set(keys))
    assert get_trade("pilot") is None and get_trade(None) is None
