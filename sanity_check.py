"""Test dymny na żywych usługach (bez atrap): ULDK → link Google Maps → SQLite w pamięci.

Użycie::

    python sanity_check.py                        # działka testowa: Pałac Kultury i Nauki
    python sanity_check.py 161106_5.0058.52/11    # dowolna inna działka (WWPPGG_R.OOOO[.AR_n].NR)

Kod wyjścia: 0 – wszystkie kontrole przeszły, 1 – co najmniej jedna nie przeszła, 2 – zły identyfikator.
"""

from __future__ import annotations

import re
import sys
import tempfile
import time
from pathlib import Path

from gunb_tool.config import HttpConfig
from gunb_tool.geocoding_uldk import POLAND_BOUNDS, WGS84_SRID, UldkClient, UldkError, UldkGeocoder, wkt_srid
from gunb_tool.http_client import HttpError, ResilientHttpClient
from gunb_tool.models import Investment, Parcel
from gunb_tool.storage import ChangeType, LeadRepository

TEST_PARCEL_ID = "146510_8.0309.24/35"  # Pałac Kultury i Nauki, Warszawa-Śródmieście
GOOGLE_MAPS_LINK = re.compile(r"^https://www\.google\.com/maps\?q=(-?\d+\.\d{6}),(-?\d+\.\d{6})$")


class Checks:
    """Licznik kontroli wypisujący wynik każdej z nich."""

    def __init__(self) -> None:
        self.failed = 0

    def check(self, ok: bool, message: str) -> bool:
        print(f"  {'✔' if ok else '✘'} {message}")
        self.failed += not ok
        return ok


def run(parcel: Parcel) -> int:
    checks = Checks()
    client = UldkClient(ResilientHttpClient(HttpConfig(timeout=30, max_retries=2, min_delay=0.0, max_delay=0.0)))

    print(f"[1/4] ULDK: GetParcelByIdOrNr id={parcel.uldk_id} srid={WGS84_SRID}")
    started = time.monotonic()
    rows = client.find_parcels(parcel.uldk_id)
    if not checks.check(bool(rows), f"ULDK zwrócił {len(rows)} wynik(i) w {time.monotonic() - started:.2f} s"):
        return 1
    row = rows[0]
    print(f"      id: {row['id']} | {row['commune']}, {row['county']}, woj. {row['voivodeship']}")
    print(f"      geom_wkt: {row['geom_wkt'][:72]}…")
    checks.check(wkt_srid(row["geom_wkt"]) == WGS84_SRID, f"geometria w układzie EPSG:{WGS84_SRID} (WGS84)")

    print("[2/4] Geokodowanie i link Google Maps")
    result = UldkGeocoder(client, region_fallback=False).geocode([parcel])
    if not checks.check(result is not None, "geokoder wyznaczył punkt (centroid działki)"):
        return 1
    lat_min, lat_max, lon_min, lon_max = POLAND_BOUNDS
    checks.check(lat_min <= result.lat <= lat_max and lon_min <= result.lon <= lon_max,
                 f"punkt w granicach Polski: lat={result.lat:.6f}, lon={result.lon:.6f}")
    link = GOOGLE_MAPS_LINK.match(result.google_maps_url)
    checks.check(bool(link), f"format https://www.google.com/maps?q={{lat}},{{lon}} → {result.google_maps_url}")
    if link:
        checks.check(abs(float(link[1]) - result.lat) < 1e-6 and abs(float(link[2]) - result.lon) < 1e-6,
                     "link wskazuje wyznaczony punkt (kolejność: lat, lon)")
    print(f"      Geoportal: {result.geoportal_url}")

    print("[3/4] SQLite (:memory:): zapis i odczyt przykładowego leada")
    with LeadRepository(":memory:") as repo:
        lead = Investment(
            id_sprawy="SANITY/1/2026",
            zrodlo="pozwolenia",
            status="decyzja",
            kategoria="komercyjna",
            nazwa_zamierzenia="Rekord testowy sanity_check",
            teryt_dzialki=result.parcel_id,
            dzialki=[result.parcel_id],
            lat=result.lat,
            lon=result.lon,
            precyzja_geo=result.precision.value,
            google_maps_url=result.google_maps_url,
            geoportal_url=result.geoportal_url,
            gmina=result.commune,
            powiat=result.county,
        )
        first = repo.upsert(lead)
        stored = repo.get(lead.id_sprawy)
        second = repo.upsert(lead)
        checks.check(first.change is ChangeType.NEW, f"pierwszy zapis: {first.change.value}")
        checks.check(
            stored is not None and (stored.lat, stored.lon, stored.google_maps_url) == (lead.lat, lead.lon, lead.google_maps_url),
            "odczyt zgodny z zapisem (lat, lon, google_maps_url)",
        )
        checks.check(second.change is ChangeType.UNCHANGED, f"ponowny zapis tego samego rekordu: {second.change.value}")
        print(f"      rekord: {stored.id_sprawy} | {stored.status} | {stored.teryt_dzialki} | "
              f"{stored.lat:.6f}, {stored.lon:.6f} | czy_wyslano={int(stored.czy_wyslano)}")

    print("[4/4] SQLite (plik tymczasowy): tryb WAL")
    with tempfile.TemporaryDirectory() as tmp, LeadRepository(Path(tmp) / "wal.sqlite") as repo:
        repo.upsert(lead)
        checks.check(repo.journal_mode == "wal", f"PRAGMA journal_mode = {repo.journal_mode}")
        checks.check(repo.busy_timeout_ms >= 1000, f"PRAGMA busy_timeout = {repo.busy_timeout_ms} ms")

    print(f"\nWynik: {'OK – wszystkie kontrole przeszły' if not checks.failed else f'BŁĘDY: {checks.failed}'}")
    return 0 if not checks.failed else 1


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    try:
        parcel = Parcel.from_id(argv[1] if len(argv) > 1 else TEST_PARCEL_ID)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        return run(parcel)
    except (HttpError, UldkError) as exc:
        print(f"  ✘ błąd połączenia z ULDK: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
