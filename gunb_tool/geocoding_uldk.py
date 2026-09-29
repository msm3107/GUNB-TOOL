"""Geokodowanie działek przez ULDK (Usługa Lokalizacji Działek Katastralnych, GUGiK).

Identyfikator działki (``WWPPGG_R.OOOO.NR``) zamieniany jest na współrzędne WGS84 (centroid
geometrii), nazwy jednostek administracyjnych oraz linki do Google Maps i Geoportalu.
Gdy żadnej działki sprawy nie ma już w ewidencji (np. po podziale), używany jest środek obrębu.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol, Sequence
from urllib.parse import quote

from .http_client import HttpError, ResilientHttpClient
from .models import Parcel

log = logging.getLogger(__name__)

DEFAULT_ULDK_URL = "https://uldk.gugik.gov.pl/"
GOOGLE_MAPS_URL = "https://www.google.com/maps?q={lat:.6f},{lon:.6f}"
GEOPORTAL_PARCEL_URL = "https://mapy.geoportal.gov.pl/imap/Imgp_2.html?identifyParcel={parcel_id}"

PARCEL_FIELDS: tuple[str, ...] = ("id", "voivodeship", "county", "commune", "geom_extent", "geom_wkt")
REGION_FIELDS: tuple[str, ...] = ("id", "region", "commune", "county", "voivodeship", "geom_extent")

_NUMBER = r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?"
_POINT_RE = re.compile(rf"({_NUMBER})\s+({_NUMBER})")
_POLYGON_SPLIT_RE = re.compile(r"\)\s*\)\s*,\s*\(\s*\(")
_RING_SPLIT_RE = re.compile(r"\)\s*,\s*\(")
_SRID_RE = re.compile(r"^\s*SRID=(\d+)\s*;", re.IGNORECASE)

WGS84_SRID = 4326
POLAND_BOUNDS = (48.9, 55.0, 14.0, 24.3)
"""Zakres (lat_min, lat_max, lon_min, lon_max) akceptowanych współrzędnych – Polska z marginesem."""


class UldkError(RuntimeError):
    """Nieoczekiwana odpowiedź usługi ULDK."""


class GeoPrecision(str, enum.Enum):
    """Dokładność lokalizacji leada."""

    PARCEL = "dzialka"
    REGION = "obreb"


@dataclass(frozen=True)
class GeocodeResult:
    """Lokalizacja sprawy: współrzędne WGS84, jednostki administracyjne i linki."""

    lat: float
    lon: float
    precision: GeoPrecision
    parcel_id: str | None
    region_id: str | None
    voivodeship: str | None
    county: str | None
    commune: str | None
    google_maps_url: str
    geoportal_url: str | None

    def to_dict(self) -> dict[str, Any]:
        """Postać do zapisu w cache."""
        data = asdict(self)
        data["precision"] = self.precision.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GeocodeResult:
        """Odtwarza wynik z :meth:`to_dict`; link Google Maps budowany jest zawsze w bieżącym formacie."""
        values = {**data, "precision": GeoPrecision(data["precision"])}
        values["google_maps_url"] = google_maps_url(values["lat"], values["lon"])
        return cls(**values)


@dataclass(frozen=True)
class CachedGeocode:
    """Wpis cache; ``result=None`` oznacza, że ULDK nie znalazł obiektu (cache negatywny)."""

    result: GeocodeResult | None


class GeocodeCache(Protocol):
    """Magazyn wyników geokodowania (implementowany m.in. przez ``storage.LeadRepository``)."""

    def get(self, key: str) -> CachedGeocode | None: ...

    def set(self, key: str, result: GeocodeResult | None) -> None: ...


class MemoryGeocodeCache:
    """Prosty cache w pamięci (na czas jednego uruchomienia / testów)."""

    def __init__(self) -> None:
        self._data: dict[str, CachedGeocode] = {}

    def get(self, key: str) -> CachedGeocode | None:
        return self._data.get(key)

    def set(self, key: str, result: GeocodeResult | None) -> None:
        self._data[key] = CachedGeocode(result)


class UldkClient:
    """Niskopoziomowy klient ULDK (odpowiedzi tekstowe: status w 1. linii, pola rozdzielone ``|``)."""

    def __init__(self, http: ResilientHttpClient, base_url: str = DEFAULT_ULDK_URL) -> None:
        self.http = http
        self.base_url = base_url

    def find_parcels(self, parcel_id: str) -> list[dict[str, str]]:
        """``GetParcelByIdOrNr`` – działki pasujące do identyfikatora (ULDK sam dopasowuje arkusz)."""
        return self._query("GetParcelByIdOrNr", parcel_id, PARCEL_FIELDS)

    def get_region(self, region_id: str) -> dict[str, str] | None:
        """``GetRegionById`` – obręb ewidencyjny (``WWPPGG_R.OOOO``)."""
        rows = self._query("GetRegionById", region_id, REGION_FIELDS)
        return rows[0] if rows else None

    def _query(self, request: str, object_id: str, fields: Sequence[str]) -> list[dict[str, str]]:
        params = {"request": request, "id": object_id, "result": ",".join(fields), "srid": "4326"}
        response = self.http.get(self.base_url, params=params)
        if response.status_code != 200:
            raise UldkError(f"ULDK {request} {object_id}: HTTP {response.status_code}")
        response.encoding = "utf-8"
        return parse_uldk_response(response.text, fields)


class UldkGeocoder:
    """Wyznacza lokalizację sprawy na podstawie jej działek.

    Args:
        client: klient ULDK.
        cache: cache wyników (także negatywnych); domyślnie w pamięci.
        max_parcels: ile działek sprawy sprawdzić, zanim użyjemy środka obrębu.
        region_fallback: czy używać środka obrębu, gdy żadnej działki nie znaleziono.
        failure_threshold: po tylu kolejnych błędach sieci geokodowanie jest wyłączane
            do końca uruchomienia (chroni przed zawieszeniem przy awarii ULDK).
    """

    def __init__(
        self,
        client: UldkClient,
        cache: GeocodeCache | None = None,
        *,
        max_parcels: int = 3,
        region_fallback: bool = True,
        failure_threshold: int = 3,
    ) -> None:
        self.client = client
        self.cache = cache if cache is not None else MemoryGeocodeCache()
        self.max_parcels = max_parcels
        self.region_fallback = region_fallback
        self.failure_threshold = failure_threshold
        self._consecutive_failures = 0

    @property
    def disabled(self) -> bool:
        """Czy zadziałał bezpiecznik (zbyt wiele kolejnych błędów ULDK)."""
        return self._consecutive_failures >= self.failure_threshold

    def geocode(self, parcels: Sequence[Parcel]) -> GeocodeResult | None:
        """Zwraca lokalizację pierwszej odnalezionej działki, a w razie potrzeby – obrębu.

        Błędy sieci nie są cache'owane i nie przerywają przetwarzania (wynik ``None``).
        """
        if not parcels or self.disabled:
            return None
        try:
            for parcel in parcels[: self.max_parcels]:
                result = self._cached(f"parcel:{parcel.uldk_id}", lambda p=parcel: self._locate_parcel(p))
                if result is not None:
                    return result
            if self.region_fallback:
                region_id = parcels[0].obreb_id
                return self._cached(f"region:{region_id}", lambda: self._locate_region(region_id))
            return None
        except (HttpError, UldkError) as exc:
            self._consecutive_failures += 1
            log.warning("ULDK: błąd geokodowania (%s)", exc)
            if self.disabled:
                log.error("ULDK: %d kolejnych błędów – geokodowanie wyłączone do końca uruchomienia",
                          self._consecutive_failures)
            return None

    def _cached(self, key: str, lookup) -> GeocodeResult | None:
        cached = self.cache.get(key)
        if cached is not None:
            return cached.result
        result = lookup()
        self._consecutive_failures = 0
        self.cache.set(key, result)
        return result

    def _locate_parcel(self, parcel: Parcel) -> GeocodeResult | None:
        rows = self.client.find_parcels(parcel.uldk_id)
        if not rows:
            return None
        row = rows[0]
        if parcel.arkusz:
            marker = f".AR_{parcel.arkusz}."
            row = next((r for r in rows if marker in r.get("id", "")), row)
        point = parcel_point(row)
        if point is None:
            return None
        lat, lon = point
        parcel_id = row.get("id") or parcel.uldk_id
        return GeocodeResult(
            lat=lat,
            lon=lon,
            precision=GeoPrecision.PARCEL,
            parcel_id=parcel_id,
            region_id=parcel.obreb_id,
            voivodeship=row.get("voivodeship") or None,
            county=row.get("county") or None,
            commune=row.get("commune") or None,
            google_maps_url=google_maps_url(lat, lon),
            geoportal_url=geoportal_parcel_url(parcel_id),
        )

    def _locate_region(self, region_id: str) -> GeocodeResult | None:
        row = self.client.get_region(region_id)
        point = parcel_point(row) if row else None
        if row is None or point is None:
            return None
        lat, lon = point
        return GeocodeResult(
            lat=lat,
            lon=lon,
            precision=GeoPrecision.REGION,
            parcel_id=None,
            region_id=row.get("id") or region_id,
            voivodeship=row.get("voivodeship") or None,
            county=row.get("county") or None,
            commune=row.get("commune") or None,
            google_maps_url=google_maps_url(lat, lon),
            geoportal_url=None,
        )


# --- Funkcje pomocnicze -------------------------------------------------------

def parse_uldk_response(text: str, fields: Sequence[str]) -> list[dict[str, str]]:
    """Parsuje odpowiedź ULDK: 1. linia to status/liczba wyników (ujemna = brak), dalej wiersze ``a|b|c``.

    Raises:
        UldkError: gdy odpowiedź nie ma oczekiwanej struktury (np. strona błędu HTML).
    """
    lines = [line.strip() for line in text.strip().splitlines()]
    if not lines or not lines[0]:
        raise UldkError("pusta odpowiedź ULDK")
    try:
        status = int(lines[0].split()[0])
    except ValueError:
        raise UldkError(f"nieoczekiwana odpowiedź ULDK: {lines[0][:80]!r}") from None
    if status < 0:
        return []
    rows = []
    for line in lines[1:]:
        values = line.split("|")
        if len(values) == len(fields):
            rows.append(dict(zip(fields, (v.strip() for v in values))))
    return rows


def parcel_point(row: dict[str, str]) -> tuple[float, float] | None:
    """Punkt ``(lat, lon)`` w WGS84 z wiersza ULDK: centroid ``geom_wkt``, a gdy brak – środek ``geom_extent``.

    Zabezpiecza przed cichym zapisaniem współrzędnych w złym układzie (np. metrów EPSG:2180,
    które ULDK zwraca, gdy parametr ``srid`` zostanie pominięty).

    Raises:
        UldkError: geometria w układzie innym niż EPSG:4326 albo współrzędne poza Polską.
    """
    wkt = row.get("geom_wkt") or ""
    srid = wkt_srid(wkt)
    if srid is not None and srid != WGS84_SRID:
        raise UldkError(f"ULDK zwrócił geometrię w układzie EPSG:{srid} zamiast EPSG:{WGS84_SRID}")
    point = wkt_centroid(wkt) or extent_center(row.get("geom_extent") or "")
    if point is None:
        return None
    lat, lon = to_lat_lon(*point)
    lat_min, lat_max, lon_min, lon_max = POLAND_BOUNDS
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        raise UldkError(f"współrzędne ({lat:.4f}, {lon:.4f}) poza Polską – nieoczekiwany układ współrzędnych")
    return lat, lon


def wkt_srid(wkt: str) -> int | None:
    """Kod EPSG z prefiksu EWKT (``SRID=4326;POLYGON(...)``) lub ``None``, gdy go brak."""
    match = _SRID_RE.match(wkt or "")
    return int(match.group(1)) if match else None


def wkt_centroid(wkt: str) -> tuple[float, float] | None:
    """Centroid (ważony powierzchnią, z odjęciem otworów) geometrii POLYGON/MULTIPOLYGON jako ``(x, y)``."""
    polygons = _parse_polygons(wkt)
    if not polygons:
        return None
    origin = polygons[0][0][0]
    area_sum = x_sum = y_sum = 0.0
    points: list[tuple[float, float]] = []
    for rings in polygons:
        for ring_index, ring in enumerate(rings):
            points.extend(ring)
            area, cx, cy = _ring_centroid([(x - origin[0], y - origin[1]) for x, y in ring])
            weight = abs(area) * (1 if ring_index == 0 else -1)  # pierwszy pierścień = obrys, kolejne = otwory
            area_sum += weight
            x_sum += weight * cx
            y_sum += weight * cy
    if area_sum <= 0:
        if not points:
            return None
        return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)
    return origin[0] + x_sum / area_sum, origin[1] + y_sum / area_sum


def extent_center(extent: str) -> tuple[float, float] | None:
    """Środek prostokąta ``minx,miny,maxx,maxy`` jako ``(x, y)``."""
    try:
        min_x, min_y, max_x, max_y = (float(v) for v in extent.split(","))
    except (ValueError, AttributeError):
        return None
    return (min_x + max_x) / 2, (min_y + max_y) / 2


def to_lat_lon(x: float, y: float) -> tuple[float, float]:
    """Zwraca ``(lat, lon)``, rozpoznając kolejność osi po zakresach współrzędnych Polski.

    Szerokość geograficzna Polski to ~49–55°, długość ~14–24,2° – zakresy się nie nakładają.
    """
    if 48.5 <= x <= 55.5 and 13.5 <= y <= 24.5:
        return x, y
    return y, x


def google_maps_url(lat: float, lon: float) -> str:
    """Link „szukaj punktu” w Google Maps."""
    return GOOGLE_MAPS_URL.format(lat=lat, lon=lon)


def geoportal_parcel_url(parcel_id: str) -> str:
    """Link do Geoportalu z podświetleniem działki (parametr ``identifyParcel``)."""
    return GEOPORTAL_PARCEL_URL.format(parcel_id=quote(parcel_id, safe="/"))


def _parse_polygons(wkt: str) -> list[list[list[tuple[float, float]]]]:
    body = (wkt or "").split(";", 1)[-1].strip()
    kind = body.split("(", 1)[0].strip().upper()
    if kind not in {"POLYGON", "MULTIPOLYGON"} or "(" not in body:
        return []
    inner = body[body.index("(") + 1: body.rindex(")")]
    polygon_texts = _POLYGON_SPLIT_RE.split(inner) if kind == "MULTIPOLYGON" else [inner]
    polygons = []
    for polygon_text in polygon_texts:
        rings = [
            [(float(x), float(y)) for x, y in _POINT_RE.findall(ring_text)]
            for ring_text in _RING_SPLIT_RE.split(polygon_text)
        ]
        rings = [ring for ring in rings if len(ring) >= 3]
        if rings:
            polygons.append(rings)
    return polygons


def _ring_centroid(ring: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Pole ze znakiem i centroid pierścienia (wzór Gaussa / „shoelace”)."""
    area = cx = cy = 0.0
    closed = ring if ring[0] == ring[-1] else ring + ring[:1]
    for (x0, y0), (x1, y1) in zip(closed, closed[1:]):
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    area /= 2
    if area == 0:
        return 0.0, sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)
    return area, cx / (6 * area), cy / (6 * area)
