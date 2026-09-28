"""Odporny klient HTTP: retry z backoffem, ``Retry-After``, rotacja User-Agent, losowe opóźnienia.

Ten sam klient obsługuje pobieranie paczek GUNB (warunkowe i wznawiane), zapytania do ULDK
oraz wysyłkę powiadomień – każdy z tych kanałów dostaje własną instancję z własnymi limitami.
"""

from __future__ import annotations

import email.utils
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import requests

from .config import HttpConfig

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)
MAX_RETRY_AFTER = 600.0
"""Górny limit (s) czekania na żądanie serwera z nagłówka ``Retry-After``."""

_CHUNK_SIZE = 1 << 20

# Sekrety bywają częścią URL-a (token bota Telegrama, token webhooka Discorda) – nie mogą trafić do logów.
_SECRET_URL_PATTERNS = (
    (re.compile(r"(/bot)[^/]+"), r"\g<1><token>"),
    (re.compile(r"(/webhooks/[^/]+/)[^/?#]+"), r"\g<1><token>"),
)


def redact_url(url: str) -> str:
    """Maskuje sekrety w URL-u (``/bot<token>/``, ``/webhooks/<id>/<token>``) przed logowaniem."""
    for pattern, replacement in _SECRET_URL_PATTERNS:
        url = pattern.sub(replacement, url)
    return url


class HttpError(RuntimeError):
    """Nieudane zapytanie HTTP (po wyczerpaniu ponowień lub z nieoczekiwanym statusem)."""

    def __init__(self, message: str, *, status_code: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url


@dataclass(frozen=True)
class DownloadResult:
    """Wynik :meth:`ResilientHttpClient.download`."""

    path: Path
    not_modified: bool
    etag: str | None
    last_modified: str | None
    size: int


class UserAgentRotator:
    """Losuje User-Agent z puli, unikając powtórzenia poprzedniego."""

    def __init__(self, agents: Sequence[str], rng: random.Random) -> None:
        if not agents:
            raise ValueError("Pula User-Agent nie może być pusta")
        self._agents = list(agents)
        self._rng = rng
        self._last: str | None = None

    def next(self) -> str:
        """Zwraca kolejny User-Agent."""
        candidates = [a for a in self._agents if a != self._last] or self._agents
        self._last = self._rng.choice(candidates)
        return self._last


class ResilientHttpClient:
    """Klient HTTP z polityką ponowień i „uprzejmym” tempem zapytań.

    Args:
        config: parametry retry, opóźnień i pula User-Agent.
        session: sesja ``requests`` (wstrzykiwana w testach).
        sleep: funkcja usypiająca (wstrzykiwana w testach).
        clock: zegar monotoniczny w sekundach (wstrzykiwany w testach).
        rng: generator losowy dla jittera i rotacji User-Agent.
    """

    def __init__(
        self,
        config: HttpConfig,
        *,
        session: requests.Session | Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._agents = UserAgentRotator(config.user_agents, self._rng)
        self._last_request_at: float | None = None

    # --- Zapytania -------------------------------------------------------------

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        """``GET`` z polityką ponowień (patrz :meth:`request`)."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        """``POST`` z polityką ponowień (patrz :meth:`request`)."""
        return self.request("POST", url, **kwargs)

    def request(
        self, method: str, url: str, *, headers: Mapping[str, str] | None = None, **kwargs: Any
    ) -> requests.Response:
        """Wykonuje zapytanie, ponawiając błędy sieci oraz statusy 408/425/429/5xx.

        Przed każdym zapytaniem (poza pierwszym) odczekuje losowy odstęp z zakresu
        ``[min_delay, max_delay]``, a każda próba dostaje inny User-Agent z puli.

        Returns:
            Odpowiedź o statusie spoza puli ponawianych (również 4xx – decyduje wywołujący).

        Raises:
            HttpError: gdy wyczerpano limit ponowień.
        """
        safe_url = redact_url(url)
        merged = {"Accept-Language": "pl-PL,pl;q=0.9,en;q=0.5"}
        merged.update(headers or {})
        kwargs.setdefault("timeout", self.config.timeout)
        last_error: BaseException | None = None
        last_status: int | None = None

        for attempt in range(self.config.max_retries + 1):
            self._throttle()
            merged["User-Agent"] = self._agents.next()
            try:
                response = self._session.request(method, url, headers=dict(merged), **kwargs)
            except RETRYABLE_EXCEPTIONS as exc:
                last_error, last_status = exc, None
                delay = self._backoff(attempt)
            else:
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                last_status = response.status_code
                last_error = HttpError(f"HTTP {response.status_code}", status_code=last_status, url=safe_url)
                retry_after = _retry_after_seconds(response)
                delay = min(retry_after, MAX_RETRY_AFTER) if retry_after is not None else self._backoff(attempt)
                response.close()
            finally:
                self._last_request_at = self._clock()

            if attempt < self.config.max_retries:
                log.warning(
                    "%s %s: %s – ponowienie %d/%d za %.1f s",
                    method, safe_url, last_error, attempt + 1, self.config.max_retries, delay,
                )
                self._sleep(delay)

        raise HttpError(
            f"{method} {safe_url}: wyczerpano limit ponowień ({self.config.max_retries}); ostatni błąd: {last_error}",
            status_code=last_status,
            url=safe_url,
        ) from last_error

    # --- Pobieranie plików -----------------------------------------------------

    def download(
        self, url: str, dest: str | Path, *, validator: Callable[[Path], bool] | None = None
    ) -> DownloadResult:
        """Pobiera plik do ``dest`` – warunkowo, strumieniowo i z wznawianiem.

        * Gdy ``dest`` istnieje i znamy jego ``ETag``/``Last-Modified`` (plik ``<dest>.meta.json``),
          wysyła ``If-None-Match``/``If-Modified-Since``; odpowiedź 304 zostawia plik bez zmian.
        * Treść trafia najpierw do ``<dest>.part``; przerwany transfer jest wznawiany nagłówkiem
          ``Range`` (z ``If-Range``), a gdy serwer go zignoruje – pobierany od nowa.
        * Plik zastępuje ``dest`` dopiero po sprawdzeniu rozmiaru i (opcjonalnie) ``validator``.

        Raises:
            HttpError: gdy nie udało się pobrać kompletnego, poprawnego pliku.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        meta_path = dest.with_name(dest.name + ".meta.json")
        cached_meta = _read_json(meta_path) if dest.exists() else {}
        part.unlink(missing_ok=True)
        resume_etag: str | None = None
        last_error: BaseException | None = None

        for attempt in range(self.config.max_retries + 1):
            if attempt:
                self._sleep(self._backoff(attempt - 1))
            offset = part.stat().st_size if part.exists() else 0
            if offset and not resume_etag:
                part.unlink()  # bez ETag nie da się bezpiecznie dokleić reszty
                offset = 0

            headers: dict[str, str] = {}
            if offset:
                headers.update({"Range": f"bytes={offset}-", "If-Range": resume_etag or ""})
            elif cached_meta:
                if cached_meta.get("etag"):
                    headers["If-None-Match"] = cached_meta["etag"]
                if cached_meta.get("last_modified"):
                    headers["If-Modified-Since"] = cached_meta["last_modified"]

            response = self.request("GET", url, headers=headers, stream=True)
            try:
                status = response.status_code
                if status == 304 and not offset and dest.exists():
                    log.info("%s: bez zmian od ostatniego pobrania (304)", url)
                    return DownloadResult(
                        dest, True, cached_meta.get("etag"), cached_meta.get("last_modified"), dest.stat().st_size
                    )
                if status == 416:
                    part.unlink(missing_ok=True)
                    resume_etag = None
                    last_error = HttpError("HTTP 416 – zakres niedostępny", status_code=416, url=url)
                    continue
                if status == 206 and offset:
                    mode = "ab"
                elif status == 200:
                    mode, offset = "wb", 0
                    resume_etag = response.headers.get("ETag")
                else:
                    raise HttpError(f"GET {url}: nieoczekiwany status HTTP {status}", status_code=status, url=url)

                expected = _expected_total_size(response, offset)
                try:
                    with part.open(mode) as fh:
                        for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                            if chunk:
                                fh.write(chunk)
                except RETRYABLE_EXCEPTIONS as exc:
                    last_error = exc
                    log.warning("%s: przerwany transfer (%s) – wznowienie", url, exc)
                    continue

                size = part.stat().st_size
                if expected is not None and size != expected:
                    last_error = HttpError(f"niekompletny plik: {size} z {expected} B", url=url)
                    log.warning("%s: %s", url, last_error)
                    if size > expected:
                        part.unlink()
                    continue
                if validator is not None and not validator(part):
                    last_error = HttpError("pobrany plik nie przeszedł walidacji", url=url)
                    log.warning("%s: %s", url, last_error)
                    part.unlink()
                    resume_etag = None
                    continue

                os.replace(part, dest)
                etag = response.headers.get("ETag") or resume_etag
                last_modified = response.headers.get("Last-Modified")
                _write_json(meta_path, {
                    "url": url,
                    "etag": etag,
                    "last_modified": last_modified,
                    "size": size,
                    "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                })
                log.info("%s: pobrano %.1f MB", url, size / 1e6)
                return DownloadResult(dest, False, etag, last_modified, size)
            finally:
                response.close()

        part.unlink(missing_ok=True)
        raise HttpError(f"Nie udało się pobrać {url}: {last_error}", url=url) from last_error

    # --- Pomocnicze ------------------------------------------------------------

    def _throttle(self) -> None:
        """Losowy odstęp od poprzedniego zapytania (``min_delay``–``max_delay``)."""
        if self._last_request_at is None or self.config.max_delay <= 0:
            return
        gap = self._rng.uniform(self.config.min_delay, self.config.max_delay)
        wait = gap - (self._clock() - self._last_request_at)
        if wait > 0:
            self._sleep(wait)

    def _backoff(self, attempt: int) -> float:
        """Wykładniczy backoff z „equal jitter”: połowa limitu + losowa druga połowa."""
        cap = min(self.config.backoff_max, self.config.backoff_base * (2 ** attempt))
        return cap / 2 + self._rng.uniform(0, cap / 2)


def _retry_after_seconds(response: Any) -> float | None:
    """Czas oczekiwania z nagłówka ``Retry-After`` lub pola ``retry_after`` w JSON (Telegram/Discord)."""
    value = response.headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                moment = email.utils.parsedate_to_datetime(value)
                return max(0.0, (moment - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass
    try:
        payload = response.json()
    except ValueError:
        return None
    if isinstance(payload, dict):
        parameters = payload.get("parameters")
        for candidate in (payload.get("retry_after"), parameters.get("retry_after") if isinstance(parameters, dict) else None):
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
                return max(0.0, float(candidate))
    return None


def _expected_total_size(response: Any, offset: int) -> int | None:
    """Oczekiwany rozmiar pliku po zakończeniu transferu (``Content-Range`` / ``Content-Length``)."""
    content_range = response.headers.get("Content-Range")
    if content_range and "/" in content_range:
        total = content_range.rsplit("/", 1)[1].strip()
        if total.isdigit():
            return int(total)
    length = response.headers.get("Content-Length")
    if length and length.isdigit():
        return offset + int(length)
    return None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
