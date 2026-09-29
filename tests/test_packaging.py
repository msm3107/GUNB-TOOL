"""Pliki instalacyjne: pip czyta requirements w kodowaniu systemu (np. cp932, cp1250), więc muszą być ASCII."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt"])
def test_requirement_files_are_plain_ascii(name):
    content = (ROOT / name).read_bytes()
    assert content.isascii(), f"{name}: znaki spoza ASCII psują `pip install -r` na Windowsie"
