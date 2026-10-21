"""Spójność nazw obszarów i kodów: działka znaleziona w innej gminie niż podaje rejestr
to lokalizacja niepewna – jawna informacja o jakości danych."""

from core.models import Investment, location_mismatch
from core.ranking import UserFilters

BASE = (53.7784, 20.4801)
DYWITY_OK = dict(
    gmina_teryt="2814042",
    teryt_dzialki="281404_2.0005.12/3",
    lat=53.8285,
    lon=20.4867,
    precyzja_geo="dzialka",
    gmina="Dywity",
    powiat="powiat olsztyński",
    powiat_teryt="2814",
)
DYWITY_ELSEWHERE = dict(
    gmina_teryt="2814042",
    teryt_dzialki="281705_2.0012.9/3",
    lat=53.507,
    lon=21.377,
    precyzja_geo="dzialka",
    gmina="Rozogi",
    powiat="powiat szczycieński",
    powiat_teryt="2814",
    miejscowosc="Lipniak",
    adres_opisowy="Lipniak",
)


def lead(id_sprawy: str, **kwargs) -> Investment:
    data = dict(
        id_sprawy=id_sprawy,
        zrodlo="pozwolenia",
        status="decyzja",
        nazwa_zamierzenia="Budowa",
        kategoria="mieszkaniowa-jednorodzinna",
    )
    data.update(kwargs)
    return Investment(**data)


def test_parcel_found_in_another_gmina_is_an_uncertain_location():
    assert location_mismatch(lead("A", **DYWITY_ELSEWHERE))
    assert not location_mismatch(lead("B", **DYWITY_OK))
    assert not location_mismatch(lead("C", gmina_teryt=None, teryt_dzialki="281705_2.0012.9/3"))
    # Lokalizacja niepewna nie liczy odległości od bazy
    inv = lead("A", **DYWITY_ELSEWHERE)
    if location_mismatch(inv):
        inv.lat, inv.lon = None, None
    assert UserFilters(baza=BASE, promien_km=100).distance_km(inv) is None
