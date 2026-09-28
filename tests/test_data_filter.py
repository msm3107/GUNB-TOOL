from dataclasses import replace

import pytest

from gunb_tool.config import FilterConfig
from gunb_tool.data_filter import LeadFilter
from gunb_tool.models import GunbCase, Source, Status


def case(description: str, category: str | None = None, **overrides) -> GunbCase:
    base = dict(
        id_sprawy="X/1",
        source=Source.POZWOLENIA,
        status=Status.DECYZJA,
        nazwa_zamierzenia=description,
        kategoria_obiektu=category,
        rodzaj_robot="budowa nowego/nowych obiektów budowlanych",
        rodzaj_inwestycji="Obiekt budowlany inny niż budynek mieszkalny jednorodzinny",
    )
    base.update(overrides)
    return GunbCase(**base)


@pytest.fixture
def lead_filter() -> LeadFilter:
    return LeadFilter(FilterConfig())


# --- Kategoryzacja -----------------------------------------------------------

def test_single_family_house_is_residential(lead_filter):
    result = lead_filter.classify(case("Budowa budynku mieszkalnego jednorodzinnego", "I"))
    assert (result.is_residential, result.is_commercial, result.is_noise) == (True, False, False)
    assert result.kategoria == "mieszkaniowa-jednorodzinna"


def test_multi_family_building_gets_own_category(lead_filter):
    result = lead_filter.classify(case("Budowa budynku mieszkalnego wielorodzinnego z garażem podziemnym", "XIII"))
    assert result.kategoria == "mieszkaniowa-wielorodzinna"


def test_warehouse_hall_is_commercial(lead_filter):
    result = lead_filter.classify(case("Budowa hali magazynowej z częścią biurową", "XVIII"))
    assert (result.is_residential, result.is_commercial) == (False, True)
    assert result.kategoria == "komercyjna"


def test_residential_service_building_is_mixed(lead_filter):
    result = lead_filter.classify(case("Budowa budynku mieszkalno-usługowego", "XIII"))
    assert result.is_residential and result.is_commercial
    assert result.kategoria == "mieszana"


def test_non_residential_building_is_not_residential(lead_filter):
    result = lead_filter.classify(case("Budowa budynku niemieszkalnego - garażu", "III"))
    assert not result.is_residential


def test_house_of_culture_is_public_not_residential(lead_filter):
    result = lead_filter.classify(case("Rozbudowa domu kultury", "IX"))
    assert not result.is_residential
    assert result.kategoria == "publiczna"


def test_single_family_investment_type_marks_residential_without_keywords(lead_filter):
    result = lead_filter.classify(
        case("Zmiana sposobu użytkowania pomieszczenia na kotłownię", "I",
             rodzaj_inwestycji="Budynek mieszkalny jednorodzinny")
    )
    assert result.is_residential


# --- Szum --------------------------------------------------------------------

@pytest.mark.parametrize(
    "description,category",
    [
        ("Budowa ogrodzenia działki", "VIII"),
        ("Budowa zjazdu indywidualnego z drogi gminnej", "IV"),
        ("Budowa przyłącza gazowego do budynku mieszkalnego jednorodzinnego", "XXVI"),
        ("Instalacja gazowa dla budynku mieszkalnego jednorodzinnego", "VIII"),
        ("Rozbudowa sieci kanalizacji sanitarnej", "XXVI"),
    ],
)
def test_noise_is_detected_when_excluded_word_leads_the_description(lead_filter, description, category):
    result = lead_filter.classify(case(description, category))
    assert result.is_noise
    assert result.kategoria == "szum"
    assert result.noise_reason


def test_building_with_connections_and_fence_is_not_noise(lead_filter):
    result = lead_filter.classify(
        case("Budowa budynku mieszkalnego jednorodzinnego wraz z przyłączami, zjazdem i ogrodzeniem", "I")
    )
    assert not result.is_noise
    assert result.is_residential


def test_noise_category_without_building_words_is_noise(lead_filter):
    result = lead_filter.classify(case("Przebudowa ul. Polnej wraz z odwodnieniem", "XXV"))
    assert result.is_noise
    assert "XXV" in result.noise_reason


def test_demolition_work_type_is_noise(lead_filter):
    result = lead_filter.classify(
        case("budynek gospodarczy", "III", rodzaj_robot="rozbiórka istniejącego obiektu budowlanego")
    )
    assert result.is_noise


def test_pure_demolition_is_noise(lead_filter):
    result = lead_filter.classify(case("Rozbiórka budynku gospodarczego", "III"))
    assert result.is_noise
    assert "rozbiórk" in result.noise_reason


@pytest.mark.parametrize(
    "description",
    [
        "Częściowa rozbiórka i przebudowa budynku mieszkalnego jednorodzinnego",
        "Rozbiórka istniejącego budynku i budowa nowego budynku mieszkalnego jednorodzinnego",
    ],
)
def test_demolition_combined_with_construction_is_a_lead(lead_filter, description):
    result = lead_filter.classify(case(description, "I"))
    assert not result.is_noise
    assert result.is_residential


def test_demolitions_are_kept_when_disabled_in_config():
    result = LeadFilter(FilterConfig(drop_demolitions=False)).classify(case("Rozbiórka budynku gospodarczego", "III"))
    assert not result.is_noise


def test_description_outweighs_single_family_investment_type(lead_filter):
    result = lead_filter.classify(
        case("Rozbudowa budynku usługowego", "III", rodzaj_inwestycji="Budynek mieszkalny jednorodzinny")
    )
    assert not result.is_residential
    assert result.kategoria == "komercyjna"


def test_exclude_keywords_come_from_config():
    description = "Budowa farmy fotowoltaicznej"
    strict = LeadFilter(FilterConfig(exclude_keywords=("fotowolt",), noise_categories=()))
    lenient = LeadFilter(FilterConfig(exclude_keywords=("ogrodzen",), noise_categories=()))
    assert strict.classify(case(description, "VIII")).is_noise
    assert not lenient.classify(case(description, "VIII")).is_noise


# --- Decyzja o zachowaniu leada ----------------------------------------------

def test_noise_is_dropped_by_default(lead_filter):
    decision = lead_filter.evaluate(case("Budowa ogrodzenia", "VIII"))
    assert not decision.keep
    assert "ogrodzen" in decision.reason


def test_noise_is_kept_when_drop_noise_disabled():
    decision = LeadFilter(FilterConfig(drop_noise=False)).evaluate(case("Budowa ogrodzenia", "VIII"))
    assert decision.keep
    assert decision.classification.is_noise


def test_include_categories_limit_kept_leads():
    only_commercial = LeadFilter(FilterConfig(include_categories=("komercyjna",)))
    assert only_commercial.evaluate(case("Budowa hali produkcyjnej", "XVIII")).keep
    decision = only_commercial.evaluate(case("Budowa budynku mieszkalnego jednorodzinnego", "I"))
    assert not decision.keep
    assert "mieszkaniowa-jednorodzinna" in decision.reason


# --- Inwestor ----------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Firma  Testowa Sp. z o.o. ", "Firma Testowa Sp. z o.o."),
        ('""', None),
        (None, None),
        ("osoba fizyczna", None),
        ("brak danych", None),
    ],
)
def test_investor_extraction(lead_filter, raw, expected):
    assert lead_filter.extract_investor(case("x", inwestor_raw=raw)) == expected


# --- Projektant --------------------------------------------------------------

def designer_case(first, last, license_no="MAP/0001/POOA/20"):
    return case("x", projektant_imie=first, projektant_nazwisko=last, projektant_uprawnienia=license_no)


def test_designer_all_caps_name_is_title_cased(lead_filter):
    designer = lead_filter.extract_designer(designer_case("JAN", "KOWALSKA-NOWAK"))
    assert designer.name == "Jan Kowalska-Nowak"
    assert designer.license_no == "MAP/0001/POOA/20"
    assert designer.firm is None
    assert designer.display == "Jan Kowalska-Nowak"


def test_designer_titles_and_prefixes_are_removed(lead_filter):
    assert lead_filter.extract_designer(designer_case("mgr inż. arch. Anna", "Nowak")).name == "Anna Nowak"
    assert lead_filter.extract_designer(designer_case("KATARZYNA", "Projektant: ŻÓŁTOWSKA-TESTOWA")).name == (
        "Katarzyna Żółtowska-Testowa"
    )


@pytest.mark.parametrize(
    "first,last",
    [("Brak projektu rozbiórki", "Brak projektu rozbiórki"), ("projektantów", "Brak"), ("Bez projektu", "1"), (None, None)],
)
def test_designer_placeholders_yield_none(lead_filter, first, last):
    assert lead_filter.extract_designer(designer_case(first, last)) is None


def test_designer_studio_is_recognised_as_firm(lead_filter):
    designer = lead_filter.extract_designer(designer_case(None, "Pracownia Architektoniczna Testowa s.c."))
    assert designer.firm == "Pracownia Architektoniczna Testowa s.c."
    assert designer.name is None
    assert designer.display == "Pracownia Architektoniczna Testowa s.c."


def test_designer_license_placeholder_is_dropped(lead_filter):
    assert lead_filter.extract_designer(designer_case("Jan", "Nowak", license_no="brak")).license_no is None


def test_evaluate_bundles_entities(lead_filter):
    decision = lead_filter.evaluate(
        replace(designer_case("Jan", "Nowak"), nazwa_zamierzenia="Budowa budynku biurowego",
                kategoria_obiektu="XVI", inwestor_raw="Testowa Sp. z o.o.")
    )
    assert decision.keep
    assert decision.investor == "Testowa Sp. z o.o."
    assert decision.designer.name == "Jan Nowak"
    assert decision.classification.kategoria == "komercyjna"
