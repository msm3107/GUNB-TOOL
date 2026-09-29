"""Klient Bot API Telegrama dla interaktywnego bota: long polling, wiadomości HTML, przyciski, edycje."""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Sequence

from .exporter import RateLimiter, TELEGRAM_MIN_INTERVAL
from .http_client import HttpError, ResilientHttpClient

log = logging.getLogger(__name__)

TELEGRAM_BOT_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramApiError(RuntimeError):
    """Telegram odrzucił wywołanie (``ok: false``) albo nie udało się go wykonać."""

    def __init__(self, method: str, code: int | None, description: str) -> None:
        super().__init__(f"Telegram {method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description

    @property
    def blocked(self) -> bool:
        """Użytkownik zablokował bota (HTTP 403)."""
        return self.code == 403


class TelegramApi:
    """Wywołania Bot API używane przez bota; tempo wysyłki pilnowane osobno dla każdego czatu.

    Args:
        http: klient HTTP (jego ``timeout`` musi być dłuższy niż long polling).
        token: token bota.
        min_interval_per_chat: odstęp między wiadomościami do jednego czatu (Telegram: ~1/s).
    """

    def __init__(
        self,
        http: ResilientHttpClient,
        token: str,
        *,
        min_interval_per_chat: float = TELEGRAM_MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("Brak tokenu bota – ustaw TELEGRAM_BOT_TOKEN")
        self.http = http
        self._token = token
        self._min_interval = max(TELEGRAM_MIN_INTERVAL, min_interval_per_chat)
        self._clock = clock
        self._sleep = sleep
        self._limiters: dict[int, RateLimiter] = {}

    def call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        """Wywołuje metodę Bot API i zwraca ``result``.

        Raises:
            TelegramApiError: błąd sieci po ponowieniach albo odpowiedź ``ok: false``.
        """
        kwargs: dict[str, Any] = {"json": payload or {}}
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self.http.post(TELEGRAM_BOT_API.format(token=self._token, method=method), **kwargs)
        except HttpError as exc:
            raise TelegramApiError(method, exc.status_code, str(exc)) from exc
        try:
            body = response.json()
        except ValueError:
            raise TelegramApiError(method, response.status_code, "niepoprawna odpowiedź (brak JSON)") from None
        if not isinstance(body, dict) or not body.get("ok"):
            body = body if isinstance(body, dict) else {}
            raise TelegramApiError(method, body.get("error_code", response.status_code), body.get("description", ""))
        return body.get("result")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        """Long polling: czeka do ``timeout`` s na wiadomości i kliknięcia przycisków."""
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 15) or []

    def send_message(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> dict[str, Any]:
        """Wysyła wiadomość HTML (bez podglądu linków), najwyżej 1/s do tego samego czatu."""
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        limiter = self._limiters.setdefault(chat_id, RateLimiter(self._min_interval, clock=self._clock, sleep=self._sleep))
        limiter.wait()
        try:
            return self.call("sendMessage", payload)
        finally:
            limiter.touch()

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: dict[str, Any] | None = None
    ) -> None:
        """Podmienia treść wiadomości (np. ekran filtrów po kliknięciu)."""
        payload: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._edit("editMessageText", payload)

    def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: dict[str, Any]) -> None:
        """Podmienia tylko przyciski (np. „⭐ Zapisz” → „⭐ Zapisany ✓”)."""
        self._edit("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup})

    def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        """Potwierdza kliknięcie (zdejmuje „kręciołek”); ``text`` pokazuje się jako dymek."""
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        try:
            self.call("answerCallbackQuery", payload)
        except TelegramApiError as exc:  # np. „query is too old” po długim pobieraniu – bez znaczenia
            log.debug("answerCallbackQuery: %s", exc)

    def set_my_commands(self, commands: Sequence[tuple[str, str]]) -> None:
        """Ustawia menu komend widoczne pod „/” w aplikacji Telegram."""
        self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})

    def _edit(self, method: str, payload: dict[str, Any]) -> None:
        try:
            self.call(method, payload)
        except TelegramApiError as exc:
            if "message is not modified" in exc.description:
                return
            raise
