"""Warstwa pobierania (ingestion): paczki GUNB – uszkodzone ZIP-y, zmiany formatu CSV, brudne pola."""

from __future__ import annotations

import logging
import zipfile

import pytest
import responses

import main
from gunb_tool import gunb_scraper
from gunb_tool.config import FilterConfig
from gunb_tool.contacts import split_contact
from gunb_tool.data_filter import LeadFilter
from gunb_tool.gunb_scraper import FetchQuery, GunbFormatError, parse_archive
from gunb_tool.models import Source
from gunb_tool.storage import LeadRepository
from tests.gunb_fixtures import POZWOLENIA_HEADER, pozwolenie
from tests.qa.conftest import GUNB_OPOLSKIE_ZIP

QUERY = FetchQuery(voivodeships=("16",))
CASE_ID = "ST-OP-NY/WNIOSEK/1/2026"


def run_fetch(workdir) -> int:
    """Pełne uruchomienie ``main.py --fetch`` (bez geokodowania) – jak z harmonogramu."""
    return main.main(["--config", str(workdir / "config.yaml"), "--fetch", "--no-geocode", "--since", "2020-01-01"])


def stored_leads(workdir) -> int:
    with LeadRepository(workdir / "data" / "qa.sqlite") as repo:
        return repo.stats()["razem"]


def damage_member(zip_bytes: bytes) -> bytes:
    """Psuje dane pliku CSV w środku archiwum, zostawiając poprawny katalog ZIP (``is_zipfile`` przechodzi)."""
    data = bytearray(zip_bytes)
    name_length = int.from_bytes(data[26:28], "little")
    extra_length = int.from_bytes(data[28:30], "little")
    start = 30 + name_length + extra_length  # lokalny nagłówek pierwszego pliku ma 30 bajtów + nazwa + extra
    for offset in range(start, start + 24):
        data[offset] ^= 0xFF
    return bytes(data)


def archive_with(tmp_path, gunb_zip, rows, **kwargs):
    path = tmp_path / "wynik_opolskie.zip"
    path.write_bytes(gunb_zip(rows, **kwargs))
    return path


# --- Uszkodzone paczki ------------------------------------------------------------------------------

def test_html_page_instead_of_zip_is_rejected_and_run_ends_gracefully(qa_workdir, http, caplog, mocker):
    http.add(responses.GET, GUNB_OPOLSKIE_ZIP, body=b"<html><body>Przerwa techniczna</body></html>",
             content_type="text/html")
    parse = mocker.spy(gunb_scraper, "parse_archive")

    with caplog.at_level(logging.ERROR):
        exit_code = run_fetch(qa_workdir)

    assert exit_code == 1  # błąd zgłoszony kodem wyjścia – bez wyjątku i bez przerwania innych zadań
    assert len(http.calls) == 4  # 1 próba + 3 ponowienia, zanim uznamy paczkę za złą
    assert parse.call_count == 0  # do parsowania nie dochodzi
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("nie przeszedł walidacji" in message for message in errors)
    cache = qa_workdir / "data" / "cache"
    assert not (cache / "wynik_opolskie.zip").exists()
    assert list(cache.glob("*.part")) == []  # żadnych porzuconych plików tymczasowych
    assert stored_leads(qa_workdir) == 0


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED], ids=["zly-crc", "zly-deflate"])
def test_zip_with_damaged_contents_is_reported_and_dropped_from_cache(qa_workdir, http, gunb_zip, caplog,
                                                                      compression):
    good = gunb_zip(compression=compression)
    http.add(responses.GET, GUNB_OPOLSKIE_ZIP, body=damage_member(good), headers={"ETag": '"v1"'})
    http.add(responses.GET, GUNB_OPOLSKIE_ZIP, body=good, headers={"ETag": '"v2"'})

    with caplog.at_level(logging.ERROR):
        assert run_fetch(qa_workdir) == 1
    assert "uszkodzone archiwum" in caplog.text
    cache = qa_workdir / "data" / "cache"
    assert not (cache / "wynik_opolskie.zip").exists()  # uszkodzona paczka nie zostaje w cache…
    assert not (cache / "wynik_opolskie.zip.meta.json").exists()

    assert run_fetch(qa_workdir) == 0  # …więc kolejne uruchomienie pobiera ją od nowa i działa
    assert "If-None-Match" not in http.calls[-1].request.headers
    assert stored_leads(qa_workdir) == 1


def test_damaged_archive_raises_format_error_not_a_crash(tmp_path, gunb_zip):
    path = tmp_path / "wynik_opolskie.zip"
    path.write_bytes(damage_member(gunb_zip(compression=zipfile.ZIP_DEFLATED)))
    with pytest.raises(GunbFormatError, match="uszkodzone archiwum"):
        parse_archive(path, Source.POZWOLENIA, QUERY)


# --- Zmiany formatu CSV po stronie urzędu ------------------------------------------------------------

def test_renamed_volume_column_kubatura_m3_is_still_understood(tmp_path, gunb_zip):
    header = ["kubatura_m3" if column == "kubatura" else column for column in POZWOLENIA_HEADER]
    row = pozwolenie()
    row["kubatura_m3"] = row.pop("kubatura")

    [case] = parse_archive(archive_with(tmp_path, gunb_zip, [row], header=header), Source.POZWOLENIA, QUERY)

    assert case.kubatura == 650.5


def test_unknown_volume_column_is_reported_to_admin_and_leads_keep_flowing(tmp_path, gunb_zip, caplog):
    header = ["objetosc" if column == "kubatura" else column for column in POZWOLENIA_HEADER]
    row = pozwolenie()
    row["objetosc"] = row.pop("kubatura")

    with caplog.at_level(logging.WARNING):
        cases = parse_archive(archive_with(tmp_path, gunb_zip, [row], header=header), Source.POZWOLENIA, QUERY)

    assert [case.id_sprawy for case in cases] == [CASE_ID]  # dane płyną dalej…
    assert cases[0].kubatura is None
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "kubatura" in errors[0]  # …a ERROR trafia na cichy kanał admina


def test_missing_case_number_column_stops_with_clear_format_error(tmp_path, gunb_zip):
    header = [column for column in POZWOLENIA_HEADER if column != "numer_gunb"]
    with pytest.raises(GunbFormatError, match="numer_gunb"):
        parse_archive(archive_with(tmp_path, gunb_zip, [pozwolenie()], header=header), Source.POZWOLENIA, QUERY)


def test_empty_csv_is_a_format_error(tmp_path):
    path = tmp_path / "wynik_opolskie.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("wynik_opolskie.csv", b"")
    with pytest.raises(GunbFormatError, match="pusty plik CSV"):
        parse_archive(path, Source.POZWOLENIA, QUERY)


def test_windows_1250_file_keeps_polish_letters_and_noise_filter_works(tmp_path, gunb_zip, caplog):
    rows = [
        pozwolenie(numer_gunb="A/1", nazwa_zam_budowlanego="Budowa budynku mieszkalnego jednorodzinnego w Łężanach"),
        pozwolenie(numer_gunb="B/1", nazwa_zam_budowlanego="Przyłącze gazowe do budynku mieszkalnego"),
    ]
    with caplog.at_level(logging.WARNING):
        cases = {case.id_sprawy: case
                 for case in parse_archive(archive_with(tmp_path, gunb_zip, rows, encoding="cp1250"),
                                           Source.POZWOLENIA, QUERY)}

    assert cases["A/1"].nazwa_zamierzenia == "Budowa budynku mieszkalnego jednorodzinnego w Łężanach"
    assert "�" not in cases["B/1"].nazwa_zamierzenia  # bez „krzaczków” zamiast polskich liter
    decision = LeadFilter(FilterConfig()).evaluate(cases["B/1"])
    assert decision.keep is False and "przylacz" in decision.reason  # słowo z „ł” nadal odsiewa szum
    assert "Windows-1250" in caplog.text


# --- Brudne pole „Projektant”: nazwisko + telefon + adres w jednym ----------------------------------

@pytest.mark.parametrize(
    "raw, name, phone, email, address",
    [
        ("Kowalski tel. 600 123 456, ul. Długa 5, 10-123 Olsztyn",
         "Kowalski", "+48600123456", None, "ul. Długa 5, 10-123 Olsztyn"),
        ("Jan Kowalski, e-mail: jan.kowalski@pracownia.test, kom. 600-123-456",
         "Jan Kowalski", "+48600123456", "jan.kowalski@pracownia.test", None),
        ("ARCH-BUD Pracownia Projektowa ul. Leśna 3/4 10-555 Olsztyn tel. (89) 527 00 00",
         "ARCH-BUD Pracownia Projektowa", "+48895270000", None, "ul. Leśna 3/4 10-555 Olsztyn"),
        ("Nowak, 10-123 Olsztyn, ul. Polna 1", "Nowak", None, None, "10-123 Olsztyn, ul. Polna 1"),
        ("Anna Nowak", "Anna Nowak", None, None, None),
    ],
)
def test_dirty_designer_text_is_split_into_name_phone_email_and_address(raw, name, phone, email, address):
    parts = split_contact(raw)
    assert (parts.name, parts.phone, parts.email, parts.address) == (name, phone, email, address)


def test_dirty_designer_field_is_cleaned_end_to_end(qa_workdir, http, gunb_zip):
    row = pozwolenie(projektant_imie="Jan",
                     projektant_nazwisko="Kowalski tel. 600 123 456 ul. Długa 5, 10-123 Olsztyn",
                     projektant_numer_uprawnien="WAM/0123/POOK/05")
    http.add(responses.GET, GUNB_OPOLSKIE_ZIP, body=gunb_zip([row]))

    assert run_fetch(qa_workdir) == 0

    with LeadRepository(qa_workdir / "data" / "qa.sqlite") as repo:
        lead = repo.get(CASE_ID)
    assert lead.projektant == "Jan Kowalski"  # na karcie leada samo nazwisko…
    assert lead.telefon == "+48600123456"  # …telefon we własnej kolumnie (kafelek „Zadzwoń”)
    assert lead.projektant_uprawnienia == "WAM/0123/POOK/05"


def test_phone_typed_into_license_field_is_not_shown_as_license(qa_workdir, http, gunb_zip):
    row = pozwolenie(projektant_numer_uprawnien="733-110-133")
    http.add(responses.GET, GUNB_OPOLSKIE_ZIP, body=gunb_zip([row]))

    assert run_fetch(qa_workdir) == 0

    with LeadRepository(qa_workdir / "data" / "qa.sqlite") as repo:
        lead = repo.get(CASE_ID)
    assert lead.projektant_uprawnienia is None
    assert lead.telefon == "+48733110133"
