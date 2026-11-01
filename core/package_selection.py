"""Validated, read-only package scope and aggregate counts; independent of HTTP."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .models import LEAD_CATEGORIES
from .ranking import haversine_km
from .stages import BIG_BUILDING_FACTOR, DAYS_PER_MONTH, get_trade
from .teryt import VOIVODESHIPS


@dataclass(frozen=True)
class PackageFilters:
    wojewodztwo: str
    branza: str
    powiaty: tuple[str, ...] = ()
    kategorie: tuple[str, ...] = LEAD_CATEGORIES
    lat: float | None = None
    lon: float | None = None
    promien_km: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.wojewodztwo, str) or self.wojewodztwo not in {item.code for item in VOIVODESHIPS}:
            raise ValueError("Wybierz województwo z listy")
        if not isinstance(self.branza, str) or get_trade(self.branza) is None:
            raise ValueError("Wybierz branżę z listy")
        if (not isinstance(self.powiaty, tuple) or len(self.powiaty) > 20
                or any(not isinstance(code, str) or len(code) != 4 or not code.isascii()
                       or not code.isdigit() or not code.startswith(self.wojewodztwo) for code in self.powiaty)
                or len(set(self.powiaty)) != len(self.powiaty)):
            raise ValueError("Wybierz do 20 różnych powiatów należących do województwa")
        if (not isinstance(self.kategorie, tuple) or not 1 <= len(self.kategorie) <= len(LEAD_CATEGORIES)
                or any(item not in LEAD_CATEGORIES for item in self.kategorie)
                or len(set(self.kategorie)) != len(self.kategorie)):
            raise ValueError("Wybierz co najmniej jeden typ inwestycji z listy")
        coordinates = (self.lat, self.lon, self.promien_km)
        if any(value is not None for value in coordinates):
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in coordinates):
                raise ValueError("Promień wymaga dwóch współrzędnych i skończonej odległości")
            if not (49 <= self.lat <= 55.1 and 14 <= self.lon <= 24.5 and 1 <= self.promien_km <= 200):
                raise ValueError("Baza: WGS84 w Polsce (49–55,1 N, 14–24,5 E), promień 1–200 km")


@dataclass(frozen=True)
class PackageEstimate:
    count: int
    with_parcel_coordinates: int
    without_parcel_coordinates: int
    last_seen: str | None


# NULL-safe equivalent of the known-municipality mismatch rule in models.location_mismatch.
_UNIT = "substr(coalesce(teryt_dzialki, ''), 1, 6)"
_PARCEL = f"""precyzja_geo = 'dzialka'
    AND typeof(lat) IN ('real', 'integer') AND lat BETWEEN -90 AND 90
    AND typeof(lon) IN ('real', 'integer') AND lon BETWEEN -180 AND 180
    AND NOT (coalesce(gmina_teryt, '') <> '' AND length({_UNIT}) = 6
        AND {_UNIT} NOT GLOB '*[^0-9]*' AND substr(gmina_teryt, 1, 6) <> {_UNIT})"""
_SCOPE = """is_noise = 0 AND status IN ('wniosek', 'decyzja', 'zgloszenie', 'brak_sprzeciwu')
    AND powiat_teryt GLOB '[0-9][0-9][0-9][0-9]'"""


def _distance(lat: object, lon: object, base_lat: float, base_lon: float) -> float:
    if type(lat) not in (float, int) or type(lon) not in (float, int):
        return math.inf
    if not (math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        return math.inf
    return haversine_km(base_lat, base_lon, lat, lon)


def estimate_package(conn: sqlite3.Connection, filters: PackageFilters, *, today: date) -> PackageEstimate:
    """Count stored matching cases, never delivery novelty or customer access.

    Uses the existing TERYT index to bound reads to one region. SQL aggregates only;
    radius refines a conservative box with the engine's spherical distance formula.
    The caller owns connection isolation, read budget and replacement protection.
    """
    conditions = [_SCOPE, "powiat_teryt >= ? AND powiat_teryt < ?"]
    params: list[object] = [filters.wojewodztwo, f"{int(filters.wojewodztwo) + 1:02d}"]
    if filters.powiaty:
        conditions.append("powiat_teryt IN (" + ",".join("?" for _ in filters.powiaty) + ")")
        params.extend(filters.powiaty)
    conditions.append("kategoria IN (" + ",".join("?" for _ in filters.kategorie) + ")")
    params.extend(filters.kategorie)
    trade = get_trade(filters.branza)
    if trade.months is not None:
        field = "substr(coalesce(nullif(data_decyzji, ''), data_wplywu), 1, 10)"
        windows = []
        for factor in (1.0, BIG_BUILDING_FACTOR):
            low, high = (round(months * factor * DAYS_PER_MONTH) for months in trade.months)
            windows.append(((today - timedelta(days=high)).isoformat(), (today - timedelta(days=low)).isoformat()))
        params.extend((windows[0][0], windows[1][0], windows[0][1], windows[1][1]))
        single = "kategoria = 'mieszkaniowa-jednorodzinna'"
        # Cheap indexed-scope/date-range checks run before calendar validation.
        conditions.extend((f"{field} BETWEEN CASE WHEN {single} THEN ? ELSE ? END AND CASE WHEN {single} THEN ? ELSE ? END",
                           f"date({field}, '+0 days') = {field}"))
    if filters.promien_km is not None:
        delta_lat = filters.promien_km / 110.5
        delta_lon = filters.promien_km / (111 * math.cos(math.radians(abs(filters.lat) + delta_lat)))
        conditions.extend((f"({_PARCEL})", "lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                           "package_distance(lat, lon, ?, ?) <= ?"))
        params.extend((filters.lat - delta_lat, filters.lat + delta_lat,
                       filters.lon - delta_lon, filters.lon + delta_lon,
                       filters.lat, filters.lon, filters.promien_km + 1e-9))
        conn.create_function("package_distance", 4, _distance, deterministic=True)
    sql = f"""SELECT COUNT(*), coalesce(SUM(CASE WHEN {_PARCEL} THEN 1 ELSE 0 END), 0),
        MAX(ostatnio_widziany) FROM investments INDEXED BY ix_investments_powiat
        WHERE {' AND '.join(conditions)}"""
    count, located, last_seen = conn.execute(sql, params).fetchone()
    return PackageEstimate(count, located, count - located, last_seen)


def list_counties(conn: sqlite3.Connection) -> tuple[tuple[str, str], ...]:
    """Bounded public labels from imported records, without addresses or case data."""
    # Scan only the small TERYT index for distinct codes, then fetch one label per
    # county. Aggregating MIN(powiat) would read every full investment row.
    rows = conn.execute("""WITH counties AS (
        SELECT DISTINCT powiat_teryt FROM investments INDEXED BY ix_investments_powiat
        WHERE powiat_teryt GLOB '[0-9][0-9][0-9][0-9]' ORDER BY powiat_teryt LIMIT 1000)
        SELECT powiat_teryt, (SELECT substr(i.powiat, 1, 100) FROM investments AS i
            WHERE i.powiat_teryt=counties.powiat_teryt LIMIT 1)
        FROM counties""").fetchall()
    return tuple((code, name or f"Powiat {code}") for code, name in rows if code[:2] in {v.code for v in VOIVODESHIPS})
