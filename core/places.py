"""Offline PRNG WFS2/GML catalog; independent from HTTP and the customer DB."""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import re
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .instance import AlreadyRunning, instance_lock
from .teryt import VOIVODESHIPS
from .text import normalize_text

APP_ID = 0x5A54504C
MS = "{http://mapserver.gis.umn.edu/mapserver}"
GML = "{http://www.opengis.net/gml/3.2}"
WFS = "{http://www.opengis.net/wfs/2.0}"
CRS = "urn:ogc:def:crs:EPSG::4326"
REGIONS = {r.code for r in VOIVODESHIPS}
MAX_SOURCE_BYTES = 512 * 1024 * 1024


class PlaceUnavailable(RuntimeError):
    """Catalog absent, unsupported or unreadable; never an empty search result."""


@dataclass(frozen=True)
class Place:
    id: str
    name: str
    municipality: str
    county: str
    region: str
    lat: float
    lon: float
    source_version: str


class PlaceCatalog:
    def __init__(self, path: Path | None):
        self.path = path

    @contextmanager
    def _read(self):
        if self.path is None:
            raise PlaceUnavailable("Katalog miejscowości jest niedostępny")
        conn = None
        try:
            uri = self.path.absolute().as_uri() + "?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, timeout=0.1)
            if (
                conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID
                or conn.execute("PRAGMA user_version").fetchone()[0] != 1
            ):
                raise PlaceUnavailable("Nieobsługiwana wersja katalogu miejscowości")
            version = conn.execute("SELECT source_version FROM metadata").fetchone()[0]
            if not isinstance(version, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}:[0-9a-f]{64}", version
            ):
                raise PlaceUnavailable("Niepoprawne metadane katalogu")
            yield conn, version
        except (OSError, sqlite3.Error, IndexError, TypeError):
            raise PlaceUnavailable("Katalog miejscowości jest niedostępny") from None
        finally:
            if conn is not None:
                conn.close()

    def search(
        self, query: str, region: str | None = None, limit: int = 20
    ) -> tuple[Place, ...]:
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 80:
            raise ValueError("Wpisz od 2 do 80 znaków nazwy miejscowości")
        query = normalize_text(query)
        if not 2 <= len(query) <= 80 or (region is not None and region not in REGIONS):
            raise ValueError("Sprawdź nazwę i województwo")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Limit wyszukiwania wynosi od 1 do 20")
        sql = "SELECT id,name,municipality,county,region,lat,lon FROM places WHERE normalized_name>=? AND normalized_name<?"
        params = [query, query + "\U0010ffff"]
        if region is not None:
            sql += " AND region=?"
            params.append(region)
        sql += " ORDER BY normalized_name,id LIMIT ?"
        with self._read() as (conn, version):
            return tuple(
                Place(*row, version) for row in conn.execute(sql, (*params, limit))
            )

    def get(self, place_id: str, source_version: str) -> Place | None:
        if (
            not isinstance(place_id, str)
            or not 1 <= len(place_id) <= 100
            or not isinstance(source_version, str)
            or not 1 <= len(source_version) <= 128
        ):
            raise ValueError("Sprawdź identyfikator i wersję miejscowości")
        with self._read() as (conn, version):
            if version != source_version:
                return None
            row = conn.execute(
                "SELECT id,name,municipality,county,region,lat,lon FROM places WHERE id=?",
                (place_id,),
            ).fetchone()
            return Place(*row, version) if row else None


class _Source:
    """Hash exactly what the XML parser consumes; bound input and prohibit DTDs."""

    def __init__(self, stream):
        self.stream, self.digest, self.size, self.tail = (
            stream,
            hashlib.sha256(),
            0,
            b"",
        )

    def read(self, size=-1):
        chunk = self.stream.read(min(size, 65536) if size >= 0 else 65536)
        self.size += len(chunk)
        text = self.tail + chunk.upper()
        if (
            self.size > MAX_SOURCE_BYTES
            or b"\0" in chunk
            or b"<!DOCTYPE" in text
            or b"<!ENTITY" in text
        ):
            raise ValueError(
                "Za duży lub niebezpieczny plik GML; wymagany UTF-8 bez DTD"
            )
        self.tail = text[-16:]
        self.digest.update(chunk)
        return chunk


def _record(feature):
    if feature.tag != MS + "M1_UrzedoweNazwyMiejscowosci":
        raise ValueError("Wymagany eksport WFS2 M1_UrzedoweNazwyMiejscowosci")

    def text(key):
        values = feature.findall(MS + key)
        value = (values[0].text or "").strip() if len(values) == 1 else ""
        if not value or len(value) > 200 or any(ord(c) < 32 for c in value):
            raise ValueError("Brak lub niepoprawny atrybut PRNG: " + key)
        return value

    ident, name, municipality, county, code = (
        text(k)
        for k in (
            "IDENTYFIKATORPRNG",
            "NAZWAGLOWNA",
            "GMINA",
            "POWIAT",
            "IDENTYFIKATORGMINY",
        )
    )
    if (
        not re.fullmatch(r"[0-9]{1,20}", ident)
        or not re.fullmatch(r"[0-9]{7}", code)
        or code[:2] not in REGIONS
    ):
        raise ValueError("Niepoprawny identyfikator PRNG lub TERYT")
    points = feature.findall(".//" + GML + "Point")
    if len(points) != 1 or points[0].get("srsName") != CRS:
        raise ValueError(
            "Wymagany punkt EPSG4326 z jawną kolejnością latitude longitude"
        )
    positions = points[0].findall(GML + "pos")
    parts = (positions[0].text or "").split() if len(positions) == 1 else []
    if len(parts) != 2:
        raise ValueError("Punkt wymaga dwóch współrzędnych")
    lat, lon = map(float, parts)
    if (
        not math.isfinite(lat)
        or not math.isfinite(lon)
        or not (49 <= lat <= 55.1 and 14 <= lon <= 24.5)
    ):
        raise ValueError("Współrzędne poza Polską lub odwrócona kolejność osi")
    return ident, name, municipality, county, code[:2], lat, lon, normalize_text(name)


def import_catalog(source: Path, destination: Path, *, source_date: date) -> str:
    """Build privately, then atomically publish; a failed import preserves the old file."""
    if (
        type(source_date) is not date
        or destination.is_symlink()
        or source.resolve() == destination.resolve()
    ):
        raise ValueError(
            "Sprawdź datę i odrębne ścieżki źródła/katalogu; symlink wyjścia niedozwolony"
        )
    destination = destination.absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(
        prefix=".prng-", suffix=".sqlite", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(filename)
    conn = None
    try:
        conn = sqlite3.connect(temporary, isolation_level=None)
        conn.executescript(f"""PRAGMA application_id={APP_ID}; PRAGMA user_version=1;
            CREATE TABLE places(id TEXT PRIMARY KEY,name TEXT,municipality TEXT,county TEXT,region TEXT,lat REAL,lon REAL,normalized_name TEXT);
            CREATE TABLE metadata(source_date TEXT,sha256 TEXT,source_version TEXT);""")
        batch, count = [], 0

        def flush():
            if batch:
                conn.execute("BEGIN IMMEDIATE")
                conn.executemany("INSERT INTO places VALUES(?,?,?,?,?,?,?,?)", batch)
                conn.execute("COMMIT")
                batch.clear()

        with source.open("rb") as stream:
            reader = _Source(stream)
            parser = ET.iterparse(reader, events=("start", "end"))
            _, root = next(parser)
            if root.tag != WFS + "FeatureCollection" or root.get("next"):
                raise ValueError("Wymagany kompletny eksport WFS2 bez odsyłacza next")
            for event, node in parser:
                if event == "end" and node.tag == WFS + "member":
                    if len(node) != 1:
                        raise ValueError("Niepoprawny member GML")
                    batch.append(_record(node[0]))
                    count += 1
                    root.remove(node)
                    if len(batch) == 500:
                        flush()
            if not count or any(
                root.get(k, "unknown") not in ("unknown", str(count))
                for k in ("numberReturned", "numberMatched")
            ):
                raise ValueError("Pusty lub niekompletny eksport PRNG")
            flush()
            digest = reader.digest.hexdigest()
        version = f"{source_date}:{digest}"
        conn.execute(
            "INSERT INTO metadata VALUES(?,?,?)", (str(source_date), digest, version)
        )
        conn.executescript(
            "CREATE INDEX idx_places_name ON places(normalized_name,id); CREATE INDEX idx_places_region_name ON places(region,normalized_name,id);"
        )
        conn.close()
        conn = None
        with instance_lock(destination.with_name(destination.name + ".import.lock")):
            if destination.is_symlink():
                raise ValueError("Symlink katalogu wyjściowego jest niedozwolony")
            if destination.exists():
                try:
                    with PlaceCatalog(destination)._read():
                        pass
                except PlaceUnavailable:
                    raise ValueError("Nie nadpisano nieznanej bazy danych") from None
            os.replace(temporary, destination)
        return version
    except (ET.ParseError, StopIteration, sqlite3.Error) as exc:
        raise ValueError(
            "Niepoprawny eksport PRNG lub powtórzony identyfikator"
        ) from exc
    finally:
        if conn is not None:
            conn.close()
        temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Import lokalnego PRNG WFS2/GML EPSG4326; bez sieci i bazy klientów"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source-date", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    try:
        version = import_catalog(args.source, args.output, source_date=args.source_date)
    except (OSError, ValueError, AlreadyRunning) as exc:
        parser.exit(1, str(exc) + "\n")
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
