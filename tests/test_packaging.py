"""Pliki instalacyjne: pip czyta requirements w kodowaniu systemu (np. cp932, cp1250), więc muszą być ASCII."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["requirements.txt", "requirements-dev.txt", "requirements.lock"])
def test_requirement_files_are_plain_ascii(name):
    content = (ROOT / name).read_bytes()
    assert content.isascii(), f"{name}: znaki spoza ASCII psują `pip install -r` na Windowsie"


def _entries(name):
    lines = (ROOT / name).read_text(encoding="ascii").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.lstrip().startswith(("#", "-r"))]


def test_server_lock_pins_every_dependency_within_the_allowed_range():
    """Serwer instaluje ``requirements.lock`` (bez rozwiązywania zależności) – musi pasować do requirements.txt."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    pinned = {}
    for line in _entries("requirements.lock"):
        name, _, version = line.partition("==")
        assert version and "=" not in version, f"requirements.lock: {line!r} – tylko dokładne wersje (==)"
        pinned[canonicalize_name(name)] = version
    for line in _entries("requirements.txt"):
        requirement = Requirement(line)
        version = pinned.get(canonicalize_name(requirement.name))
        assert version is not None, f"{requirement.name}: brak w requirements.lock"
        assert requirement.specifier.contains(version), f"{requirement.name}=={version} poza zakresem {requirement}"
