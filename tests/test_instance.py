"""Jedna instancja bota na jednych danych – blokada pliku, zwalniana przez system także po awarii procesu."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from gunb_tool.instance import AlreadyRunning, instance_lock, lock_is_free

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


def test_lock_held_by_another_process_blocks_and_dies_with_it(tmp_path):
    lock = tmp_path / "gunb-bot.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys\nfrom gunb_tool.instance import instance_lock\n"
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
