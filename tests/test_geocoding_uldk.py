import pytest
import requests

from gunb_tool.geocoding_uldk import (
    GeoPrecision,
    MemoryGeocodeCache,
    UldkClient,
    UldkError,
    UldkGeocoder,
    extent_center,
    geoportal_parcel_url,
    google_maps_url,
    parcel_point,
    parse_uldk_response,
    to_lat_lon,
    wkt_centroid,
    wkt_srid,
)
from gunb_tool.geocoding_uldk import GeocodeResult
from gunb_tool.models import Parcel
from tests.fakes import FakeResponse, make_client

SQUARE_WKT = "SRID=4326;POLYGON((17.000 51.000,17.002 51.000,17.002 51.002,17.000 51.002,17.000 51.000))"
PARCEL_FIELDS = "id,voivodeship,county,commune,geom_extent,geom_wkt"


def parcel_body(parcel_id: str = "160602_4.0038.178/11", wkt: str = SQUARE_WKT) -> str:
    return f"1\n{parcel_id}|opolskie|powiat namysłowski|Namysłów|17.000,51.000,17.002,51.002|{wkt}\n"


NOT_FOUND = "-1 brak wyników\nbłędny format odpowiedzi XML, usługa zwróciła odpowiedź\n"
REGION_BODY = "0\n160602_4.0038|Namysłów|Namysłów|powiat namysłowski|opolskie|17.60,51.00,17.80,51.10\n"


def geocoder(script, **kwargs):
    client, session, clock = make_client(script)
    params = dict(cache=MemoryGeocodeCache())
    params.update(kwargs)
    return UldkGeocoder(UldkClient(client, "https://uldk.test/"), **params), session


P1 = Parcel("160602_4", "0038", "178/11")
P2 = Parcel("160602_4", "0038", "178/12")


# --- Parsowanie odpowiedzi ---------------------------------------------------

def test_parse_response_maps_fields_for_each_result_row():
    rows = parse_uldk_response("1\nA|opolskie\nB|śląskie\n", ("id", "voivodeship"))
    assert rows == [{"id": "A", "voivodeship": "opolskie"}, {"id": "B", "voivodeship": "śląskie"}]


def test_parse_response_returns_empty_list_when_not_found():
    assert parse_uldk_response(NOT_FOUND, ("id",)) == []


def test_parse_response_rejects_unexpected_payload():
    with pytest.raises(UldkError):
        parse_uldk_response("<html>Service Unavailable</html>", ("id",))


# --- Geometria ---------------------------------------------------------------

def test_wkt_centroid_of_square():
    x, y = wkt_centroid(SQUARE_WKT)
    assert (x, y) == pytest.approx((17.001, 51.001))


def test_wkt_centroid_of_multipolygon_is_area_weighted():
    wkt = "MULTIPOLYGON(((0 0,2 0,2 2,0 2,0 0)),((10 0,11 0,11 1,10 1,10 0)))"
    x, y = wkt_centroid(wkt)
    assert x == pytest.approx((1 * 4 + 10.5 * 1) / 5)
    assert y == pytest.approx((1 * 4 + 0.5 * 1) / 5)


def test_wkt_centroid_subtracts_holes():
    wkt = "POLYGON((0 0,4 0,4 4,0 4,0 0),(0 0,2 0,2 2,0 2,0 0))"
    x, y = wkt_centroid(wkt)
    assert (x, y) == pytest.approx(((2 * 16 - 1 * 4) / 12, (2 * 16 - 1 * 4) / 12))


def test_wkt_centroid_returns_none_for_unsupported_geometry():
    assert wkt_centroid("POINT(17 51)") is None
    assert wkt_centroid("") is None


def test_extent_center():
    assert extent_center("17.0,51.0,18.0,52.0") == pytest.approx((17.5, 51.5))
    assert extent_center("zepsute") is None


@pytest.mark.parametrize("x,y", [(17.7, 51.07), (51.07, 17.7)])
def test_axis_order_is_detected_from_polish_coordinate_ranges(x, y):
    assert to_lat_lon(x, y) == (51.07, 17.7)


def test_link_builders():
    assert google_maps_url(51.1, 17.25) == "https://www.google.com/maps?q=51.100000,17.250000"
    assert geoportal_parcel_url("161106_5.0058.AR_1.52/11") == (
        "https://mapy.geoportal.gov.pl/imap/Imgp_2.html?identifyParcel=161106_5.0058.AR_1.52/11"
    )


# --- Geokodowanie spraw ------------------------------------------------------

def test_geocodes_first_parcel_with_centroid_admin_names_and_links():
    geo, session = geocoder([FakeResponse(200, parcel_body())])

    result = geo.geocode([P1])

    assert result.precision is GeoPrecision.PARCEL
    assert (result.lat, result.lon) == pytest.approx((51.001, 17.001))
    assert result.parcel_id == "160602_4.0038.178/11"
    assert (result.county, result.commune) == ("powiat namysłowski", "Namysłów")
    assert result.google_maps_url == "https://www.google.com/maps?q=51.001000,17.001000"
    assert result.geoportal_url.endswith("identifyParcel=160602_4.0038.178/11")
    params = session.calls[0].params
    assert params["request"] == "GetParcelByIdOrNr"
    assert params["id"] == "160602_4.0038.178/11"
    assert params["srid"] == "4326"
    assert params["result"] == PARCEL_FIELDS


def test_tries_next_parcel_when_first_one_no_longer_exists():
    geo, session = geocoder([FakeResponse(200, NOT_FOUND), FakeResponse(200, parcel_body("160602_4.0038.178/12"))])
    result = geo.geocode([P1, P2])
    assert result.parcel_id == "160602_4.0038.178/12"
    assert len(session.calls) == 2


def test_falls_back_to_region_center_when_no_parcel_is_found():
    geo, session = geocoder([FakeResponse(200, NOT_FOUND), FakeResponse(200, REGION_BODY)])

    result = geo.geocode([P1])

    assert result.precision is GeoPrecision.REGION
    assert (result.lat, result.lon) == pytest.approx((51.05, 17.70))
    assert result.region_id == "160602_4.0038"
    assert result.geoportal_url is None
    assert session.calls[1].params["request"] == "GetRegionById"
    assert session.calls[1].params["id"] == "160602_4.0038"


def test_returns_none_without_region_fallback():
    geo, _ = geocoder([FakeResponse(200, NOT_FOUND)], region_fallback=False)
    assert geo.geocode([P1]) is None


def test_respects_max_parcels_per_case():
    geo, session = geocoder([FakeResponse(200, NOT_FOUND)], max_parcels=1, region_fallback=False)
    assert geo.geocode([P1, P2]) is None
    assert len(session.calls) == 1


def test_prefers_result_on_the_map_sheet_from_registry():
    body = (
        "2\n"
        f"160602_4.0038.AR_1.5|opolskie|powiat namysłowski|Namysłów|17.0,51.0,17.002,51.002|{SQUARE_WKT}\n"
        "160602_4.0038.AR_2.5|opolskie|powiat namysłowski|Namysłów|18.0,52.0,18.002,52.002|"
        "SRID=4326;POLYGON((18 52,18.002 52,18.002 52.002,18 52.002,18 52))\n"
    )
    geo, _ = geocoder([FakeResponse(200, body)])
    result = geo.geocode([Parcel("160602_4", "0038", "5", "2")])
    assert result.parcel_id == "160602_4.0038.AR_2.5"
    assert result.lat == pytest.approx(52.001)


def test_cache_prevents_repeated_queries_for_found_and_missing_parcels():
    cache = MemoryGeocodeCache()
    geo, session = geocoder([FakeResponse(200, parcel_body()), FakeResponse(200, NOT_FOUND)],
                            cache=cache, region_fallback=False)
    first = geo.geocode([P1])
    assert geo.geocode([P2]) is None
    assert geo.geocode([P1]) == first
    assert geo.geocode([P2]) is None
    assert len(session.calls) == 2


def test_network_failures_are_not_cached_and_trip_circuit_breaker():
    failures = [requests.ConnectionError("down")] * 4  # make_client: max_retries=3 -> 4 próby na zapytanie
    geo, session = geocoder(failures * 3, region_fallback=False, failure_threshold=2)

    assert geo.geocode([P1]) is None
    assert geo.geocode([P1]) is None  # błąd nie trafił do cache – ponowna próba
    assert geo.disabled
    calls_before = len(session.calls)
    assert geo.geocode([P2]) is None
    assert len(session.calls) == calls_before  # po zadziałaniu bezpiecznika brak zapytań


# --- Układ współrzędnych (dane w formatach zwracanych przez ULDK, bez atrap) ----------

# Fragmenty prawdziwej odpowiedzi ULDK dla działki 146510_8.0309.24/35 (PKiN) bez i z srid=4326.
PKIN_WKT_2180 = ("SRID=2180;POLYGON((636949.912510792 487118.535496397,636951.39488307 487114.826462336,"
                 "636878.994335615 487085.898157001,636949.912510792 487118.535496397))")
PKIN_EXTENT_2180 = "636821.749569414,486765.834999748,637172.485849584,487146.4539283"
PKIN_EXTENT_4326 = "21.004071992527,52.2298917920906,21.0091601832588,52.2332887897828"


def test_wkt_srid_reads_ewkt_prefix():
    assert wkt_srid(SQUARE_WKT) == 4326
    assert wkt_srid(PKIN_WKT_2180) == 2180
    assert wkt_srid("POLYGON((0 0,1 0,1 1,0 0))") is None


def test_parcel_point_returns_lat_lon_for_wgs84_geometry():
    assert parcel_point({"geom_wkt": SQUARE_WKT, "geom_extent": ""}) == pytest.approx((51.001, 17.001))


def test_parcel_point_falls_back_to_extent():
    lat, lon = parcel_point({"geom_wkt": "", "geom_extent": PKIN_EXTENT_4326})
    assert (lat, lon) == pytest.approx((52.23159, 21.00662), abs=1e-5)


def test_parcel_point_rejects_geometry_in_puwg_1992():
    with pytest.raises(UldkError, match="2180"):
        parcel_point({"geom_wkt": PKIN_WKT_2180, "geom_extent": PKIN_EXTENT_2180})


def test_parcel_point_rejects_coordinates_outside_poland():
    with pytest.raises(UldkError, match="poza Polską"):
        parcel_point({"geom_wkt": "", "geom_extent": PKIN_EXTENT_2180})


def test_parcel_point_without_geometry_is_none():
    assert parcel_point({"geom_wkt": "", "geom_extent": ""}) is None


def test_cached_result_gets_current_google_maps_link_format():
    cached = {
        "lat": 52.2316, "lon": 21.0066, "precision": "dzialka", "parcel_id": "146510_8.0309.24/35",
        "region_id": "146510_8.0309", "voivodeship": "mazowieckie", "county": "powiat Warszawa",
        "commune": "Warszawa (miasto)", "geoportal_url": None,
        "google_maps_url": "https://www.google.com/maps/search/?api=1&query=52.231600,21.006600",
    }
    assert GeocodeResult.from_dict(cached).google_maps_url == "https://www.google.com/maps?q=52.231600,21.006600"
