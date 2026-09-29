"""Modele domenowe: źródła danych, statusy spraw RWDZ, działki, sprawy i leady."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field, fields
from datetime import date

from .text import normalize_text


class Source(str, enum.Enum):
    """Rodzaj rejestru GUNB, z którego pochodzi sprawa."""

    POZWOLENIA = "pozwolenia"  # Rejestr Wniosków i Decyzji (pozwolenia na budowę)
    ZGLOSZENIA = "zgloszenia"  # Rejestr Zgłoszeń


class Status(str, enum.Enum):
    """Znormalizowany status sprawy – obejmuje wszystkie stany z wyszukiwarki RWDZ."""

    WNIOSEK = "wniosek"
    DECYZJA = "decyzja"
    ODMOWA = "odmowa"
    UMORZENIE = "umorzenie"
    WYCOFANY = "wycofany"
    BEZ_ROZPATRZENIA = "bez_rozpatrzenia"
    ZGLOSZENIE = "zgloszenie"
    BRAK_SPRZECIWU = "brak_sprzeciwu"
    SPRZECIW = "sprzeciw"
    INNY = "inny"

    @classmethod
    def from_gunb_text(cls, text: str | None) -> Status | None:
        """Mapuje opis stanu z GUNB (np. ``"Brak sprzeciwu"``) na status.

        Zwraca ``None`` dla pustego tekstu i ``Status.INNY`` dla nieznanego opisu.
        """
        key = normalize_text(text)
        if not key:
            return None
        return _STATUS_BY_TEXT.get(key, cls.INNY)

    @property
    def label(self) -> str:
        """Czytelna etykieta statusu do powiadomień i arkusza."""
        return _STATUS_LABELS[self]

    @property
    def is_negative(self) -> bool:
        """Czy status kończy sprawę bez możliwości realizacji inwestycji."""
        return self in _NEGATIVE_STATUSES


_STATUS_BY_TEXT = {
    "w trakcie rozpatrywania": Status.WNIOSEK,
    "decyzja pozytywna": Status.DECYZJA,
    "decyzja odmowna": Status.ODMOWA,
    "decyzja umarzajaca": Status.UMORZENIE,
    "wycofany przez inwestora": Status.WYCOFANY,
    "bez rozpatrzenia": Status.BEZ_ROZPATRZENIA,
    "sprawa w toku": Status.ZGLOSZENIE,
    "brak sprzeciwu": Status.BRAK_SPRZECIWU,
    "decyzja o sprzeciwie": Status.SPRZECIW,
}

_STATUS_LABELS = {
    Status.WNIOSEK: "wniosek (w trakcie rozpatrywania)",
    Status.DECYZJA: "decyzja – pozwolenie na budowę",
    Status.ODMOWA: "decyzja odmowna",
    Status.UMORZENIE: "decyzja umarzająca",
    Status.WYCOFANY: "wniosek wycofany przez inwestora",
    Status.BEZ_ROZPATRZENIA: "pozostawiony bez rozpatrzenia",
    Status.ZGLOSZENIE: "zgłoszenie (sprawa w toku)",
    Status.BRAK_SPRZECIWU: "zgłoszenie – brak sprzeciwu",
    Status.SPRZECIW: "decyzja o sprzeciwie",
    Status.INNY: "inny",
}

_NEGATIVE_STATUSES = frozenset(
    {Status.ODMOWA, Status.UMORZENIE, Status.WYCOFANY, Status.BEZ_ROZPATRZENIA, Status.SPRZECIW}
)

BUILDING_CATEGORIES: dict[str, str] = {
    "I": "budynki mieszkalne jednorodzinne",
    "II": "budynki służące gospodarce rolnej",
    "III": "inne niewielkie budynki (domy letniskowe, budynki gospodarcze, garaże do 2 stanowisk)",
    "IV": "elementy dróg publicznych i kolejowych (skrzyżowania, wjazdy, zjazdy, przejazdy)",
    "V": "obiekty sportu i rekreacji (stadiony, amfiteatry, odkryte baseny)",
    "VI": "cmentarze",
    "VII": "obiekty służące nawigacji wodnej",
    "VIII": "inne budowle",
    "IX": "budynki kultury, nauki i oświaty",
    "X": "budynki kultu religijnego",
    "XI": "budynki służby zdrowia, opieki społecznej i socjalnej",
    "XII": "budynki administracji publicznej",
    "XIII": "pozostałe budynki mieszkalne",
    "XIV": "budynki zakwaterowania turystycznego i rekreacyjnego (hotele, pensjonaty)",
    "XV": "budynki sportu i rekreacji (hale sportowe, kryte baseny)",
    "XVI": "budynki biurowe i konferencyjne",
    "XVII": "budynki handlu, gastronomii i usług",
    "XVIII": "budynki przemysłowe i magazynowe",
    "XIX": "zbiorniki przemysłowe (silosy, elewatory)",
    "XX": "stacje paliw",
    "XXI": "obiekty transportu wodnego (porty, przystanie, nabrzeża)",
    "XXII": "place składowe, postojowe, składowiska odpadów, parkingi",
    "XXIII": "obiekty lotniskowe",
    "XXIV": "obiekty gospodarki wodnej (zbiorniki, stawy rybne)",
    "XXV": "drogi i kolejowe drogi szynowe",
    "XXVI": "sieci (elektroenergetyczne, telekomunikacyjne, gazowe, wodociągowe, kanalizacyjne)",
    "XXVII": "budowle hydrotechniczne",
    "XXVIII": "obiekty mostowe (mosty, wiadukty, przepusty, tunele)",
    "XXIX": "wolno stojące kominy, maszty, elektrownie wiatrowe",
    "XXX": "obiekty korzystania z zasobów wodnych (ujęcia, oczyszczalnie)",
}
"""Kategorie obiektów budowlanych wg załącznika do Prawa budowlanego (opisy jak w wyszukiwarce RWDZ)."""

LEAD_CATEGORIES: tuple[str, ...] = (
    "mieszkaniowa-jednorodzinna",
    "mieszkaniowa-wielorodzinna",
    "mieszana",
    "komercyjna",
    "publiczna",
    "rolnicza",
    "inna",
)
"""Kategorie biznesowe leadów nadawane przez ``data_filter`` (plus ``"szum"`` dla odrzuconych)."""

NOISE_CATEGORY = "szum"

_JEDNOSTKA_RE = re.compile(r"^\d{6}_\d$")


@dataclass(frozen=True)
class Parcel:
    """Działka ewidencyjna zapisana w sprawie RWDZ.

    Attributes:
        jednostka: identyfikator jednostki ewidencyjnej, np. ``"160602_4"``.
        obreb: czterocyfrowy numer obrębu, np. ``"0038"``.
        numer: numer działki, np. ``"178/11"``.
        arkusz: numer arkusza mapy (opcjonalny).
    """

    jednostka: str
    obreb: str
    numer: str
    arkusz: str | None = None

    @classmethod
    def from_raw(
        cls, jednostka: str | None, obreb: str | None, numer: str | None, arkusz: str | None
    ) -> Parcel | None:
        """Buduje działkę z surowych pól CSV; zwraca ``None`` dla niekompletnych danych."""
        jednostka = (jednostka or "").strip()
        obreb = (obreb or "").strip()
        numer = (numer or "").strip()
        if not (_JEDNOSTKA_RE.match(jednostka) and obreb and numer):
            return None
        if obreb.isdigit():
            obreb = obreb.zfill(4)
        return cls(jednostka, obreb, numer, (arkusz or "").strip() or None)

    @classmethod
    def from_id(cls, parcel_id: str) -> Parcel:
        """Parsuje identyfikator ``WWPPGG_R.OOOO[.AR_n].NR`` (np. ``146510_8.0309.24/35``).

        Raises:
            ValueError: identyfikator ma niepoprawną strukturę.
        """
        parts = parcel_id.strip().split(".")
        arkusz = None
        if len(parts) == 4 and parts[2].upper().startswith("AR_"):
            arkusz = parts[2][3:]
            parts = [parts[0], parts[1], parts[3]]
        parcel = cls.from_raw(*parts, arkusz) if len(parts) == 3 else None
        if parcel is None:
            raise ValueError(f"Niepoprawny identyfikator działki: {parcel_id!r} (oczekiwano WWPPGG_R.OOOO.NR)")
        return parcel

    @property
    def obreb_id(self) -> str:
        """Identyfikator obrębu ewidencyjnego (``WWPPGG_R.OOOO``)."""
        return f"{self.jednostka}.{self.obreb}"

    @property
    def uldk_id(self) -> str:
        """Identyfikator do ULDK bez arkusza – ``GetParcelByIdOrNr`` dopasowuje arkusz sam."""
        return f"{self.obreb_id}.{self.numer}"

    @property
    def full_id(self) -> str:
        """Pełny identyfikator działki (z arkuszem ``AR_n``, jeśli podany)."""
        if self.arkusz:
            return f"{self.obreb_id}.AR_{self.arkusz}.{self.numer}"
        return self.uldk_id


@dataclass
class GunbCase:
    """Sprawa z rejestru GUNB – wiersze CSV scalone po numerze sprawy (jedna działka = jeden wiersz)."""

    id_sprawy: str
    source: Source
    status: Status
    status_raw: str | None = None
    numer_urzedu: str | None = None
    organ: str | None = None
    data_wplywu: date | None = None
    data_decyzji: date | None = None
    numer_decyzji: str | None = None
    inwestor_raw: str | None = None
    wojewodztwo: str | None = None
    miasto: str | None = None
    kod_pocztowy: str | None = None
    terc: str | None = None
    ulica: str | None = None
    nr_domu: str | None = None
    rodzaj_inwestycji: str | None = None
    kategoria_obiektu: str | None = None
    rodzaj_robot: str | None = None
    nazwa_zamierzenia: str | None = None
    kubatura: float | None = None
    projektant_imie: str | None = None
    projektant_nazwisko: str | None = None
    projektant_uprawnienia: str | None = None
    parcels: list[Parcel] = field(default_factory=list)

    @property
    def gmina_teryt(self) -> str | None:
        """Siedmiocyfrowy kod TERYT gminy (z ``terc`` lub z jednostki ewidencyjnej)."""
        if self.terc and len(self.terc) == 7 and self.terc.isdigit():
            return self.terc
        for parcel in self.parcels:
            return parcel.jednostka[:6] + parcel.jednostka[7]
        return None

    @property
    def powiat_teryt(self) -> str | None:
        """Czterocyfrowy kod TERYT powiatu."""
        gmina = self.gmina_teryt
        return gmina[:4] if gmina else None

    @property
    def wojewodztwo_teryt(self) -> str | None:
        """Dwucyfrowy kod TERYT województwa."""
        gmina = self.gmina_teryt
        return gmina[:2] if gmina else None

    @property
    def adres_opisowy(self) -> str | None:
        """Adres w formie ``"ul. Długa 5, 00-001 Miasto"`` (pomija brakujące części)."""
        locality = " ".join(p for p in (self.kod_pocztowy, self.miasto) if p)
        if self.ulica:
            street = " ".join(p for p in (self.ulica, self.nr_domu) if p)
            return ", ".join(p for p in (street, locality) if p) or None
        return " ".join(p for p in (locality, self.nr_domu) if p) or None

    @property
    def event_date(self) -> date | None:
        """Data ostatniego zdarzenia w sprawie: decyzja, a gdy jej brak – wpływ wniosku."""
        return self.data_decyzji or self.data_wplywu


@dataclass
class Investment:
    """Lead inwestycyjny w postaci zapisywanej w tabeli ``investments``."""

    id_sprawy: str
    zrodlo: str
    status: str
    status_opis: str | None = None
    data_aktualizacji: str | None = None
    data_wplywu: str | None = None
    data_decyzji: str | None = None
    numer_urzedu: str | None = None
    numer_decyzji: str | None = None
    organ: str | None = None
    kategoria: str | None = None
    kategoria_obiektu: str | None = None
    rodzaj_robot: str | None = None
    nazwa_zamierzenia: str | None = None
    adres_opisowy: str | None = None
    miejscowosc: str | None = None
    wojewodztwo: str | None = None
    powiat: str | None = None
    gmina: str | None = None
    powiat_teryt: str | None = None
    gmina_teryt: str | None = None
    teryt_dzialki: str | None = None
    dzialki: list[str] = field(default_factory=list)
    lat: float | None = None
    lon: float | None = None
    precyzja_geo: str | None = None
    google_maps_url: str | None = None
    geoportal_url: str | None = None
    inwestor: str | None = None
    projektant: str | None = None
    projektant_uprawnienia: str | None = None
    pracownia: str | None = None
    kubatura: float | None = None
    is_residential: bool = False
    is_commercial: bool = False
    is_noise: bool = False
    # --- Pola księgowe: ustawia wyłącznie warstwa storage. ---
    czy_wyslano: bool = False
    wyslano_kanaly: str = ""
    utworzono: str | None = None
    zmieniono: str | None = None
    status_zmieniony: str | None = None
    zsynchronizowano: str | None = None
    ostatnio_widziany: str | None = None


BOOKKEEPING_FIELDS: tuple[str, ...] = (
    "czy_wyslano",
    "wyslano_kanaly",
    "utworzono",
    "zmieniono",
    "status_zmieniony",
    "zsynchronizowano",
    "ostatnio_widziany",
)
"""Pola zarządzane przez storage – nie wchodzą do porównania treści leada."""

CONTENT_FIELDS: tuple[str, ...] = tuple(
    f.name for f in fields(Investment) if f.name not in BOOKKEEPING_FIELDS
)
"""Pola opisujące treść leada (porównywane przy wykrywaniu aktualizacji)."""
