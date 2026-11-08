"""Private, immutable download artifacts; synthetic files/DB only."""

import hashlib
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from core.package_access import PackageAccessStore
from core.package_files import (
    ArtifactUnavailable,
    inspect_artifact,
    open_verified,
)
from core.storage import LeadRepository

NOW = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)


@pytest.fixture
def package(tmp_path):
    path, root = tmp_path / "downloads.sqlite", tmp_path / "private"
    root.mkdir(mode=0o700)
    with LeadRepository(path) as repo:
        c = repo.connection
        c.executemany(
            "INSERT INTO b2b_customers(customer_id,email) VALUES(?,?)",
            [(1, "owner@example.test"), (2, "other@example.test")],
        )
        c.execute(
            "INSERT INTO b2b_orders(order_id,customer_id,typ_pakietu,status_platnosci) VALUES(1,1,'jednorazowy','paid')"
        )
        c.execute(
            "INSERT INTO b2b_deliveries(delivery_id,order_id,typ_dostawy,doreczono_utc,plik_xlsx,plik_csv) VALUES(1,1,'delta','2026-11-01','sample.xlsx','sample.csv')"
        )
    for suffix in ("xlsx", "csv"):
        (root / ("sample." + suffix)).write_bytes(b"SYNTHETIC-" + suffix.encode())
    return PackageAccessStore(path), root


def test_manifest_is_two_immutable_formats_and_recording_never_publishes(package):
    store, root = package
    artifacts = tuple(
        inspect_artifact(root, "sample." + fmt, fmt) for fmt in ("xlsx", "csv")
    )
    store.record_artifacts(1, artifacts)
    store.record_artifacts(1, artifacts)  # safe backfill retry
    with sqlite3.connect(store.path) as c:
        assert c.execute(
            "SELECT format,size,sha256 FROM package_artifacts ORDER BY format"
        ).fetchall() == [(a.format, a.size, a.sha256) for a in reversed(artifacts)]
        assert c.execute("SELECT doreczono_utc FROM b2b_deliveries").fetchone() == (
            "2026-11-01",
        )
        assert c.execute("SELECT count(*) FROM package_publications").fetchone() == (0,)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE package_artifacts SET sha256=?", ("0" * 64,))
    with pytest.raises(ValueError):
        store.record_artifacts(
            1, (replace(artifacts[0], relative_name="other.xlsx"), artifacts[1])
        )


def test_real_file_is_hashed_and_streamed_through_one_handle(package):
    _, root = package
    artifact = inspect_artifact(root, "sample.csv", "csv")
    assert (
        artifact.size == 13
        and artifact.sha256 == hashlib.sha256(b"SYNTHETIC-csv").hexdigest()
    )
    with open_verified(root, artifact) as opened:
        assert opened.stream.read() == b"SYNTHETIC-csv"
    assert opened.stream.closed


@pytest.mark.parametrize(
    "name",
    [
        "../sample.csv",
        "/sample.csv",
        "sub/sample.csv",
        "sub\\sample.csv",
        "C:sample.csv",
        "sample.csv:stream",
        "..csv",
        "sample.xlsx",
    ],
)
def test_arbitrary_or_mismatched_paths_are_rejected(package, name):
    _, root = package
    with pytest.raises(ArtifactUnavailable):
        inspect_artifact(root, name, "csv")


def test_missing_corrupt_and_oversized_manifest_are_rejected(package):
    _, root = package
    good = inspect_artifact(root, "sample.csv", "csv")
    (root / "sample.csv").write_bytes(b"CORRUPT")
    with pytest.raises(ArtifactUnavailable):
        open_verified(root, good)
    with pytest.raises(ArtifactUnavailable):
        open_verified(root, replace(good, size=65 * 1024 * 1024))
    (root / "sample.csv").unlink()
    with pytest.raises(ArtifactUnavailable):
        open_verified(root, good)


@pytest.mark.parametrize("parent", [False, True])
def test_file_and_parent_symlinks_are_rejected(package, tmp_path, parent):
    _, root = package
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sample.csv").write_bytes(b"PRIVATE-OTHER")
    alias = tmp_path / "alias" if parent else root / "alias.csv"
    try:
        alias.symlink_to(
            outside if parent else outside / "sample.csv", target_is_directory=parent
        )
    except OSError:
        pytest.skip("OS does not permit synthetic symlinks")
    with pytest.raises(ArtifactUnavailable):
        inspect_artifact(
            alias if parent else root, "sample.csv" if parent else "alias.csv", "csv"
        )


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO")
def test_fifo_never_blocks_download(package):
    _, root = package
    os.mkfifo(root / "fifo.csv")
    with pytest.raises(ArtifactUnavailable):
        inspect_artifact(root, "fifo.csv", "csv")


def test_path_replacement_after_verification_cannot_change_stream(package):
    _, root = package
    good = inspect_artifact(root, "sample.csv", "csv")
    with open_verified(root, good) as opened:
        alternate = root / "alternate.csv"
        alternate.write_bytes(b"OTHER-SECRET")
        try:
            os.replace(alternate, root / "sample.csv")
        except PermissionError:
            assert os.name == "nt"  # native handle denies replacement while open
        assert opened.stream.read() == b"SYNTHETIC-csv"
