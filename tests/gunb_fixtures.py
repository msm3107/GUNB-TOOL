"""Syntetyczne paczki danych w formacie GUNB (bez prawdziwych danych osobowych)."""

from __future__ import annotations

import zipfile
from pathlib import Path

POZWOLENIA_HEADER = (
    "numer_urzad;numer_gunb;nazwa_organu;adres_organu;data_wplywu_wniosku;numer_decyzji_urzedu;"
    "data_wydania_decyzji;nazwa_inwestor;wojewodztwo;miasto;terc;cecha;cecha;ulica;ulica_dalej;nr_domu;"
    "rodzaj_inwestycji;kategoria;nazwa_zamierzenia_bud;nazwa_zam_budowlanego;kubatura;projektant_nazwisko;"
    "projektant_imie;projektant_numer_uprawnien;jednosta_numer_ew;obreb_numer;numer_dzialki;"
    "numer_arkusza_dzialki;jednostka_stara_numeracja_z_wniosku;stara_numeracja_obreb_z_wnioskiu;"
    "stara_numeracja_dzialka_z_wniosku"
).split(";")

ZGLOSZENIA_HEADER = (
    "numer_ewidencyjny_system;numer_ewidencyjny_urzad;data_wplywu_wniosku_do_urzedu;nazwa_organu;"
    "wojewodztwo_objekt;obiekt_kod_pocztowy;miasto;terc;cecha;cecha;ulica;ulica_dalej;nr_domu;kategoria;"
    "nazwa_zam_budowlanego;rodzaj_zam_budowlanego;kubatura;stan;jednostki_numer;obreb_numer;numer_dzialki;"
    "numer_arkusza_dzialki;nazwisko_projektanta;imie_projektanta;projektant_numer_uprawnien;"
    "projektant_pozostali"
).split(";")


def pozwolenie(**values: str) -> dict[str, str]:
    """Wiersz pliku pozwoleń z sensownymi wartościami domyślnymi (klucze = nazwy kolumn)."""
    row = {
        "numer_urzad": "AB.6740.1.2026",
        "numer_gunb": "ST-OP-NY/WNIOSEK/1/2026",
        "nazwa_organu": "Starosta Powiatu Testowego",
        "adres_organu": "ul. Urzędowa 1, 00-001 Testowo",
        "data_wplywu_wniosku": "2026-08-01 00:00:00",
        "numer_decyzji_urzedu": "10/2026",
        "data_wydania_decyzji": "2026-09-10 00:00:00",
        "nazwa_inwestor": '""',
        "wojewodztwo": "opolskie",
        "miasto": "Nysa",
        "terc": "1607054",
        "cecha": "ul.  ",
        "ulica": "Testowa",
        "ulica_dalej": '""',
        "nr_domu": "5",
        "rodzaj_inwestycji": "Budynek mieszkalny jednorodzinny",
        "kategoria": "I",
        "nazwa_zamierzenia_bud": "budowa nowego/nowych obiektów budowlanych",
        "nazwa_zam_budowlanego": "Budowa budynku mieszkalnego jednorodzinnego",
        "kubatura": "650.5",
        "projektant_nazwisko": "Testowy",
        "projektant_imie": "Jan",
        "projektant_numer_uprawnien": "OPL/0001/PWOA/20",
        "jednosta_numer_ew": "160705_4",
        "obreb_numer": "0005",
        "numer_dzialki": "13",
        "numer_arkusza_dzialki": "",
    }
    row.update(values)
    return row


def zgloszenie(**values: str) -> dict[str, str]:
    """Wiersz pliku zgłoszeń z wartościami domyślnymi."""
    row = {
        "numer_ewidencyjny_system": "ST-OP-NY/ZGŁOSZENIE/1/2026",
        "numer_ewidencyjny_urzad": "1/2026",
        "data_wplywu_wniosku_do_urzedu": "2026-09-15 00:00:00",
        "nazwa_organu": "Starosta Powiatu Testowego",
        "wojewodztwo_objekt": "opolskie",
        "obiekt_kod_pocztowy": "48-300",
        "miasto": "Nysa",
        "terc": "1607054",
        "cecha": "ul.  ",
        "ulica": "Sychty",
        "ulica_dalej": "ks. Bernarda ",
        "nr_domu": "58",
        "kategoria": "I",
        "nazwa_zam_budowlanego": "Budowa budynku mieszkalnego jednorodzinnego do 70 m2",
        "rodzaj_zam_budowlanego": "budowa nowego/nowych obiektów budowlanych",
        "kubatura": "",
        "stan": "Brak sprzeciwu",
        "jednostki_numer": "160705_4",
        "obreb_numer": "0005",
        "numer_dzialki": "21/4",
        "numer_arkusza_dzialki": "",
        "nazwisko_projektanta": "Projektantka",
        "imie_projektanta": "Anna",
        "projektant_numer_uprawnien": "OPL/0002/PBKb/19",
        "projektant_pozostali": '""',
    }
    row.update(values)
    return row


def _quote(value: str, delimiter: str) -> str:
    if value == '""':
        return value
    if delimiter in value or "\n" in value or '"' in value:
        return '"' + value.replace('"', '""') + '"'
    return value


def write_zip(
    path: Path,
    header: list[str],
    rows: list[dict[str, str] | list[str]],
    *,
    delimiter: str = ";",
    bom: bool = True,
    member: str | None = None,
) -> Path:
    """Zapisuje paczkę ZIP z jednym plikiem CSV w formacie GUNB."""
    lines = [delimiter.join(header)]
    for row in rows:
        if isinstance(row, dict):
            values = [row.get(col, "") for col in header]
        else:
            values = row
        lines.append(delimiter.join(_quote(v, delimiter) for v in values))
    text = ("﻿" if bom else "") + "\n".join(lines) + "\n"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member or path.with_suffix(".csv").name, text.encode("utf-8"))
    return path
