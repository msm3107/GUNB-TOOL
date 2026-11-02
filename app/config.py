"""Small, explicit web configuration; independent from engine configuration files."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .stripe_service import CheckoutSettings


@dataclass(frozen=True)
class WebConfig:
    cors_origins: tuple[str, ...] = ()
    database_path: Path | None = None
    checkout: CheckoutSettings = field(default_factory=CheckoutSettings)
    webhook_secret: str = field(default="", repr=False)
    test_package_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.database_path is not None and not isinstance(self.database_path, Path):
            raise ValueError("database_path musi być ścieżką Path albo None")
        if not isinstance(self.checkout, CheckoutSettings):
            raise TypeError("Niepoprawna konfiguracja Checkout")
        if not isinstance(self.webhook_secret, str):
            raise TypeError("Niepoprawny sekret webhooka")
        if self.webhook_secret:
            static = Path(__file__).resolve().parent / "static"
            if (not self.checkout.test_mode or not re.fullmatch(r"whsec_[A-Za-z0-9_]{16,}",self.webhook_secret)
                    or not isinstance(self.test_package_dir,Path) or not self.test_package_dir.is_absolute()
                    or self.test_package_dir.resolve().is_relative_to(static)):
                raise ValueError("Webhook wymaga testowego Checkout, sekretu i prywatnego katalogu poza static")
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
        raw_output = source.get("GUNB_WEB_TEST_PACKAGE_DIR", "").strip()
        return cls(cors_origins=origins, database_path=Path(raw_path).expanduser().absolute() if raw_path else None,
                   checkout=CheckoutSettings.from_env(source), webhook_secret=source.get("GUNB_WEB_STRIPE_WEBHOOK_SECRET",""),
                   test_package_dir=Path(raw_output).expanduser().absolute() if raw_output else None)
