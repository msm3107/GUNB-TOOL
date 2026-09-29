"""Pobieranie i parsowanie oficjalnych paczek danych rejestru RWDZ (GUNB).

Wyszukiwarka ``wyszukiwarka.gunb.gov.pl`` jest chroniona CAPTCHA, dlatego scraper korzysta
z udostępnianych przez GUNB plików „Dane do pobrania” (aktualizowanych co noc):

* ``wynik_<województwo>.zip`` – Rejestr Wniosków i Decyzji (pozwolenia na budowę),
* ``wynik_zgloszenia_2022_up.zip`` – Rejestr Zgłoszeń dla całego kraju.

Pliki CSV mają kilkadziesiąt–kilkaset MB, więc są czytane strumieniowo; wiersze (jedna działka
= jeden wiersz) są filtrowane od razu, scalane w sprawy, a wynik jest zwracany stronami.
"""

from __future__ import annotations

import csv
import io
import logging
import math
import re
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterator, Protocol, Sequence

from .http_client import DownloadResult
from .models import GunbCase, Parcel, Source, Status
from .teryt import Voivodeship, get_voivodeship
from .text import clean

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://wyszukiwarka.gunb.gov.pl/pliki_pobranie/"
ZGLOSZENIA_FILE = "wynik_zgloszenia_2022_up.zip"

_DELIMITERS = (";", "#", ",", "\t", "|")
_JEDNOSTKA_RE = re.compile(r"^\d{6}_\d$")
_PARCEL_SPLIT_RE = re.compile(r"\s*[;,]\s*")
_FULL_PARCEL_ID_RE = re.compile(r"^\d{6}_\d\.[^.\s]+\.\S+$")
_PARCEL_NUMBER_RE = re.compile(r"\d+(?:/\d+)*")

# Logiczne pole -> możliwe nazwy kolumn (pierwsza istniejąca wygrywa). Alternatywy chronią
# przed drobnymi zmianami formatu po stronie GUNB (np. poprawką literówki „jednosta_numer_ew”).
_COLUMNS: dict[Source, dict[str, tuple[str, ...]]] = {
    Source.POZWOLENIA: {
        "id": ("numer_gunb",),
        "numer_urzedu": ("numer_urzad",),
        "organ": ("nazwa_organu",),
        "data_wplywu": ("data_wplywu_wniosku", "data_wplywu_wniosku_do_urzedu"),
        "numer_decyzji": ("numer_decyzji_urzedu",),
        "data_decyzji": ("data_wydania_decyzji",),
        "stan": ("stan", "status"),
        "inwestor": ("nazwa_inwestor", "nazwa_inwestora"),
        "wojewodztwo": ("wojewodztwo", "wojewodztwo_objekt", "wojewodztwo_obiekt"),
        "miasto": ("miasto",),
        "kod_pocztowy": ("obiekt_kod_pocztowy", "kod_pocztowy"),
        "terc": ("terc",),
        "cecha": ("cecha",),
        "ulica": ("ulica",),
        "ulica_dalej": ("ulica_dalej",),
        "nr_domu": ("nr_domu",),
        "rodzaj_inwestycji": ("rodzaj_inwestycji",),
        "kategoria": ("kategoria",),
        "rodzaj_robot": ("nazwa_zamierzenia_bud",),
        "nazwa_zamierzenia": ("nazwa_zam_budowlanego",),
        "kubatura": ("kubatura",),
        "projektant_nazwisko": ("projektant_nazwisko", "nazwisko_projektanta"),
        "projektant_imie": ("projektant_imie", "imie_projektanta"),
        "projektant_uprawnienia": ("projektant_numer_uprawnien",),
        "jednostka": ("jednosta_numer_ew", "jednostka_numer_ew", "jednostki_numer"),
        "obreb": ("obreb_numer",),
        "dzialka": ("numer_dzialki",),
        "arkusz": ("numer_arkusza_dzialki",),
    },
    Source.ZGLOSZENIA: {
        "id": ("numer_ewidencyjny_system",),
        "numer_urzedu": ("numer_ewidencyjny_urzad",),
        "organ": ("nazwa_organu",),
        "data_wplywu": ("data_wplywu_wniosku_do_urzedu", "data_wplywu_wniosku"),
        "stan": ("stan",),
        "wojewodztwo": ("wojewodztwo_objekt", "wojewodztwo_obiekt", "wojewodztwo"),
        "kod_pocztowy": ("obiekt_kod_pocztowy", "kod_pocztowy"),
        "miasto": ("miasto",),
        "terc": ("terc",),
        "cecha": ("cecha",),
        "ulica": ("ulica",),
        "ulica_dalej": ("ulica_dalej",),
        "nr_domu": ("nr_domu",),
        "kategoria": ("kategoria",),
        "nazwa_zamierzenia": ("nazwa_zam_budowlanego",),
        "rodzaj_robot": ("rodzaj_zam_budowlanego",),
        "kubatura": ("kubatura",),
        "jednostka": ("jednostki_numer", "jednosta_numer_ew", "jednostka_numer_ew"),
        "obreb": ("obreb_numer",),
        "dzialka": ("numer_dzialki",),
        "arkusz": ("numer_arkusza_dzialki",),
        "projektant_nazwisko": ("nazwisko_projektanta", "projektant_nazwisko"),
        "projektant_imie": ("imie_projektanta", "projektant_imie"),
        "projektant_uprawnienia": ("projektant_numer_uprawnien",),
    },
}
_REQUIRED_FIELDS = ("id",)
_IMPORTANT_FIELDS = ("data_wplywu", "terc", "jednostka", "nazwa_zamierzenia")


class GunbFormatError(RuntimeError):
    """Paczka GUNB ma nieoczekiwany format (uszkodzony ZIP, brak kolumny itp.)."""


class Downloader(Protocol):
    """Interfejs pobierania wykorzystywany przez scraper (spełnia go ``ResilientHttpClient``)."""

    def download(self, url: str, dest: Path, *, validator=None) -> DownloadResult: ...


@dataclass(frozen=True)
class FetchQuery:
    """Zakres danych do pobrania.

    Attributes:
        voivodeships: dwucyfrowe kody TERYT województw.
        powiats: czterocyfrowe kody TERYT powiatów (pusty zbiór = bez filtra).
        date_from: najwcześniejsza data (włącznie) – patrz ``date_field``.
        date_to: najpóźniejsza data (włącznie).
        date_field: ``"decyzja"`` (data wydania decyzji) lub ``"wplyw"`` (data złożenia);
            zgłoszenia i wnioski bez decyzji zawsze filtrowane są po dacie wpływu.
        sources: rejestry do przetworzenia.
    """

    voivodeships: tuple[str, ...]
    powiats: frozenset[str] = frozenset()
    date_from: date | None = None
    date_to: date | None = None
    date_field: str = "decyzja"
    sources: tuple[Source, ...] = (Source.POZWOLENIA,)


@dataclass
class Page:
    """Strona wyników: fragment spraw z jednej paczki."""

    source: Source
    label: str
    number: int
    total_pages: int
    total_items: int
    items: list[GunbCase] = field(default_factory=list)


@dataclass
class ParseStats:
    """Statystyki parsowania jednej paczki (do logów)."""

    rows: int = 0
    matched_rows: int = 0
    malformed_rows: int = 0
    cases: int = 0


class GunbScraper:
    """Pobiera paczki RWDZ i zwraca pasujące sprawy stronami.

    Args:
        http: klient z metodą ``download`` (zwykle :class:`~gunb_tool.http_client.ResilientHttpClient`).
        cache_dir: katalog na pobrane paczki (pozwala na pobieranie warunkowe przy kolejnych uruchomieniach).
        base_url: adres katalogu z paczkami GUNB.
    """

    def __init__(self, http: Downloader, cache_dir: Path, base_url: str = DEFAULT_BASE_URL) -> None:
        self.http = http
        self.cache_dir = Path(cache_dir)
        self.base_url = base_url.rstrip("/") + "/"

    def archive_url(self, source: Source, voivodeship: Voivodeship | None = None) -> str:
        """Adres paczki dla danego rejestru (pozwolenia wymagają województwa)."""
        if source is Source.ZGLOSZENIA:
            return self.base_url + ZGLOSZENIA_FILE
        if voivodeship is None:
            raise ValueError("Paczki pozwoleń są publikowane per województwo – podaj województwo")
        return f"{self.base_url}wynik_{voivodeship.slug}.zip"

    def download(self, source: Source, voivodeship: Voivodeship | None = None) -> DownloadResult:
        """Pobiera (lub potwierdza aktualność) paczki w katalogu cache."""
        url = self.archive_url(source, voivodeship)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.http.download(url, self.cache_dir / url.rsplit("/", 1)[1], validator=zipfile.is_zipfile)

    def fetch_pages(self, query: FetchQuery, page_size: int = 200) -> Iterator[Page]:
        """Pobiera paczki wskazane w zapytaniu i zwraca pasujące sprawy stronami po ``page_size``.

        Sprawy w obrębie paczki są posortowane od najnowszego zdarzenia (decyzja/wpływ).
        """
        for source in query.sources:
            if source is Source.POZWOLENIA:
                for code in query.voivodeships:
                    voivodeship = get_voivodeship(code)
                    archive = self.download(source, voivodeship)
                    cases = parse_archive(archive.path, source, query, default_voivodeship=voivodeship.code)
                    yield from paginate(cases, source, f"{source.value}/{voivodeship.name}", page_size)
            else:
                archive = self.download(source)
                cases = parse_archive(archive.path, source, query)
                names = ",".join(get_voivodeship(c).name for c in query.voivodeships)
                yield from paginate(cases, source, f"{source.value}/{names}", page_size)


def paginate(cases: Sequence[GunbCase], source: Source, label: str, page_size: int) -> Iterator[Page]:
    """Dzieli listę spraw na strony (numeracja od 1)."""
    if page_size < 1:
        raise ValueError("page_size musi być >= 1")
    total_pages = math.ceil(len(cases) / page_size)
    for number in range(total_pages):
        start = number * page_size
        yield Page(source, label, number + 1, total_pages, len(cases), list(cases[start:start + page_size]))


def parse_archive(
    path: Path, source: Source, query: FetchQuery, *, default_voivodeship: str | None = None
) -> list[GunbCase]:
    """Czyta paczkę ZIP GUNB i zwraca sprawy pasujące do zapytania (najnowsze pierwsze).

    Args:
        path: plik ZIP z jednym plikiem CSV.
        source: rejestr, z którego pochodzi plik (wyznacza mapowanie kolumn).
        query: filtry województwa, powiatu i daty.
        default_voivodeship: województwo przypisywane wierszom bez kodu TERYT i nazwy
            (paczki pozwoleń są per województwo).

    Raises:
        GunbFormatError: uszkodzone archiwum lub brak wymaganej kolumny.
    """
    stats = ParseStats()
    cases: dict[str, GunbCase] = {}
    parcel_sets: dict[str, set[Parcel]] = {}
    voivodeships = frozenset(query.voivodeships)
    use_decision_date = source is Source.POZWOLENIA and query.date_field == "decyzja"
    has_date_filter = query.date_from is not None or query.date_to is not None

    try:
        with _open_csv(Path(path)) as (header, rows):
            idx = _resolve_columns(header, _COLUMNS[source], Path(path))
            i_id, i_terc, i_jedn, i_woj = idx["id"], idx["terc"], idx["jednostka"], idx["wojewodztwo"]
            for row in rows:
                stats.rows += 1
                if len(row) != len(header):
                    stats.malformed_rows += 1
                    continue

                teryt = _gmina_teryt(_cell(row, i_terc), _cell(row, i_jedn))
                voivodeship = teryt[:2] if teryt else (_voivodeship_code(_cell(row, i_woj)) or default_voivodeship)
                if voivodeship not in voivodeships:
                    continue
                if query.powiats and teryt[:4] not in query.powiats:
                    continue
                if has_date_filter and not _date_matches(row, idx, use_decision_date, query):
                    continue

                case_id = clean(row[i_id])
                if not case_id:
                    stats.malformed_rows += 1
                    continue
                stats.matched_rows += 1
                case = cases.get(case_id)
                if case is None:
                    case = cases[case_id] = _build_case(row, idx, source, case_id)
                    parcel_sets[case_id] = set()
                for parcel in _parcels(row, idx):
                    if parcel not in parcel_sets[case_id]:
                        parcel_sets[case_id].add(parcel)
                        case.parcels.append(parcel)
    except zipfile.BadZipFile as exc:
        raise GunbFormatError(f"{Path(path).name}: uszkodzone archiwum ZIP ({exc})") from exc

    stats.cases = len(cases)
    log.info(
        "%s: %d wierszy, %d pasujących, %d spraw, %d wierszy pominiętych (zły format)",
        Path(path).name, stats.rows, stats.matched_rows, stats.cases, stats.malformed_rows,
    )
    return sorted(cases.values(), key=lambda c: (c.event_date or date.min, c.id_sprawy), reverse=True)


# --- Parsowanie wiersza -----------------------------------------------------

def _build_case(row: list[str], idx: dict[str, int | None], source: Source, case_id: str) -> GunbCase:
    def value(name: str) -> str | None:
        return clean(_cell(row, idx.get(name)))

    stan = value("stan")
    numer_decyzji = value("numer_decyzji")
    data_decyzji = _parse_date(value("data_decyzji"))
    status = Status.from_gunb_text(stan)
    if status is None:
        if source is Source.ZGLOSZENIA:
            status = Status.ZGLOSZENIE
        else:
            status = Status.DECYZJA if (data_decyzji or numer_decyzji) else Status.WNIOSEK

    return GunbCase(
        id_sprawy=case_id,
        source=source,
        status=status,
        status_raw=stan,
        numer_urzedu=value("numer_urzedu"),
        organ=value("organ"),
        data_wplywu=_parse_date(value("data_wplywu")),
        data_decyzji=data_decyzji,
        numer_decyzji=numer_decyzji,
        inwestor_raw=value("inwestor"),
        wojewodztwo=value("wojewodztwo"),
        miasto=value("miasto"),
        kod_pocztowy=value("kod_pocztowy"),
        terc=value("terc"),
        ulica=_street(value("cecha"), value("ulica_dalej"), value("ulica")),
        nr_domu=value("nr_domu"),
        rodzaj_inwestycji=value("rodzaj_inwestycji"),
        kategoria_obiektu=(value("kategoria") or "").upper() or None,
        rodzaj_robot=value("rodzaj_robot"),
        nazwa_zamierzenia=value("nazwa_zamierzenia"),
        kubatura=_parse_float(value("kubatura")),
        projektant_imie=value("projektant_imie"),
        projektant_nazwisko=value("projektant_nazwisko"),
        projektant_uprawnienia=value("projektant_uprawnienia"),
    )


def _parcels(row: list[str], idx: dict[str, int | None]) -> Iterator[Parcel]:
    """Działki z wiersza: pole numeru może zawierać kilka pozycji, pełne identyfikatory
    (``146510_8.0309.24/35``) albo dopiski („3/1 część”, „dz. nr 12/3”)."""
    for token in _PARCEL_SPLIT_RE.split(_cell(row, idx.get("dzialka")).strip()):
        if _FULL_PARCEL_ID_RE.match(token):
            try:
                yield Parcel.from_id(token)
            except ValueError:
                log.debug("Pominięto niepoprawny identyfikator działki %r", token)
            continue
        number = _PARCEL_NUMBER_RE.search(token)
        if number is None:
            continue
        parcel = Parcel.from_raw(
            _cell(row, idx.get("jednostka")), _cell(row, idx.get("obreb")), number.group(0),
            _cell(row, idx.get("arkusz")),
        )
        if parcel is not None:
            yield parcel


def _date_matches(row: list[str], idx: dict[str, int | None], use_decision_date: bool, query: FetchQuery) -> bool:
    moment = _parse_date(_cell(row, idx.get("data_decyzji"))) if use_decision_date else None
    moment = moment or _parse_date(_cell(row, idx.get("data_wplywu")))
    if moment is None:
        return False
    if query.date_from is not None and moment < query.date_from:
        return False
    return query.date_to is None or moment <= query.date_to


def _street(cecha: str | None, ulica_dalej: str | None, ulica: str | None) -> str | None:
    """Pełna nazwa ulicy wg konwencji TERYT: cecha + druga część nazwy + główna część nazwy."""
    if not ulica:
        return None
    return " ".join(part for part in (cecha, ulica_dalej, ulica) if part)


def _gmina_teryt(terc: str, jednostka: str) -> str:
    """Siedmiocyfrowy kod gminy z ``terc`` lub z identyfikatora jednostki (``160705_4`` -> ``1607054``)."""
    terc = terc.strip()
    if len(terc) == 7 and terc.isdigit():
        return terc
    jednostka = jednostka.strip()
    if _JEDNOSTKA_RE.match(jednostka):
        return jednostka[:6] + jednostka[7]
    return ""


@lru_cache(maxsize=64)
def _voivodeship_code(name: str) -> str | None:
    name = name.strip()
    if not name or name == '""':
        return None
    try:
        return get_voivodeship(name).code
    except KeyError:
        return None


def _cell(row: list[str], index: int | None) -> str:
    return row[index] if index is not None else ""


def _parse_date(value: str | None) -> date | None:
    """Parsuje ``RRRR-MM-DD[ hh:mm:ss]`` lub ``DD.MM.RRRR``."""
    if not value:
        return None
    text = value.strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.strptime(text, "%d.%m.%Y").date()
    except ValueError:
        return None


def _parse_float(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace("\xa0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


# --- CSV --------------------------------------------------------------------

@contextmanager
def _open_csv(path: Path) -> Iterator[tuple[list[str], Iterator[list[str]]]]:
    """Otwiera CSV z archiwum: wykrywa separator, usuwa BOM, deduplikuje nazwy kolumn."""
    with zipfile.ZipFile(path) as archive:
        member = _csv_member(archive, path)
        with archive.open(member) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
            header_line = text.readline()
            if not header_line.strip():
                raise GunbFormatError(f"{path.name}: pusty plik CSV")
            delimiter = _detect_delimiter(header_line, path)
            header = _dedupe([h.strip().lower() for h in next(csv.reader([header_line], delimiter=delimiter))])
            yield header, csv.reader(text, delimiter=delimiter)


def _csv_member(archive: zipfile.ZipFile, path: Path) -> str:
    names = [n for n in archive.namelist() if not n.endswith("/")]
    csv_names = [n for n in names if n.lower().endswith(".csv")]
    if csv_names or names:
        return (csv_names or names)[0]
    raise GunbFormatError(f"{path.name}: archiwum nie zawiera pliku CSV")


def _detect_delimiter(header_line: str, path: Path) -> str:
    counts = {d: header_line.count(d) for d in _DELIMITERS}
    delimiter = max(counts, key=counts.get)
    if counts[delimiter] == 0:
        raise GunbFormatError(f"{path.name}: nie rozpoznano separatora kolumn w nagłówku")
    return delimiter


def _dedupe(header: list[str]) -> list[str]:
    """Nadaje unikalne nazwy powtórzonym kolumnom (``cecha``, ``cecha_2``...)."""
    seen: dict[str, int] = {}
    result = []
    for name in header:
        seen[name] = seen.get(name, 0) + 1
        result.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return result


def _resolve_columns(header: list[str], columns: dict[str, tuple[str, ...]], path: Path) -> dict[str, int | None]:
    positions = {name: i for i, name in reversed(list(enumerate(header)))}
    index = {name: next((positions[c] for c in candidates if c in positions), None)
             for name, candidates in columns.items()}
    for name in _REQUIRED_FIELDS:
        if index[name] is None:
            raise GunbFormatError(
                f"{path.name}: brak wymaganej kolumny {' / '.join(columns[name])} – "
                "GUNB mógł zmienić format pliku"
            )
    missing = [name for name in _IMPORTANT_FIELDS if index.get(name) is None]
    if missing:
        log.warning("%s: brak kolumn %s – część pól leadów będzie pusta", path.name, ", ".join(missing))
    return index
