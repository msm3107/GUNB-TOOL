"""Warstwa geokodowania (ULDK): ponowienia z backoffem, bezpiecznik, walidacja geometrii i układu EPSG."""

from __future__ import annotations

import logging

import pytest
import responses

from gunb_tool.geocoding_uldk import (
    GeoPrecision,
    MemoryGeocodeCache,
    UldkClient,
    UldkError,
    UldkGeocoder,
    parcel_point,
)
from gunb_tool.http_client import HttpError
from gunb_tool.models import Parcel
from tests.qa.conftest import ULDK_URL, slept

PARCEL = Parcel("160705_4", "0005", "13")
WKT_4326 = "SRID=4326;POLYGON((17.330 50.470,17.332 50.470,17.332 50.472,17.330 50.472,17.330 50.470))"
EXTENT_4326 = "17.330,50.470,17.332,50.472"
WKT_2180 = ("SRID=2180;POLYGON((636949.9 487118.5,636951.4 487114.8,636878.9 487085.9,"
            "636949.9 487118.5))")
REGION_BODY = "0\n160705_4.0005|Nysa|Nysa|powiat nyski|opolskie|17.30,50.45,17.36,50.49\n"


def parcel_body(wkt: str = WKT_4326, extent: str = EXTENT_4326) -> str:
    return f"1\n160705_4.0005.13|opolskie|powiat nyski|Nysa|{extent}|{wkt}\n"


@pytest.fixture
def uldk(make_http) -> UldkClient:
    return UldkClient(make_http(), ULDK_URL)


def geocoder(make_http, *, region_fallback: bool = False) -> UldkGeocoder:
    return UldkGeocoder(UldkClient(make_http(), ULDK_URL), MemoryGeocodeCache(), region_fallback=region_fallback)


# --- Ponowienia i backoff ------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_error_is_retried_three_times_with_exponential_backoff_then_raised(uldk, http, sleep, status):
    http.add(responses.GET, ULDK_URL, status=status, body="Internal Server Error")

    with pytest.raises(HttpError) as excinfo:
        uldk.find_parcels(PARCEL.uldk_id)

    assert len(http.calls) == 4  # 1 próba + 3 ponowienia – dopiero potem wyjątek
    assert excinfo.value.status_code == status
    waits = slept(sleep)
    assert len(waits) == 3
    assert 1 <= waits[0] <= 2 and 2 <= waits[1] <= 4 and 4 <= waits[2] <= 8  # 2 → 4 → 8 s (z jitterem)


def test_transient_errors_recover_without_raising(uldk, http, sleep):
    http.add(responses.GET, ULDK_URL, status=503)
    http.add(responses.GET, ULDK_URL, status=502)
    http.add(responses.GET, ULDK_URL, body=parcel_body())

    rows = uldk.find_parcels(PARCEL.uldk_id)

    assert rows[0]["id"] == "160705_4.0005.13"
    assert len(http.calls) == 3 and len(slept(sleep)) == 2


def test_client_error_is_not_retried(uldk, http, sleep):
    http.add(responses.GET, ULDK_URL, status=400, body="Bad request")
    with pytest.raises(UldkError, match="HTTP 400"):
        uldk.find_parcels(PARCEL.uldk_id)
    assert len(http.calls) == 1 and slept(sleep) == []


def test_request_asks_uldk_for_wgs84_coordinates(uldk, http):
    http.add(responses.GET, ULDK_URL, body=parcel_body())
    uldk.find_parcels(PARCEL.uldk_id)
    url = http.calls[0].request.url
    assert "srid=4326" in url and "request=GetParcelByIdOrNr" in url


def test_uldk_outage_leaves_lead_without_coordinates_instead_of_crashing(make_http, http, caplog):
    http.add(responses.GET, ULDK_URL, status=503)
    with caplog.at_level(logging.WARNING):
        assert geocoder(make_http).geocode([PARCEL]) is None
    assert "ULDK" in caplog.text


def test_breaker_stops_hammering_uldk_after_three_failed_requests(make_http, http):
    http.add(responses.GET, ULDK_URL, status=503)
    geo = geocoder(make_http)
    for _ in range(3):
        assert geo.geocode([PARCEL]) is None
    calls_after_outage = len(http.calls)
    assert calls_after_outage == 12  # 3 sprawy × (1 próba + 3 ponowienia)

    for _ in range(50):
        assert geo.geocode([PARCEL]) is None
    assert len(http.calls) == calls_after_outage  # kolejne sprawy nie czekają na martwy serwer


def test_html_error_page_with_status_200_is_handled(make_http, http):
    http.add(responses.GET, ULDK_URL, body="<html>Service Unavailable</html>")
    assert geocoder(make_http).geocode([PARCEL]) is None


# --- Geometria i układ współrzędnych ------------------------------------------------------------------

def test_valid_wgs84_parcel_gives_point_inside_the_parcel(make_http, http):
    http.add(responses.GET, ULDK_URL, body=parcel_body())
    result = geocoder(make_http).geocode([PARCEL])
    assert result.precision is GeoPrecision.PARCEL
    assert 50.470 <= result.lat <= 50.472 and 17.330 <= result.lon <= 17.332
    assert result.google_maps_url == f"https://www.google.com/maps?q={result.lat:.6f},{result.lon:.6f}"


def test_geometry_in_wrong_epsg_is_rejected_instead_of_saving_metres_as_degrees(make_http, http, caplog):
    http.add(responses.GET, ULDK_URL, body=parcel_body(wkt=WKT_2180, extent="636878.9,487085.9,636951.4,487118.5"))
    with caplog.at_level(logging.WARNING):
        assert geocoder(make_http).geocode([PARCEL]) is None
    assert "EPSG:2180" in caplog.text
    with pytest.raises(UldkError, match="EPSG:2180"):
        parcel_point({"geom_wkt": WKT_2180, "geom_extent": ""})


def test_coordinates_without_srid_outside_poland_are_rejected():
    metres = "POLYGON((636949.9 487118.5,636951.4 487114.8,636878.9 487085.9,636949.9 487118.5))"
    with pytest.raises(UldkError, match="poza Polską"):
        parcel_point({"geom_wkt": metres, "geom_extent": ""})


@pytest.mark.parametrize("wkt", ["SRID=4326;POLYGON EMPTY", "", "SRID=4326;POLYGON(())", "SRID=4326;MULTIPOLYGON EMPTY"])
def test_empty_polygon_gives_no_point(make_http, http, wkt):
    http.add(responses.GET, ULDK_URL, body=parcel_body(wkt=wkt, extent=""))
    assert parcel_point({"geom_wkt": wkt, "geom_extent": ""}) is None
    assert geocoder(make_http).geocode([PARCEL]) is None


def test_empty_parcel_geometry_falls_back_to_the_precinct_centre(make_http, http):
    http.add(responses.GET, ULDK_URL, body=parcel_body(wkt="SRID=4326;POLYGON EMPTY", extent=""))
    http.add(responses.GET, ULDK_URL, body=REGION_BODY)

    result = geocoder(make_http, region_fallback=True).geocode([PARCEL])

    assert result.precision is GeoPrecision.REGION
    assert 50.45 <= result.lat <= 50.49 and 17.30 <= result.lon <= 17.36
    assert "request=GetRegionById" in http.calls[-1].request.url
