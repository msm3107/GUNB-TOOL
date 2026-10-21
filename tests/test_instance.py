"""Jedna instancja bota na jednych danych – blokada pliku, zwalniana przez system także po awarii procesu."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.instance import (
    LEGACY_BOT_LOCK_NAME,
    AlreadyRunning,
    engine_process_lock,
    instance_lock,
    lock_is_free,
)

ROOT = Path(__file__).resolve().parent.parent


def test_second_instance_on_the_same_data_is_refused(tmp_path):
    lock = tmp_path / "gunb-bot.lock"
    with instance_lock(lock):
        assert not lock_is_free(lock)
        with pytest.raises(AlreadyRunning) as excinfo:
            with instance_lock(lock):
                pass
        assert str(os.getpid()) in str(excinfo.value)  # komunikat mówi, który proces trzyma blokadę
    assert lock_is_free(lock)
    with instance_lock(lock):  # po zakończeniu pierwszej – wolne
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="symlink lock regression is exercised on Linux CI")
def test_current_engine_detects_legacy_lock_next_to_symlinked_database(tmp_path):
    target_db = tmp_path / "actual" / "gunb_leads.sqlite"
    target_db.parent.mkdir()
    target_db.touch()
    alias_dir = tmp_path / "alias"
    alias_dir.mkdir()
    alias_db = alias_dir / target_db.name
    alias_db.symlink_to(target_db)

    # Older releases created gunb-bot.lock lexically beside the configured database path.
    with pytest.raises(AlreadyRunning), instance_lock(alias_dir / LEGACY_BOT_LOCK_NAME), engine_process_lock(alias_db):
        pass


@pytest.mark.skipif(sys.platform == "win32", reason="symlink lock regression is exercised on Linux CI")
def test_engine_process_lock_deduplicates_a_symlinked_database_directory(tmp_path):
    actual_dir = tmp_path / "actual"
    actual_dir.mkdir()
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(actual_dir, target_is_directory=True)
    db_path = alias_dir / "gunb_leads.sqlite"

    with engine_process_lock(db_path):
        pass


def test_lock_held_by_another_process_blocks_and_dies_with_it(tmp_path):
    lock = tmp_path / "gunb-bot.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys\nfrom core.instance import instance_lock\n"
         f"with instance_lock(r'{lock}'):\n    print('trzymam', flush=True)\n    sys.stdin.readline()\n"
         "    import os; os._exit(9)  # awaria procesu z blokadą w ręku\n"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "trzymam"
        with pytest.raises(AlreadyRunning):
            with instance_lock(lock):
                pass
    finally:
        holder.communicate("koniec\n", timeout=30)
    assert holder.returncode == 9
    with instance_lock(lock):  # system zwolnił blokadę zabitego procesu
        pass
