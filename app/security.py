"""Stateless HTTP response policy, outside FastAPI's server-error middleware."""

from __future__ import annotations

import logging
from urllib.parse import unquote

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class DownloadLogRedaction(logging.Filter):
    """Uvicorn access records carry the raw request target in positional args."""

    def __init__(self):
        super().__init__("gunb.download-redaction")

    def filter(self, record):
        def private_target(value):
            if isinstance(value,str):
                decoded=unquote(value)
                if '/pobierz/' in decoded:
                    return '/pobierz/[REDACTED]'
                if '/panel/paczki/' in decoded:
                    return '/panel/paczki/[PRIVATE]'
            return None

        def redact(value):
            return private_target(value) or value

        target=private_target(record.msg)
        if target:
            record.msg, record.args = 'Private request '+target, ()
        elif isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) for key, value in record.args.items()}
        return True


def redact_download_access_logs():
    logger = logging.getLogger("uvicorn.access")
    if not any(f.name == "gunb.download-redaction" for f in logger.filters):
        logger.addFilter(DownloadLogRedaction())


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    headers[name] = value
                if scope.get("path") == "/zamowienie":
                    headers["Referrer-Policy"] = "same-origin"
                if scope.get("path") in {"/zamowienie/podsumowanie", "/zamowienie/checkout"}:
                    headers["Content-Security-Policy"] = SECURITY_HEADERS["Content-Security-Policy"].replace(
                        "form-action 'self';", "form-action 'self' https://checkout.stripe.com;"
                    )
                    headers["Referrer-Policy"] = "same-origin"
                if scope.get("path") in {"/panel", "/panel/uwagi", "/panel/rozliczenia"}:
                    # Chromium checks form-action against the POST's redirects.
                    # Only this page's native Portal form needs the exact host
                    # already enforced by PortalService; no wildcard or script relaxation.
                    headers["Content-Security-Policy"] = SECURITY_HEADERS["Content-Security-Policy"].replace(
                        "form-action 'self';", "form-action 'self' https://billing.stripe.com;"
                    )
                if scope.get("path", "").startswith("/panel/paczki/") or scope.get("path") in {
                    "/logowanie",
                    "/auth/verify",
                    "/auth/magic-link",
                    "/auth/logout",
                    "/panel",
                    "/panel/uwagi",
                    "/panel/rozliczenia",
                }:
                    # Chromium's native non-CORS form POST can send Origin:null
                    # under no-referrer. Fragments never enter HTTP Referer;
                    # same-origin keeps native forms usable without allowing null.
                    headers["Referrer-Policy"] = "same-origin"
                # Only the ASGI server's scheme is trusted, not raw forwarded request headers.
                if scope.get("scheme") == "https":
                    headers["Strict-Transport-Security"] = "max-age=31536000"
            await send(message)

        await self.app(scope, receive, send_with_headers)
