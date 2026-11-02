from dataclasses import replace
from datetime import date

import pytest

from core.models import Investment
from core.package_selection import PackageFilters, estimate_package, list_counties

TODAY = date(2026, 11, 1)


def add(repo, key, **values):
    fields = {"id_sprawy": key, "zrodlo": "pozwolenia", "status": "decyzja",
              "powiat_teryt": "2814", "powiat": "Powiat testowy", "kategoria": "mieszkaniowa-jednorodzinna",
              "data_decyzji": "2026-06-01", "nazwa_zamierzenia": "Fikcyjna budowa"}
    fields.update(values)
    repo.upsert(Investment(**fields))


def test_estimate_scopes_imported_cases_without_noise_or_negative_status(repo):
    add(repo, "A")
    add(repo, "B", kategoria="komercyjna", powiat_teryt="2801")
    add(repo, "other-region", powiat_teryt="1401")
    add(repo, "negative", status="odmowa")
    add(repo, "noise", is_noise=True)
    add(repo, "unknown", status="inny")
    base = PackageFilters("28", "materialy")
    result = estimate_package(repo.connection, base, today=TODAY)
    assert result.count == 2 and result.with_parcel_coordinates == 0
    assert result.without_parcel_coordinates == 2 and result.last_seen
    assert estimate_package(repo.connection, replace(base, powiaty=("2814",)), today=TODAY).count == 1
    assert estimate_package(repo.connection, replace(base, kategorie=("komercyjna",)), today=TODAY).count == 1
    assert list_counties(repo.connection) == (("1401", "Powiat testowy"), ("2801", "Powiat testowy"), ("2814", "Powiat testowy"))


def test_trade_filter_uses_estimated_windows_and_larger_building_factor(repo):
    add(repo, "house-now")
    add(repo, "house-too-new", data_decyzji="2026-10-01")
    add(repo, "large-now", kategoria="komercyjna", data_decyzji="2026-03-01")
    add(repo, "large-too-new", kategoria="komercyjna")
    add(repo, "application-fallback", data_decyzji=None, data_wplywu="2026-06-01")
    add(repo, "invalid-date", data_decyzji="2026-02-30")
    add(repo, "missing-date", data_decyzji=None)
    assert estimate_package(repo.connection, PackageFilters("28", "dach"), today=TODAY).count == 3
    assert estimate_package(repo.connection, PackageFilters("28", "materialy"), today=TODAY).count == 7


def test_radius_refines_box_and_excludes_municipality_or_mismatched_parcel(repo):
    parcel = {"lat": 53.0, "lon": 20.0, "precyzja_geo": "dzialka", "gmina_teryt": "2814042", "teryt_dzialki": "281404_2.0001.1"}
    add(repo, "near", **parcel)
    add(repo, "box-corner", **dict(parcel, lat=53.08, lon=20.13))
    add(repo, "far", **dict(parcel, lat=54.0))
    add(repo, "municipality", **dict(parcel, precyzja_geo="gmina"))
    add(repo, "mismatch", **dict(parcel, teryt_dzialki="280101_1.0001.1"))
    add(repo, "missing")
    result = estimate_package(repo.connection, PackageFilters("28", "materialy", lat=53, lon=20, promien_km=10), today=TODAY)
    assert result.count == result.with_parcel_coordinates == 1
    all_cases = estimate_package(repo.connection, PackageFilters("28", "materialy"), today=TODAY)
    assert all_cases.count == 6 and all_cases.with_parcel_coordinates == 3


def test_trade_window_includes_exact_boundaries_for_both_building_sizes(repo):
    for i, day in enumerate(("2026-05-02", "2026-07-02", "2026-05-01", "2026-07-03")):
        add(repo, f"house-{i}", data_decyzji=day)
    for i, day in enumerate(("2026-01-31", "2026-05-02", "2026-01-30", "2026-05-03")):
        add(repo, f"large-{i}", data_decyzji=day, kategoria="komercyjna")
    assert estimate_package(repo.connection, PackageFilters("28", "dach"), today=TODAY).count == 4


@pytest.mark.parametrize("change", (
    {"wojewodztwo": "99"}, {"wojewodztwo": "28' OR 1=1"}, {"branza": "anything"},
    {"powiaty": ("1401",)}, {"powiaty": ("2814", "2814")}, {"kategorie": ()},
    {"kategorie": ("szum",)}, {"lat": 53}, {"lat": 53, "lon": 20},
    {"lat": 53, "lon": 20, "promien_km": 0},
    {"lat": float("nan"), "lon": 20, "promien_km": 10},
    {"lat": True, "lon": 20, "promien_km": 10},
    {"lat": 89, "lon": 20, "promien_km": 10}, {"powiaty": ["2814"]},
))
def test_invalid_filters_fail_before_any_query(change):
    with pytest.raises(ValueError):
        PackageFilters(**dict({"wojewodztwo": "28", "branza": "materialy"}, **change))


def test_empty_matching_scope_is_an_actual_zero(repo):
    result = estimate_package(repo.connection, PackageFilters("28", "materialy"), today=TODAY)
    assert result.count == result.with_parcel_coordinates == result.without_parcel_coordinates == 0
    assert result.last_seen is None


def test_selection_uses_covering_package_index_and_does_not_write(repo):
    add(repo, "A")
    statements = []
    repo.connection.set_trace_callback(statements.append)
    estimate_package(repo.connection, PackageFilters("28", "materialy"), today=TODAY)
    query = next(sql for sql in statements if "SELECT COUNT" in sql)
    plan = repo.connection.execute("EXPLAIN QUERY PLAN " + query).fetchall()
    assert any("SEARCH" in row[3] and "ix_investments_package" in row[3] for row in plan)
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE", "BEGIN IMMEDIATE")) for sql in statements)


@pytest.mark.parametrize("base_lat,base_lon,radius", ((53, 20, 10), (49, 24.5, 1), (55.1, 14, 200)))
def test_radius_matches_spherical_reference_inside_outside_and_near_boundary(repo, base_lat, base_lon, radius):
    import math

    from core.ranking import haversine_km

    # Destination points on the sphere at radius +/- 1 millimetre and radius/2,
    # around the compass: includes diamond shortcuts and box-corner refinement.
    phi, lam = math.radians(base_lat), math.radians(base_lon)
    rows = []
    expected = 0
    for distance in (radius / 2, radius - 1e-6, radius, radius + 1e-6, radius * 1.2):
        for bearing in range(0, 360, 5):
            angle, delta = math.radians(bearing), distance / 6371
            lat = math.asin(math.sin(phi) * math.cos(delta) + math.cos(phi) * math.sin(delta) * math.cos(angle))
            lon = lam + math.atan2(math.sin(angle) * math.sin(delta) * math.cos(phi), math.cos(delta) - math.sin(phi) * math.sin(lat))
            lat, lon = math.degrees(lat), math.degrees(lon)
            rows.append((lat, lon))
            expected += haversine_km(base_lat, base_lon, lat, lon) <= radius + 1e-9
    assert expected == 216
    for i, (lat, lon) in enumerate(rows):
        add(repo, str(i), lat=lat, lon=lon, precyzja_geo="dzialka")
    result = estimate_package(repo.connection, PackageFilters("28", "materialy", lat=base_lat, lon=base_lon, promien_km=radius), today=TODAY)
    assert result.count == result.with_parcel_coordinates == expected


def test_radius_never_passes_invalid_geometry_to_spherical_math(repo):
    for i, (lat, lon) in enumerate(((None, 20), (53, None), ("bad", 20), (53, "bad"),
                                  (float("inf"), 20), (53, float("inf")), (91, 20), (53, 181))):
        add(repo, str(i), lat=lat, lon=lon, precyzja_geo="dzialka")
    result = estimate_package(repo.connection, PackageFilters("28", "materialy", lat=53, lon=20, promien_km=50), today=TODAY)
    assert result.count == result.with_parcel_coordinates == 0


def test_county_metadata_scans_covering_codes_and_uses_bounded_label_lookups(repo):
    add(repo, "A", powiat=None)
    add(repo, "B", powiat_teryt="1401", powiat="Powiat inny")
    statements = []
    repo.connection.set_trace_callback(statements.append)
    assert list_counties(repo.connection) == (("1401", "Powiat inny"), ("2814", "Powiat 2814"))
    query = next(sql for sql in statements if "WITH counties" in sql)
    plans = [row[3] for row in repo.connection.execute("EXPLAIN QUERY PLAN " + query)]
    assert any("COVERING INDEX ix_investments_powiat" in item for item in plans)
    assert any("SEARCH i USING INDEX ix_investments_powiat" in item for item in plans)
