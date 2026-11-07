"""Validated, read-only package scope and aggregate counts; independent of HTTP."""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from .models import LEAD_CATEGORIES, Investment
from .package_sql import PARCEL_SQL as _PARCEL
from .package_sql import SCOPE_SQL as _SCOPE
from .stages import BIG_BUILDING_FACTOR, DAYS_PER_MONTH, get_trade
from .storage import investment_from_row
from .teryt import VOIVODESHIPS


class PackageFilterError(ValueError):
    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


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
            raise PackageFilterError("wojewodztwo", "Wybierz województwo z listy")
        if not isinstance(self.branza, str) or get_trade(self.branza) is None:
            raise PackageFilterError("branza", "Wybierz branżę z listy")
        if (not isinstance(self.powiaty, tuple) or len(self.powiaty) > 20
                or any(not isinstance(code, str) or len(code) != 4 or not code.isascii()
                       or not code.isdigit() or not code.startswith(self.wojewodztwo) for code in self.powiaty)
                or len(set(self.powiaty)) != len(self.powiaty)):
            raise PackageFilterError("powiaty", "Wybierz do 20 różnych powiatów należących do województwa")
        if (not isinstance(self.kategorie, tuple) or not 1 <= len(self.kategorie) <= len(LEAD_CATEGORIES)
                or any(item not in LEAD_CATEGORIES for item in self.kategorie)
                or len(set(self.kategorie)) != len(self.kategorie)):
            raise PackageFilterError("kategorie", "Wybierz co najmniej jeden typ inwestycji z listy")
        coordinates = (self.lat, self.lon, self.promien_km)
        if any(value is not None for value in coordinates):
            if any(type(value) not in (int, float) or not math.isfinite(value) for value in coordinates):
                raise PackageFilterError("coordinates", "Promień wymaga dwóch współrzędnych i skończonej odległości")
            if not (49 <= self.lat <= 55.1 and 14 <= self.lon <= 24.5 and 1 <= self.promien_km <= 200):
                raise PackageFilterError("coordinates", "Baza: WGS84 w Polsce (49–55,1 N, 14–24,5 E), promień 1–200 km")


@dataclass(frozen=True)
class PackageEstimate:
    count: int
    with_parcel_coordinates: int
    without_parcel_coordinates: int
    last_seen: str | None


class PackageTooLarge(ValueError):
    """A requested snapshot exceeds the bounded preparation size."""


def _radius_check(base_lat: float, base_lon: float, radius: float):
    # The engine's haversine formula is monotonic in its `a` term for these
    # distances. Compare a <= sin(radius / (2*R))**2 directly: no per-row sqrt,
    # atan2 or repeated conversion of the constant base/radius is needed.
    rad = math.pi / 180
    half_rad = rad / 2
    cos_base = math.cos(base_lat * rad)
    threshold = math.sin((radius + 1e-9) / (2 * 6371.0)) ** 2
    sin, cos = math.sin, math.cos

    def within(lat: float, lon: float) -> bool:
        # Called only inside SQL CASE after PARCEL_SQL has proved that both
        # coordinates are numeric and in range (CASE evaluation is lazy).
        a_lat = sin((lat - base_lat) * half_rad)
        a_lon = sin((lon - base_lon) * half_rad)
        return a_lat * a_lat + cos_base * cos(lat * rad) * a_lon * a_lon <= threshold

    return within


def _package_scope(conn: sqlite3.Connection, filters: PackageFilters, today: date) -> tuple[str, list[object]]:
    """One scope for estimation and paid-package selection."""
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
        # A meridian segment followed by a parallel segment is an upper bound
        # on great-circle distance: R*(abs(dlat)+abs(dlon)) in radians. Points
        # inside this conservative diamond need no trigonometry. The remainder
        # still uses the engine's exact spherical distance (including corners).
        conditions.extend(("lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                           (f"CASE WHEN ({_PARCEL}) THEN CASE "
                           "WHEN abs(lat - ?) + abs(lon - ?) <= ? THEN 1 "
                           "ELSE package_within_radius(lat, lon) END ELSE 0 END")))
        params.extend((filters.lat - delta_lat, filters.lat + delta_lat,
                       filters.lon - delta_lon, filters.lon + delta_lon,
                       filters.lat, filters.lon, math.degrees(filters.promien_km / 6371.0)))
        conn.create_function("package_within_radius", 2,
                             _radius_check(filters.lat, filters.lon, filters.promien_km), deterministic=True)
    return ' AND '.join(conditions), params


def estimate_package(conn: sqlite3.Connection, filters: PackageFilters, *, today: date) -> PackageEstimate:
    """Aggregate stored matches; caller owns read isolation/budget/replacement guard."""
    where, params = _package_scope(conn, filters, today)
    # Every radius match has already passed parcel quality in WHERE.
    located_sql = "COUNT(*)" if filters.promien_km is not None else f"coalesce(SUM({_PARCEL}), 0)"
    sql = f"""SELECT COUNT(*), {located_sql},
        MAX(ostatnio_widziany) FROM investments INDEXED BY ix_investments_package
        WHERE {where}"""
    count, located, last_seen = conn.execute(sql, params).fetchone()
    return PackageEstimate(count, located, count - located, last_seen)


def select_package(conn: sqlite3.Connection, filters: PackageFilters, *, today: date, limit: int = 10_000) -> tuple[Investment, ...]:
    """Bounded snapshot using exactly the estimator's geography/category/timing."""
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("limit must be 1..10000")
    where, params = _package_scope(conn, filters, today)
    rows = conn.execute(f"""SELECT * FROM investments INDEXED BY ix_investments_package
        WHERE {where} LIMIT ?""", (*params,limit+1)).fetchall()
    if len(rows) > limit:
        raise PackageTooLarge("package limit exceeded")
    return tuple(investment_from_row(row) for row in rows)


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
