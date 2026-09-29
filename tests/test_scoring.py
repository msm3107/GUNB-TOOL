import pytest

from gunb_tool.models import Investment
from gunb_tool.scoring import score_investment


def lead(**overrides) -> Investment:
    base = dict(
        id_sprawy="X/1",
        zrodlo="pozwolenia",
        status="decyzja",
        kategoria="mieszkaniowa-jednorodzinna",
        kategoria_obiektu="I",
        rodzaj_robot="budowa nowego/nowych obiektów budowlanych",
        nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego",
        kubatura=878.0,  # mediana domu w powiecie poznańskim
    )
    base.update(overrides)
    return Investment(**base)


def test_big_multi_building_residential_project_is_hot():
    # Prawdziwy lead z Warszawy (Białołęka): 26 265 m³, zespół dwóch budynków wielorodzinnych, inwestor-spółka.
    score = score_investment(lead(
        kategoria="mieszkaniowa-wielorodzinna", kategoria_obiektu="XIII", kubatura=26265.0,
        nazwa_zamierzenia="Budowa zespołu dwóch budynków wielorodzinnych z garażem podziemnym",
        inwestor="Napollo 3 Sp. z o.o.",
    ))
    assert score.priority == "hot"
    assert "kilka budynków" in score.reasons
    assert score.points >= 6


def test_typical_new_house_is_normal():
    assert score_investment(lead()).priority == "normal"


def test_small_garage_is_low():
    score = score_investment(lead(
        kategoria="inna", kategoria_obiektu="III", kubatura=60.0, nazwa_zamierzenia="Budowa garażu blaszanego",
    ))
    assert score.priority == "low"
    assert "mały obiekt" in score.reasons


@pytest.mark.parametrize(
    "description",
    [
        "Budowa dziewięciu budynków mieszkalnych jednorodzinnych w zabudowie szeregowej",
        "Budowa 5 budynków mieszkalnych jednorodzinnych",
        "Budowa osiedla domów jednorodzinnych",
        "Budowa budynków mieszkalnych jednorodzinnych w zabudowie bliźniaczej",
    ],
)
def test_several_buildings_are_recognised(description):
    assert "kilka budynków" in score_investment(lead(nazwa_zamierzenia=description)).reasons


def test_single_building_is_not_counted_as_several():
    assert "kilka budynków" not in score_investment(lead(nazwa_zamierzenia="Budowa budynku usługowego")).reasons


def test_renovation_works_score_lower_than_new_construction():
    new = score_investment(lead())
    works = score_investment(lead(rodzaj_robot="wykonanie robót budowlanych innych niż wymienione powyżej"))
    assert works.points < new.points


def test_unknown_volume_does_not_penalise():
    assert score_investment(lead(kubatura=None)).priority == "normal"


def test_reasons_are_short_polish_labels():
    score = score_investment(lead(kubatura=21000.0, kategoria="komercyjna", inwestor="Firma Sp. z o.o."))
    assert score.reasons[0] == "kubatura ≥ 20 000 m³"
    assert "inwestor firmowy" in score.reasons


@pytest.mark.parametrize(
    "description",
    [
        "dot.[e-Dor] Wniosek o zmianę pozwolenia na budowę nr 569/26",       # prawdziwe opisy z pow. poznańskiego
        "dot. zmiany decyzji 2953/25 z dnia 25.09.2025 pozwolenia na budowę budynku",
    ],
)
def test_amendment_of_earlier_permit_is_penalised(description):
    score = score_investment(lead(nazwa_zamierzenia=description))
    assert "zmiana wcześniejszego pozwolenia" in score.reasons
    assert score.priority == "low"
