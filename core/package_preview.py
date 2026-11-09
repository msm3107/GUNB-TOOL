"""Bounded immutable CSV projection; no live investment joins and no web imports."""

from __future__ import annotations

import csv
import math
import re
import threading
import time
from dataclasses import dataclass
from io import TextIOWrapper
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .export_package import EXPORT_COLUMNS, _protect_csv_value
from .package_access import DownloadBusy, PackageAccessStore, PackageNotFound
from .package_files import open_verified

PAGE_SIZE = 50
MAX_ROWS = 10000
CATEGORIES = ("new", "change", "full")


class PreviewUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class PreviewRow:
    case_id: str
    category: str
    values: dict[str, str]
    google_maps: str | None
    geoportal: str | None
    truncated: bool


@dataclass(frozen=True)
class PreviewPage:
    rows: tuple[PreviewRow, ...]
    selected: PreviewRow | None
    counts: dict[str, int]
    total: int
    page: int
    pages: int
    category: str
    artifact_sha256: str


def map_links(values: dict[str, str]) -> tuple[str | None, str | None]:
    def parsed(name):
        value = values[name]
        if (
            not value
            or not value.isascii()
            or any(ord(c) < 33 or ord(c) == 127 or c == "\\" for c in value)
        ):
            return None
        try:
            p = urlsplit(value)
            pairs = parse_qsl(p.query, strict_parsing=True, max_num_fields=2)
            if p.scheme != "https" or p.fragment or len(pairs) != 1:
                return None
            return p, pairs[0]
        except ValueError:
            return None

    google = geo = None
    p = parsed("Google Maps")
    if (
        p is not None
        and p[0].netloc == "www.google.com"
        and p[0].path == "/maps"
        and p[1][0] == "q"
    ):
        try:
            lat, lon = map(float, p[1][1].split(","))
            saved_lat, saved_lon = (
                float(values["Szerokość geogr."]),
                float(values["Długość geogr."]),
            )
            if (
                all(math.isfinite(n) for n in (lat, lon, saved_lat, saved_lon))
                and -90 <= lat <= 90
                and -180 <= lon <= 180
                and abs(lat - saved_lat) <= 0.000001
                and abs(lon - saved_lon) <= 0.000001
            ):
                google = values["Google Maps"]
        except ValueError:
            pass
    p = parsed("Geoportal")
    if (
        p is not None
        and p[0].netloc == "mapy.geoportal.gov.pl"
        and p[0].path == "/imap/Imgp_2.html"
        and p[1][0] == "identifyParcel"
        and re.fullmatch(r"[0-9][0-9_./-]{0,119}", p[1][1])
        and ".." not in p[1][1]
    ):
        geo = values["Geoportal"]
    return google, geo


class PackagePreview:
    def __init__(self, store: PackageAccessStore, root: Path | None, *, slots=None):
        self.store, self.root = store, root
        self._slots = slots if slots is not None else threading.BoundedSemaphore(4)

    def read(
        self,
        customer_id: int,
        delivery_id: int,
        *,
        page: int = 1,
        category: str = "all",
        case_id: str | None = None,
    ) -> PreviewPage:
        if (
            type(page) is not int
            or not 1 <= page <= 200
            or category not in ("all", *CATEGORIES)
        ):
            raise ValueError("Niepoprawna strona lub filtr paczki")
        if (
            type(customer_id) is not int
            or type(delivery_id) is not int
            or not 0 < customer_id < 2**63
            or not 0 < delivery_id < 2**63
            or (case_id is not None and (not case_id or len(case_id) > 512))
        ):
            raise ValueError("Niepoprawna tożsamość paczki")
        if not self._slots.acquire(blocking=False):
            raise DownloadBusy
        try:
            artifact, items = self.store.preview_metadata(customer_id, delivery_id)
            if self.root is None or len(items) > MAX_ROWS:
                raise PreviewUnavailable
            identities = {}
            for raw_id, kind in items:
                if (
                    not raw_id.strip()
                    or len(raw_id) > 512
                    or not raw_id.isprintable()
                    or kind not in CATEGORIES
                ):
                    raise PreviewUnavailable
                key = (_protect_csv_value(raw_id), kind)
                if key in identities:
                    raise PreviewUnavailable
                identities[key] = raw_id
            with open_verified(self.root, artifact) as opened:
                if opened.stream.read(3) != b"\xef\xbb\xbf":
                    raise PreviewUnavailable
                opened.stream.seek(0)
                with TextIOWrapper(
                    opened.stream, encoding="utf-8-sig", errors="strict", newline=""
                ) as text:
                    reader = csv.reader(text, strict=True)
                    if tuple(next(reader, ())) != EXPORT_COLUMNS:
                        raise PreviewUnavailable
                    counts = {kind: 0 for kind in CATEGORIES}
                    seen = set()
                    rows = []
                    selected = None
                    total = 0
                    deadline = time.monotonic() + 5
                    for index, values in enumerate(reader):
                        if (
                            index >= MAX_ROWS
                            or len(values) != len(EXPORT_COLUMNS)
                            or any(len(v) > 131072 for v in values)
                            or time.monotonic() > deadline
                        ):
                            raise PreviewUnavailable
                        key = (values[0], values[1])
                        if key not in identities or key in seen:
                            raise PreviewUnavailable
                        seen.add(key)
                        kind = key[1]
                        counts[kind] += 1
                        matches = category == "all" or kind == category
                        wanted = (
                            matches
                            and (page - 1) * PAGE_SIZE <= total < page * PAGE_SIZE
                        )
                        if wanted or identities[key] == case_id:
                            data = dict(zip(EXPORT_COLUMNS, values))
                            google, geo = map_links(data)
                            truncated = any(len(v) > 2000 for v in values)
                            row = PreviewRow(
                                identities[key],
                                kind,
                                {k: v[:2000] for k, v in data.items()},
                                google,
                                geo,
                                truncated,
                            )
                            if wanted:
                                rows.append(row)
                            if row.case_id == case_id:
                                selected = row
                        if matches:
                            total += 1
                    if len(seen) != len(identities):
                        raise PreviewUnavailable
                    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
                    if page > pages:
                        raise ValueError("Strona nie istnieje w zapisanej paczce")
                    if case_id is not None and selected is None:
                        raise PackageNotFound
                    return PreviewPage(
                        tuple(rows),
                        selected,
                        counts,
                        total,
                        page,
                        pages,
                        category,
                        artifact.sha256,
                    )
        except (OSError, csv.Error, UnicodeError):
            raise PreviewUnavailable from None
        finally:
            self._slots.release()

    def history(self, customer_id: int, case_id: str):
        return self.store._read(
            lambda c: tuple(
                dict(r)
                for r in c.execute(
                    "SELECT issue_id,typ,opis,zgloszono_utc FROM issue_reports WHERE customer_id=? AND id_sprawy=? ORDER BY issue_id DESC LIMIT 4",
                    (customer_id, case_id),
                )
            )
        )
