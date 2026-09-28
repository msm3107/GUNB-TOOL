from datetime import date

import pytest

from gunb_tool.models import GunbCase, Parcel, Source, Status
from gunb_tool.teryt import get_voivodeship
from gunb_tool.text import clean, normalize_text


def make_case(**overrides) -> GunbCase:
    base = dict(id_sprawy="ST-XX/WNIOSEK/1/2026", source=Source.POZWOLENIA, status=Status.DECYZJA)
    base.update(overrides)
    return GunbCase(**base)


# --- text -------------------------------------------------------------------

def test_normalize_text_folds_polish_diacritics_case_and_whitespace():
    assert normalize_text("  Budowa   BUDYNKU  Mieszkalnego\nŁąka źdźbło ") == (
        "budowa budynku mieszkalnego laka zdzblo"
    )


def test_clean_collapses_whitespace_and_maps_blank_to_none():
    assert clean("  ul.   Długa \n") == "ul. Długa"
    assert clean('""') is None
    assert clean("   ") is None
    assert clean(None) is None


# --- teryt ------------------------------------------------------------------

def test_voivodeship_lookup_by_code_name_and_name_without_diacritics():
    assert get_voivodeship("12").slug == "malopolskie"
    assert get_voivodeship("małopolskie").code == "12"
    assert get_voivodeship("MALOPOLSKIE").code == "12"
    assert get_voivodeship("2").code == "02"


def test_voivodeship_lookup_rejects_unknown_code():
    with pytest.raises(KeyError):
        get_voivodeship("99")


# --- Parcel -----------------------------------------------------------------

def test_parcel_uldk_id_pads_obreb_and_omits_map_sheet():
    parcel = Parcel.from_raw("160602_4", "38", " 178/11 ", "")
    assert parcel is not None
    assert parcel.uldk_id == "160602_4.0038.178/11"
    assert parcel.obreb_id == "160602_4.0038"
    assert parcel.arkusz is None


def test_parcel_full_id_contains_map_sheet_when_present():
    parcel = Parcel.from_raw("161106_5", "0058", "52/11", "1")
    assert parcel is not None
    assert parcel.full_id == "161106_5.0058.AR_1.52/11"
    assert parcel.uldk_id == "161106_5.0058.52/11"


@pytest.mark.parametrize(
    "jednostka,obreb,numer",
    [("", "0001", "12"), ("160602_4", "", "12"), ("160602_4", "0001", ""), ("16060_4", "0001", "12")],
)
def test_parcel_from_raw_rejects_incomplete_or_malformed_data(jednostka, obreb, numer):
    assert Parcel.from_raw(jednostka, obreb, numer, None) is None


# --- Status -----------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Brak sprzeciwu", Status.BRAK_SPRZECIWU),
        ("  decyzja POZYTYWNA ", Status.DECYZJA),
        ("Decyzja umarzająca", Status.UMORZENIE),
        ("Decyzja odmowna", Status.ODMOWA),
        ("Decyzja o sprzeciwie", Status.SPRZECIW),
        ("Sprawa w toku", Status.ZGLOSZENIE),
        ("W trakcie rozpatrywania", Status.WNIOSEK),
        ("Wycofany przez inwestora", Status.WYCOFANY),
        ("Bez rozpatrzenia", Status.BEZ_ROZPATRZENIA),
        ("całkiem nowy stan", Status.INNY),
    ],
)
def test_status_from_gunb_text_maps_registry_states(text, expected):
    assert Status.from_gunb_text(text) is expected


def test_status_from_gunb_text_returns_none_for_blank():
    assert Status.from_gunb_text("  ") is None


def test_negative_statuses_are_flagged():
    assert Status.ODMOWA.is_negative
    assert Status.SPRZECIW.is_negative
    assert not Status.DECYZJA.is_negative
    assert not Status.WNIOSEK.is_negative


# --- GunbCase ---------------------------------------------------------------

def test_case_derives_teryt_codes_from_terc():
    case = make_case(terc="1606024")
    assert case.wojewodztwo_teryt == "16"
    assert case.powiat_teryt == "1606"
    assert case.gmina_teryt == "1606024"


def test_case_falls_back_to_parcel_unit_when_terc_missing():
    case = make_case(terc=None, parcels=[Parcel.from_raw("160602_4", "0038", "1", None)])
    assert case.powiat_teryt == "1606"
    assert case.gmina_teryt == "1606024"


def test_case_address_joins_street_number_postcode_and_city():
    case = make_case(ulica="ul. ks. Bernarda Sychty", nr_domu="58", kod_pocztowy="84-140", miasto="Jastarnia")
    assert case.adres_opisowy == "ul. ks. Bernarda Sychty 58, 84-140 Jastarnia"


def test_case_address_without_street_puts_number_after_city():
    case = make_case(miasto="Połczyno", nr_domu="20")
    assert case.adres_opisowy == "Połczyno 20"


def test_case_event_date_prefers_decision_date():
    case = make_case(data_wplywu=date(2026, 8, 1), data_decyzji=date(2026, 9, 2))
    assert case.event_date == date(2026, 9, 2)
    assert make_case(data_wplywu=date(2026, 8, 1)).event_date == date(2026, 8, 1)
