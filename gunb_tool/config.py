"""Wczytywanie i walidacja konfiguracji: ``config.yaml`` + zmienne środowiskowe (``.env``).

Sekrety (tokeny, adresy webhooków) nie powinny być wpisywane do ``config.yaml`` wprost –
plik obsługuje placeholdery ``${NAZWA}`` oraz ``${NAZWA:-wartość_domyślna}`` rozwijane
ze zmiennych środowiskowych (opcjonalnie wczytywanych z pliku ``.env`` obok konfiguracji).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from .models import BUILDING_CATEGORIES, LEAD_CATEGORIES, Source
from .teryt import get_voivodeship
from .text import fold_polish

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Błąd konfiguracji; komunikat jest przeznaczony dla użytkownika."""


DEFAULT_USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
)

DEFAULT_EXCLUDE_KEYWORDS: tuple[str, ...] = (
    "ogrodzen", "zjazd", "przylacz", "siec", "gazow", "gazociag", "wodociag", "kanalizac",
    "elektroenerget", "kablow", "napowietrzn", "oswietleni", "telekomunikac", "bazow",
    "maszt", "reklam", "fotowolt", "wiatrow", "zbiornik", "oczyszczal",
    "szamb", "studni", "drog", "chodnik", "parking", "melioracj",
)
"""Rdzenie słów oznaczających „szum” (dopasowanie do tekstu bez polskich znaków)."""

DEFAULT_NOISE_CATEGORIES: tuple[str, ...] = (
    "IV", "VI", "VII", "XXI", "XXII", "XXIII", "XXIV", "XXV", "XXVI", "XXVII", "XXVIII", "XXIX", "XXX",
)

DEFAULT_RESIDENTIAL_PATTERNS: tuple[str, ...] = (
    r"(?<!nie)mieszkal", r"jednorodzin", r"wielorodzin", r"blizniacz", r"szeregow",
    r"\bdom(u|y|ow|ami)?\b(?! (kultury|pomocy|opieki|pogrzebow|wypoczynk|dziecka|studenck))",
    r"letnisk", r"rekreacji indywidualnej", r"apartament", r"osiedl",
)

DEFAULT_COMMERCIAL_PATTERNS: tuple[str, ...] = (
    r"\bhal(a|i|e|ami)?\b", r"magazyn", r"biur", r"uslug", r"handl", r"produkcyj", r"hotel",
    r"motel", r"pensjonat", r"restaurac", r"gastronom", r"sklep", r"market", r"warsztat",
    r"salon", r"stacj\w* (paliw|obslugi|kontroli)", r"myjni", r"pawilon", r"przemysl", r"logistyczn",
)

DATE_FIELDS: tuple[str, ...] = ("decyzja", "wplyw")


@dataclass(frozen=True)
class HttpConfig:
    """Polityka sieciowa wspólna dla GUNB i ULDK."""

    timeout: float = 60.0
    max_retries: int = 5
    backoff_base: float = 2.0
    backoff_max: float = 60.0
    min_delay: float = 1.0
    max_delay: float = 3.0
    user_agents: tuple[str, ...] = DEFAULT_USER_AGENTS


@dataclass(frozen=True)
class GunbConfig:
    """Zakres pobierania danych z rejestru GUNB."""

    base_url: str = "https://wyszukiwarka.gunb.gov.pl/pliki_pobranie/"
    cache_dir: Path = Path("data/cache")
    sources: tuple[Source, ...] = (Source.POZWOLENIA,)
    voivodeships: tuple[str, ...] = ()
    powiats: tuple[str, ...] = ()
    date_field: str = "decyzja"
    lookback_days: int = 60
    page_size: int = 200


@dataclass(frozen=True)
class FilterConfig:
    """Reguły kategoryzacji i odrzucania szumu."""

    drop_noise: bool = True
    drop_demolitions: bool = True
    exclude_keywords: tuple[str, ...] = DEFAULT_EXCLUDE_KEYWORDS
    noise_categories: tuple[str, ...] = DEFAULT_NOISE_CATEGORIES
    include_categories: tuple[str, ...] = ()
    residential_patterns: tuple[str, ...] = DEFAULT_RESIDENTIAL_PATTERNS
    commercial_patterns: tuple[str, ...] = DEFAULT_COMMERCIAL_PATTERNS


@dataclass(frozen=True)
class GeocodingConfig:
    """Geokodowanie działek przez ULDK (GUGiK)."""

    enabled: bool = True
    uldk_url: str = "https://uldk.gugik.gov.pl/"
    max_parcels_per_case: int = 3
    region_fallback: bool = True
    min_delay: float = 0.2
    max_delay: float = 0.8
    negative_cache_days: int = 30


@dataclass(frozen=True)
class StorageConfig:
    """Lokalizacja bazy SQLite."""

    db_path: Path = Path("data/gunb_leads.sqlite")


@dataclass(frozen=True)
class NotificationsConfig:
    """Limity wspólne dla kanałów powiadomień."""

    max_messages_per_run: int = 30
    max_age_days: int = 14


@dataclass(frozen=True)
class TelegramConfig:
    """Bot Telegrama (token z @BotFather, identyfikator czatu/kanału)."""

    bot_token: str = ""
    chat_id: str = ""
    delay_seconds: float = 1.1


@dataclass(frozen=True)
class DiscordConfig:
    """Webhook kanału Discord."""

    webhook_url: str = ""
    delay_seconds: float = 1.0


@dataclass(frozen=True)
class SheetsConfig:
    """Eksport do Google Sheets przez konto serwisowe."""

    service_account_file: Path | None = None
    spreadsheet_id: str = ""
    worksheet: str = "Leady"


@dataclass(frozen=True)
class LoggingConfig:
    """Poziom i (opcjonalny) plik logów."""

    level: str = "INFO"
    file: Path | None = None


@dataclass(frozen=True)
class AppConfig:
    """Kompletna, zwalidowana konfiguracja aplikacji."""

    http: HttpConfig
    gunb: GunbConfig
    filter: FilterConfig
    geocoding: GeocodingConfig
    storage: StorageConfig
    notifications: NotificationsConfig
    telegram: TelegramConfig
    discord: DiscordConfig
    sheets: SheetsConfig
    logging: LoggingConfig


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_KNOWN_SECTIONS = {
    "http", "gunb", "filter", "geocoding", "storage", "notifications", "telegram", "discord", "sheets",
    "logging",
}


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> AppConfig:
    """Wczytuje, rozwija placeholdery i waliduje konfigurację.

    Args:
        path: ścieżka do ``config.yaml``; ścieżki względne w pliku liczone są od jego katalogu.
        env: źródło zmiennych dla ``${...}``. Domyślnie ``os.environ`` uzupełnione
            o plik ``.env`` z katalogu konfiguracji (wartości już ustawione mają pierwszeństwo).

    Raises:
        ConfigError: brak pliku, błędny YAML lub niepoprawne wartości.
    """
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Brak pliku konfiguracyjnego: {path}")
    base_dir = path.resolve().parent
    if env is None:
        _load_dotenv(base_dir / ".env")
        env = os.environ
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Niepoprawny YAML w {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: plik musi zawierać mapę sekcji (http, gunb, filter, ...)")
    for unknown in sorted(set(raw) - _KNOWN_SECTIONS):
        log.warning("Nieznana sekcja konfiguracji %r – zostanie pominięta", unknown)
    raw = _interpolate(raw, env)

    return AppConfig(
        http=_http(_section(raw, "http")),
        gunb=_gunb(_section(raw, "gunb"), base_dir),
        filter=_filter(_section(raw, "filter")),
        geocoding=_geocoding(_section(raw, "geocoding")),
        storage=_storage(_section(raw, "storage"), base_dir),
        notifications=_notifications(_section(raw, "notifications")),
        telegram=_telegram(_section(raw, "telegram")),
        discord=_discord(_section(raw, "discord")),
        sheets=_sheets(_section(raw, "sheets"), base_dir),
        logging=_logging(_section(raw, "logging"), base_dir),
    )


# --- Sekcje ---------------------------------------------------------------

def _http(data: dict[str, Any]) -> HttpConfig:
    defaults = HttpConfig()
    min_delay = _number(data, "http", "min_delay", defaults.min_delay)
    max_delay = _number(data, "http", "max_delay", defaults.max_delay)
    if min_delay > max_delay:
        raise ConfigError("http.min_delay nie może być większe niż http.max_delay")
    agents = tuple(str(a).strip() for a in _list(data, "http", "user_agents", defaults.user_agents))
    if not agents or not all(agents):
        raise ConfigError("http.user_agents: lista musi zawierać co najmniej jeden niepusty User-Agent")
    return HttpConfig(
        timeout=_number(data, "http", "timeout", defaults.timeout, minimum=1),
        max_retries=int(_number(data, "http", "max_retries", defaults.max_retries)),
        backoff_base=_number(data, "http", "backoff_base", defaults.backoff_base),
        backoff_max=_number(data, "http", "backoff_max", defaults.backoff_max),
        min_delay=min_delay,
        max_delay=max_delay,
        user_agents=agents,
    )


def _gunb(data: dict[str, Any], base_dir: Path) -> GunbConfig:
    defaults = GunbConfig()
    voivodeships = [_voivodeship_code(v) for v in _list(data, "gunb", "voivodeships", ())]
    powiats = [_powiat_code(p) for p in _list(data, "gunb", "powiats", ())]
    for powiat in powiats:
        if powiat[:2] not in voivodeships:
            voivodeships.append(powiat[:2])
    if not voivodeships:
        raise ConfigError(
            "gunb.voivodeships: podaj co najmniej jedno województwo (kod TERYT, np. '12') "
            "albo listę gunb.powiats"
        )

    sources = []
    for value in _list(data, "gunb", "sources", [s.value for s in defaults.sources]):
        try:
            sources.append(Source(str(value).strip().lower()))
        except ValueError:
            raise ConfigError(
                f"gunb.sources: nieznane źródło {value!r} (dozwolone: pozwolenia, zgloszenia)"
            ) from None
    if not sources:
        raise ConfigError("gunb.sources: podaj co najmniej jedno źródło (pozwolenia, zgloszenia)")

    date_field = str(data.get("date_field", defaults.date_field)).strip().lower()
    if date_field not in DATE_FIELDS:
        raise ConfigError(f"gunb.date_field: {date_field!r} – dozwolone wartości: {', '.join(DATE_FIELDS)}")

    return GunbConfig(
        base_url=str(data.get("base_url", defaults.base_url)).rstrip("/") + "/",
        cache_dir=_path(data.get("cache_dir", defaults.cache_dir), base_dir),
        sources=tuple(dict.fromkeys(sources)),
        voivodeships=tuple(dict.fromkeys(voivodeships)),
        powiats=tuple(dict.fromkeys(powiats)),
        date_field=date_field,
        lookback_days=int(_number(data, "gunb", "lookback_days", defaults.lookback_days, minimum=1)),
        page_size=int(_number(data, "gunb", "page_size", defaults.page_size, minimum=1)),
    )


def _filter(data: dict[str, Any]) -> FilterConfig:
    defaults = FilterConfig()
    noise_categories = tuple(
        str(c).strip().upper() for c in _list(data, "filter", "noise_categories", defaults.noise_categories)
    )
    for category in noise_categories:
        if category not in BUILDING_CATEGORIES:
            raise ConfigError(f"filter.noise_categories: {category!r} nie jest kategorią obiektu (I–XXX)")
    include = tuple(
        str(c).strip().lower() for c in _list(data, "filter", "include_categories", defaults.include_categories)
    )
    for category in include:
        if category not in LEAD_CATEGORIES:
            raise ConfigError(
                f"filter.include_categories: {category!r} – dozwolone: {', '.join(LEAD_CATEGORIES)}"
            )
    return FilterConfig(
        drop_noise=_bool(data, "filter", "drop_noise", defaults.drop_noise),
        drop_demolitions=_bool(data, "filter", "drop_demolitions", defaults.drop_demolitions),
        exclude_keywords=_patterns(data, "exclude_keywords", defaults.exclude_keywords),
        noise_categories=noise_categories,
        include_categories=include,
        residential_patterns=_patterns(data, "residential_patterns", defaults.residential_patterns),
        commercial_patterns=_patterns(data, "commercial_patterns", defaults.commercial_patterns),
    )


def _geocoding(data: dict[str, Any]) -> GeocodingConfig:
    defaults = GeocodingConfig()
    min_delay = _number(data, "geocoding", "min_delay", defaults.min_delay)
    max_delay = _number(data, "geocoding", "max_delay", defaults.max_delay)
    if min_delay > max_delay:
        raise ConfigError("geocoding.min_delay nie może być większe niż geocoding.max_delay")
    return GeocodingConfig(
        enabled=_bool(data, "geocoding", "enabled", defaults.enabled),
        uldk_url=str(data.get("uldk_url", defaults.uldk_url)),
        max_parcels_per_case=int(
            _number(data, "geocoding", "max_parcels_per_case", defaults.max_parcels_per_case, minimum=1)
        ),
        region_fallback=_bool(data, "geocoding", "region_fallback", defaults.region_fallback),
        min_delay=min_delay,
        max_delay=max_delay,
        negative_cache_days=int(
            _number(data, "geocoding", "negative_cache_days", defaults.negative_cache_days)
        ),
    )


def _storage(data: dict[str, Any], base_dir: Path) -> StorageConfig:
    return StorageConfig(db_path=_path(data.get("db_path", StorageConfig.db_path), base_dir))


def _notifications(data: dict[str, Any]) -> NotificationsConfig:
    defaults = NotificationsConfig()
    return NotificationsConfig(
        max_messages_per_run=int(
            _number(data, "notifications", "max_messages_per_run", defaults.max_messages_per_run, minimum=1)
        ),
        max_age_days=int(_number(data, "notifications", "max_age_days", defaults.max_age_days, minimum=1)),
    )


def _telegram(data: dict[str, Any]) -> TelegramConfig:
    defaults = TelegramConfig()
    return TelegramConfig(
        bot_token=str(data.get("bot_token") or "").strip(),
        chat_id=str(data.get("chat_id") or "").strip(),
        delay_seconds=_number(data, "telegram", "delay_seconds", defaults.delay_seconds),
    )


def _discord(data: dict[str, Any]) -> DiscordConfig:
    defaults = DiscordConfig()
    return DiscordConfig(
        webhook_url=str(data.get("webhook_url") or "").strip(),
        delay_seconds=_number(data, "discord", "delay_seconds", defaults.delay_seconds),
    )


def _sheets(data: dict[str, Any], base_dir: Path) -> SheetsConfig:
    sa_file = str(data.get("service_account_file") or "").strip()
    return SheetsConfig(
        service_account_file=_path(sa_file, base_dir) if sa_file else None,
        spreadsheet_id=str(data.get("spreadsheet_id") or "").strip(),
        worksheet=str(data.get("worksheet") or SheetsConfig.worksheet).strip(),
    )


def _logging(data: dict[str, Any], base_dir: Path) -> LoggingConfig:
    level = str(data.get("level", LoggingConfig.level)).strip().upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError(f"logging.level: nieznany poziom {level!r}")
    log_file = str(data.get("file") or "").strip()
    return LoggingConfig(level=level, file=_path(log_file, base_dir) if log_file else None)


# --- Pomocnicze -----------------------------------------------------------

def _load_dotenv(path: Path) -> None:
    """Wczytuje ``.env`` (jeśli istnieje) bez nadpisywania istniejących zmiennych."""
    if not path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv jest w requirements.txt
        log.warning("Znaleziono %s, ale brak pakietu python-dotenv – plik pominięty", path)
        return
    load_dotenv(path, override=False)


def _interpolate(value: Any, env: Mapping[str, str]) -> Any:
    """Rekurencyjnie rozwija ``${VAR}`` / ``${VAR:-domyślna}`` w wartościach tekstowych."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: env.get(m.group(1)) or m.group(2) or "", value)
    if isinstance(value, list):
        return [_interpolate(v, env) for v in value]
    if isinstance(value, dict):
        return {k: _interpolate(v, env) for k, v in value.items()}
    return value


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    data = raw.get(name) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"Sekcja {name!r} musi być mapą klucz: wartość")
    return data


def _list(data: dict[str, Any], section: str, key: str, default: Any) -> list[Any]:
    value = data.get(key, default)
    if value is None:
        return []
    if isinstance(value, (str, int)):
        return [value]
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{section}.{key}: oczekiwano listy")
    return list(value)


def _number(data: dict[str, Any], section: str, key: str, default: float, minimum: float = 0) -> float:
    value = data.get(key, default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{section}.{key}: oczekiwano liczby, otrzymano {value!r}") from None
    if number < minimum:
        raise ConfigError(f"{section}.{key}: wartość musi być >= {minimum}")
    return number


def _bool(data: dict[str, Any], section: str, key: str, default: bool) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "tak", "on"}:
        return True
    if text in {"0", "false", "no", "nie", "off", ""}:
        return False
    raise ConfigError(f"{section}.{key}: oczekiwano wartości logicznej, otrzymano {value!r}")


def _patterns(data: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Normalizuje wzorce (małe litery, bez diakrytyków) i sprawdza, czy są poprawnymi regexami."""
    patterns = []
    for item in _list(data, "filter", key, default):
        pattern = fold_polish(str(item)).lower().strip()
        if not pattern:
            continue
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"filter.{key}: niepoprawny wzorzec {item!r}: {exc}") from None
        patterns.append(pattern)
    return tuple(patterns)


def _voivodeship_code(value: Any) -> str:
    try:
        return get_voivodeship(str(value)).code
    except KeyError:
        raise ConfigError(
            f"gunb.voivodeships: nieznane województwo {value!r} (podaj kod TERYT, np. '12', lub nazwę)"
        ) from None


def _powiat_code(value: Any) -> str:
    code = str(value).strip()
    if len(code) != 4 or not code.isdigit():
        raise ConfigError(
            f"gunb.powiats: {value!r} nie jest 4-cyfrowym kodem TERYT powiatu – podawaj kody "
            "w cudzysłowie, np. '0201' (bez cudzysłowu YAML czyta 0201 jako liczbę ósemkową)"
        )
    try:
        get_voivodeship(code[:2])
    except KeyError:
        raise ConfigError(f"gunb.powiats: {code!r} – pierwsze dwie cyfry nie są kodem województwa") from None
    return code


def _path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value)).expanduser()
    return path if path.is_absolute() else base_dir / path
