import shutil
from datetime import date
from pathlib import Path

import pytest

from gunb_tool.gunb_scraper import FetchQuery, GunbFormatError, GunbScraper, parse_archive
from gunb_tool.http_client import DownloadResult
from gunb_tool.models import Source, Status
from tests.gunb_fixtures import (
    POZWOLENIA_HEADER,
    ZGLOSZENIA_HEADER,
    pozwolenie,
    write_zip,
    zgloszenie,
)

OPOLSKIE = ("16",)


def query(**overrides) -> FetchQuery:
    params = dict(voivodeships=OPOLSKIE, sources=(Source.POZWOLENIA,))
    params.update(overrides)
    return FetchQuery(**params)


# --- parse_archive: format pliku ---------------------------------------------

def test_merges_rows_of_one_case_even_when_not_adjacent(tmp_path):
    archive = write_zip(tmp_path / "wynik_opolskie.zip", POZWOLENIA_HEADER, [
        pozwolenie(numer_gunb="A/1", numer_dzialki="13"),
        pozwolenie(numer_gunb="B/2", numer_dzialki="7"),
        pozwolenie(numer_gunb="A/1", numer_dzialki="14/2", numer_arkusza_dzialki="3"),
        pozwolenie(numer_gunb="A/1", numer_dzialki="13"),  # duplikat działki
    ])

    cases = {c.id_sprawy: c for c in parse_archive(archive, Source.POZWOLENIA, query())}

    assert set(cases) == {"A/1", "B/2"}
    assert [p.full_id for p in cases["A/1"].parcels] == ["160705_4.0005.13", "160705_4.0005.AR_3.14/2"]


def test_parses_case_fields_from_pozwolenia(tmp_path):
    archive = write_zip(tmp_path / "wynik_opolskie.zip", POZWOLENIA_HEADER, [
        pozwolenie(nazwa_inwestor="Firma Testowa Sp. z o.o. ", ulica="Jarosława", ulica_dalej="bp. "),
    ])

    (case,) = parse_archive(archive, Source.POZWOLENIA, query())

    assert case.source is Source.POZWOLENIA
    assert case.status is Status.DECYZJA
    assert case.data_wplywu == date(2026, 8, 1)
    assert case.data_decyzji == date(2026, 9, 10)
    assert case.numer_decyzji == "10/2026"
    assert case.inwestor_raw == "Firma Testowa Sp. z o.o."
    assert case.kubatura == pytest.approx(650.5)
    assert case.kategoria_obiektu == "I"
    assert case.rodzaj_robot == "budowa nowego/nowych obiektów budowlanych"
    assert case.nazwa_zamierzenia == "Budowa budynku mieszkalnego jednorodzinnego"
    assert case.projektant_nazwisko == "Testowy"
    assert case.adres_opisowy == "ul. bp. Jarosława 5, Nysa"
    assert case.powiat_teryt == "1607"


def test_pozwolenie_without_decision_is_pending_application(tmp_path):
    archive = write_zip(tmp_path / "w.zip", POZWOLENIA_HEADER, [
        pozwolenie(numer_decyzji_urzedu='""', data_wydania_decyzji='""'),
    ])
    (case,) = parse_archive(archive, Source.POZWOLENIA, query(date_field="wplyw"))
    assert case.status is Status.WNIOSEK


def test_zgloszenia_status_comes_from_stan_column(tmp_path):
    archive = write_zip(tmp_path / "z.zip", ZGLOSZENIA_HEADER, [zgloszenie()])

    (case,) = parse_archive(archive, Source.ZGLOSZENIA, query(sources=(Source.ZGLOSZENIA,)))

    assert case.status is Status.BRAK_SPRZECIWU
    assert case.status_raw == "Brak sprzeciwu"
    assert case.adres_opisowy == "ul. ks. Bernarda Sychty 58, 48-300 Nysa"
    assert case.data_decyzji is None


def test_detects_hash_delimiter_and_file_without_bom(tmp_path):
    archive = write_zip(tmp_path / "w.zip", POZWOLENIA_HEADER, [pozwolenie()], delimiter="#", bom=False)
    (case,) = parse_archive(archive, Source.POZWOLENIA, query())
    assert case.miasto == "Nysa"


def test_quoted_newlines_inside_fields_are_preserved_as_single_record(tmp_path):
    archive = write_zip(tmp_path / "w.zip", POZWOLENIA_HEADER, [
        pozwolenie(nazwa_zam_budowlanego="Budowa budynku;\nmieszkalnego"),
        pozwolenie(numer_gunb="B/2"),
    ])
    cases = parse_archive(archive, Source.POZWOLENIA, query())
    assert len(cases) == 2
    assert any(c.nazwa_zamierzenia == "Budowa budynku; mieszkalnego" for c in cases)


def test_rows_with_wrong_column_count_are_skipped(tmp_path):
    archive = write_zip(tmp_path / "w.zip", POZWOLENIA_HEADER, [
        ["za", "mało", "kolumn"],
        pozwolenie(),
    ])
    assert len(parse_archive(archive, Source.POZWOLENIA, query())) == 1


def test_missing_case_id_column_raises_format_error(tmp_path):
    header = [c for c in POZWOLENIA_HEADER if c != "numer_gunb"]
    archive = write_zip(tmp_path / "w.zip", header, [pozwolenie()])
    with pytest.raises(GunbFormatError, match="numer_gunb"):
        parse_archive(archive, Source.POZWOLENIA, query())


def test_multiple_parcel_numbers_in_one_field_are_split(tmp_path):
    archive = write_zip(tmp_path / "w.zip", POZWOLENIA_HEADER, [pozwolenie(numer_dzialki="12/1, 12/2;13")])
    (case,) = parse_archive(archive, Source.POZWOLENIA, query())
    assert [p.numer for p in case.parcels] == ["12/1", "12/2", "13"]


# --- parse_archive: filtry ---------------------------------------------------

def test_filters_by_powiat_using_terc_or_parcel_unit(tmp_path):
    archive = write_zip(tmp_path / "w.zip", POZWOLENIA_HEADER, [
        pozwolenie(numer_gunb="NYSA/1", terc="1607054"),
        pozwolenie(numer_gunb="OPOLE/1", terc="1661011", jednosta_numer_ew="166101_1"),
        pozwolenie(numer_gunb="BEZ-TERC/1", terc="", jednosta_numer_ew="160705_4"),
    ])
    cases = parse_archive(archive, Source.POZWOLENIA, query(powiats=frozenset({"1607"})))
    assert sorted(c.id_sprawy for c in cases) == ["BEZ-TERC/1", "NYSA/1"]


def test_filters_by_decision_date_range(tmp_path):
    archive = write_zip(tmp_path / "w.zip", POZWOLENIA_HEADER, [
        pozwolenie(numer_gunb="STARA/1", data_wydania_decyzji="2026-08-31 00:00:00"),
        pozwolenie(numer_gunb="NOWA/1", data_wydania_decyzji="2026-09-01 00:00:00"),
        pozwolenie(numer_gunb="PRZYSZLA/1", data_wydania_decyzji="2026-10-01 00:00:00"),
    ])
    cases = parse_archive(
        archive, Source.POZWOLENIA, query(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30))
    )
    assert [c.id_sprawy for c in cases] == ["NOWA/1"]


def test_date_field_wplyw_filters_by_application_date(tmp_path):
    archive = write_zip(tmp_path / "w.zip", POZWOLENIA_HEADER, [
        pozwolenie(numer_gunb="A/1", data_wplywu_wniosku="2026-09-05 00:00:00"),
        pozwolenie(numer_gunb="B/1", data_wplywu_wniosku="2026-07-05 00:00:00"),
    ])
    cases = parse_archive(archive, Source.POZWOLENIA, query(date_field="wplyw", date_from=date(2026, 9, 1)))
    assert [c.id_sprawy for c in cases] == ["A/1"]


def test_national_zgloszenia_are_filtered_by_voivodeship(tmp_path):
    archive = write_zip(tmp_path / "z.zip", ZGLOSZENIA_HEADER, [
        zgloszenie(numer_ewidencyjny_system="OP/1"),
        zgloszenie(numer_ewidencyjny_system="PM/1", wojewodztwo_objekt="pomorskie", terc="2211021",
                   jednostki_numer="221102_4"),
        zgloszenie(numer_ewidencyjny_system="OP/BEZ-TERC", terc="", jednostki_numer=""),
    ])
    cases = parse_archive(archive, Source.ZGLOSZENIA, query(sources=(Source.ZGLOSZENIA,)))
    assert sorted(c.id_sprawy for c in cases) == ["OP/1", "OP/BEZ-TERC"]


# --- GunbScraper.fetch_pages: pobieranie + paginacja -------------------------

class FakeDownloader:
    """Atrapa klienta HTTP: „pobiera” pliki z przygotowanego katalogu."""

    def __init__(self, files: dict[str, Path]) -> None:
        self.files = files
        self.urls: list[str] = []

    def download(self, url, dest, *, validator=None):
        self.urls.append(url)
        shutil.copy(self.files[url.rsplit("/", 1)[1]], dest)
        assert validator is None or validator(Path(dest))
        return DownloadResult(Path(dest), False, None, None, Path(dest).stat().st_size)


def test_fetch_pages_paginates_cases_newest_first(tmp_path):
    rows = [
        pozwolenie(numer_gunb=f"S/{day}", data_wydania_decyzji=f"2026-09-{day:02d} 00:00:00")
        for day in range(1, 6)
    ]
    fixture = write_zip(tmp_path / "wynik_opolskie.zip", POZWOLENIA_HEADER, rows)
    http = FakeDownloader({"wynik_opolskie.zip": fixture})
    scraper = GunbScraper(http, cache_dir=tmp_path / "cache", base_url="https://gunb.test/pliki/")

    pages = list(scraper.fetch_pages(query(), page_size=2))

    assert http.urls == ["https://gunb.test/pliki/wynik_opolskie.zip"]
    assert [len(p.items) for p in pages] == [2, 2, 1]
    assert [p.number for p in pages] == [1, 2, 3]
    assert all(p.total_pages == 3 and p.total_items == 5 for p in pages)
    assert [c.id_sprawy for p in pages for c in p.items] == ["S/5", "S/4", "S/3", "S/2", "S/1"]


def test_fetch_pages_downloads_national_zgloszenia_once_for_many_voivodeships(tmp_path):
    fixture = write_zip(tmp_path / "wynik_zgloszenia_2022_up.zip", ZGLOSZENIA_HEADER, [
        zgloszenie(numer_ewidencyjny_system="OP/1"),
        zgloszenie(numer_ewidencyjny_system="PM/1", terc="2211021", jednostki_numer="221102_4"),
    ])
    http = FakeDownloader({"wynik_zgloszenia_2022_up.zip": fixture})
    scraper = GunbScraper(http, cache_dir=tmp_path / "cache", base_url="https://gunb.test/pliki/")

    pages = list(scraper.fetch_pages(query(voivodeships=("16", "22"), sources=(Source.ZGLOSZENIA,))))

    assert http.urls == ["https://gunb.test/pliki/wynik_zgloszenia_2022_up.zip"]
    assert sorted(c.id_sprawy for p in pages for c in p.items) == ["OP/1", "PM/1"]
