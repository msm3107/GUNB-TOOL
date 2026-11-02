"""Repeatable synthetic-only benchmark: python -m tests.benchmark_package_estimator.

No production paths accepted. Every run creates and removes its own temporary WAL
database. Timings include the service's lock, new read-only connection and budget.
"""

import argparse
import cProfile
import json
import math
import statistics
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.estimator import Estimator
from core.package_selection import PackageFilters
from core.storage import LeadRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--unique", action="store_true", help="Use unique coordinates instead of 100 repeated centroids")
    parser.add_argument("--municipality", action="store_true", help="Include matching municipality/parcel identifiers")
    parser.add_argument("--ring", action="store_true", help="Stress exact radius checks with points 49.9/50.1 km from the base")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    if not (1 <= args.rows <= 1_000_000 and 1 <= args.runs <= 100):
        parser.error("rows: 1..1000000; runs: 1..100")
    today = datetime.now(ZoneInfo("Europe/Warsaw")).date()
    decision = (today - timedelta(days=153)).isoformat()
    stamp = "2026-11-01T00:00:00+00:00"
    with tempfile.TemporaryDirectory(prefix="zolta-estimate-benchmark-") as folder:
        path = Path(folder) / "synthetic.sqlite"
        with LeadRepository(path) as repo:
            def rows():
                for i in range(args.rows):
                    offset = i / args.rows / 10 if args.unique else (i % 100) / 1000
                    lat, lon = 53 + offset, 20 + offset
                    if args.ring:
                        phi = math.radians(53)
                        bearing = 2 * math.pi * i / args.rows
                        delta = (49.9 if i % 2 == 0 else 50.1) / 6371
                        lat = math.asin(math.sin(phi) * math.cos(delta) + math.cos(phi) * math.sin(delta) * math.cos(bearing))
                        lon = math.radians(20) + math.atan2(math.sin(bearing) * math.sin(delta) * math.cos(phi),
                                                          math.cos(delta) - math.sin(phi) * math.sin(lat))
                        lat, lon = math.degrees(lat), math.degrees(lon)
                    yield (f"SYNTHETIC-{i}", "pozwolenia", "decyzja", f"280{i % 5 + 1}",
                           "mieszkaniowa-jednorodzinna" if i % 2 == 0 else "komercyjna", decision,
                           lat, lon, "dzialka",
                           "2801011" if args.municipality else None,
                           "280101_1.0001.1" if args.municipality else None, stamp, stamp, stamp, stamp)

            with repo.connection:
                repo.connection.execute("BEGIN IMMEDIATE")
                repo.connection.executemany("""INSERT INTO investments
                    (id_sprawy,zrodlo,status,powiat_teryt,kategoria,data_decyzji,lat,lon,precyzja_geo,
                     gmina_teryt,teryt_dzialki,utworzono,zmieniono,status_zmieniony,ostatnio_widziany)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows())
        service = Estimator(path)
        cases = {
            "region": PackageFilters("28", "materialy"),
            "county": PackageFilters("28", "materialy", powiaty=("2801",)),
            "trade": PackageFilters("28", "dach"),
            "radius": PackageFilters("28", "materialy", lat=53, lon=20, promien_km=50),
        }
        report = []
        for name, filters in cases.items():
            samples = []
            for _ in range(args.runs):
                start = time.perf_counter()
                result = service.estimate(filters)
                samples.append((time.perf_counter() - start) * 1000)
            report.append({"case": name, "count": result.count, "located": result.with_parcel_coordinates,
                           "median_ms": round(statistics.median(samples), 2), "max_ms": round(max(samples), 2)})
        print(json.dumps({"rows": args.rows, "unique": args.unique, "ring": args.ring, "municipality": args.municipality,
                          "runs": args.runs, "results": report}, indent=2))
        if args.profile:
            # Profile instrumentation can exceed the normal 500 ms budget.
            profiler = cProfile.Profile()
            profiler.runcall(Estimator(path, query_seconds=5).estimate, cases["radius"])
            profiler.print_stats(sort="cumtime")


if __name__ == "__main__":
    main()
