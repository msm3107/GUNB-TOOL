"""Synthetic official-WFS-shaped records only; no network or production DB."""

import hashlib
import sqlite3
from datetime import date
from html import escape

import pytest

from core.places import PlaceCatalog, PlaceUnavailable, import_catalog, main

DAY = date(2026, 11, 7)


def gml(rows=None, *, crs="urn:ogc:def:crs:EPSG::4326", extra=""):
    rows = (
        rows
        if rows is not None
        else [
            ("1", "Łódź", "Gmina A", "Powiat A", "1001011", "51.77 19.45"),
            ("2", "Łódź", "Gmina B", "Powiat B", "2814011", "53.0 20.0"),
        ]
    )
    members = []
    for ident, name, municipality, county, code, pos in rows:
        values = {
            "IDENTYFIKATORPRNG": ident,
            "NAZWAGLOWNA": name,
            "GMINA": municipality,
            "POWIAT": county,
            "IDENTYFIKATORGMINY": code,
        }
        attributes = "".join(f"<ms:{k}>{escape(v)}</ms:{k}>" for k, v in values.items())
        members.append(
            f"<wfs:member><ms:M1_UrzedoweNazwyMiejscowosci>{attributes}"
            f'<ms:msGeometry><gml:Point srsName="{crs}"><gml:pos>{pos}</gml:pos>'
            "</gml:Point></ms:msGeometry></ms:M1_UrzedoweNazwyMiejscowosci></wfs:member>"
        )
    return (
        f'<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" '
        f'xmlns:ms="http://mapserver.gis.umn.edu/mapserver" xmlns:gml="http://www.opengis.net/gml/3.2" '
        f'numberReturned="{len(rows)}" numberMatched="{len(rows)}" {extra}>'
        + "".join(members)
        + "</wfs:FeatureCollection>"
    )


@pytest.fixture
def catalog(tmp_path):
    source, output = tmp_path / "synthetic.gml", tmp_path / "places.sqlite"
    source.write_text(gml(), encoding="utf-8")
    version = import_catalog(source, output, source_date=DAY)
    return source, output, version


def test_catalog_disambiguates_polish_names_and_versions(catalog):
    source, output, version = catalog
    reader = PlaceCatalog(output)
    places = reader.search("LODZ")
    assert [p.id for p in places] == ["1", "2"]
    assert places[0].municipality != places[1].municipality
    assert reader.search("Łó", "28") == (places[1],)
    assert reader.get("2", version) == places[1]
    assert reader.get("2", "old") is None
    assert reader.get("missing", version) is None
    assert version == f"{DAY}:{hashlib.sha256(source.read_bytes()).hexdigest()}"
    assert (places[1].lat, places[1].lon, places[1].region) == (53, 20, "28")
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT source_date,sha256 FROM metadata").fetchone() == (
            str(DAY),
            hashlib.sha256(source.read_bytes()).hexdigest(),
        )
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM places WHERE normalized_name>=? AND normalized_name<? ORDER BY normalized_name,id LIMIT 20",
            ("lo", "loz"),
        ).fetchall()
        assert "INDEX" in str(plan)
    before = output.read_bytes()
    assert reader.search("nieistniejąca") == ()
    assert output.read_bytes() == before


@pytest.mark.parametrize(
    "query,region,limit",
    [("a", None, 20), ("a" * 81, None, 20), ("ab", "99", 20), ("ab", None, 21)],
)
def test_catalog_rejects_unbounded_inputs(catalog, query, region, limit):
    with pytest.raises(ValueError):
        PlaceCatalog(catalog[1]).search(query, region, limit)


def test_catalog_unavailable_is_not_no_matches(tmp_path):
    for path in (None, tmp_path / "missing.sqlite"):
        with pytest.raises(PlaceUnavailable):
            PlaceCatalog(path).search("Łódź")
    assert not (tmp_path / "missing.sqlite").exists()


@pytest.mark.parametrize(
    "content",
    [
        gml(crs="urn:ogc:def:crs:EPSG::2180"),
        gml([("1", "Test", "Gmina", "Powiat", "2814011", "20 53")]),
        gml([("1", "Test", "Gmina", "Powiat", "2814011", "nan 20")]),
        gml([("1", "Test", "Gmina", "Powiat", "9914011", "53 20")]),
        gml([("1", "A", "G", "P", "2814011", "53 20")] * 2),
        gml([]),
        gml(extra='next="https://example.test/next"'),
        gml().replace('numberReturned="2"', 'numberReturned="3"'),
        '<!DOCTYPE x [<!ENTITY y "boom">]>' + gml(),
        "<not-xml",
    ],
)
def test_invalid_import_keeps_previous_catalog(catalog, content):
    source, output, version = catalog
    before = output.read_bytes()
    source.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        import_catalog(source, output, source_date=DAY)
    assert output.read_bytes() == before
    assert PlaceCatalog(output).get("1", version)


def test_import_does_not_replace_unrelated_database(tmp_path):
    source, output = tmp_path / "source.gml", tmp_path / "customers.sqlite"
    source.write_text(gml(), encoding="utf-8")
    with sqlite3.connect(output) as conn:
        conn.execute("CREATE TABLE private(value TEXT)")
    before = output.read_bytes()
    with pytest.raises(ValueError):
        import_catalog(source, output, source_date=DAY)
    assert output.read_bytes() == before


def test_atomic_replacement_invalidates_old_place_version(catalog):
    source, output, old_version = catalog
    source.write_text(gml().replace("Gmina A", "Gmina C"), encoding="utf-8")
    version = import_catalog(source, output, source_date=DAY)
    assert version != old_version
    assert PlaceCatalog(output).get("1", old_version) is None
    assert PlaceCatalog(output).get("1", version).municipality == "Gmina C"


def test_catalog_cli_without_engine_config(tmp_path):
    source, output = tmp_path / "source.gml", tmp_path / "catalog.sqlite"
    source.write_text(gml(), encoding="utf-8")
    assert main([str(source), "--output", str(output), "--source-date", str(DAY)]) == 0
    assert len(PlaceCatalog(output).search("lodz")) == 2
