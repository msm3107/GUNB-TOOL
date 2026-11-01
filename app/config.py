"""Small, explicit web configuration; independent from engine configuration files."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class WebConfig:
    cors_origins: tuple[str, ...] = ()
    database_path: Path | None = None

    def __post_init__(self) -> None:
        if self.database_path is not None and not isinstance(self.database_path, Path):
            raise ValueError("database_path musi być ścieżką Path albo None")
        if not isinstance(self.cors_origins, tuple):
            raise TypeError("CORS wymaga krotki jawnych originów HTTP(S)")
        for origin in self.cors_origins:
            if (
                not isinstance(origin, str) or not origin.isascii() or not origin
                or any(ord(char) < 33 or ord(char) == 127 for char in origin)
                or any(char in origin for char in "*\\?#")
            ):
                raise ValueError("Niepoprawny origin CORS")
            try:
                parsed = urlsplit(origin)
                _ = parsed.port  # Validate an explicitly configured port as well.
                if (
                    parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment or parsed.netloc.endswith(":")
                ):
                    raise ValueError
            except ValueError:
                raise ValueError("CORS wymaga originu bez ścieżki, danych logowania i wildcardów") from None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> WebConfig:
        source = os.environ if environ is None else environ
        origins = tuple(value.strip() for value in source.get("GUNB_WEB_CORS_ORIGINS", "").split(",") if value.strip())
        raw_path = source.get("GUNB_WEB_DB_PATH", "").strip()
        return cls(cors_origins=origins, database_path=Path(raw_path).expanduser().absolute() if raw_path else None)
