"""Owner-only, retryable download links and owned-handle streaming."""

import hashlib
import hmac
import re
import secrets
import threading
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.identity import AuthenticatedCustomer
from app.routes.auth import AuthInputError, _input
from core.checkout_store import CheckoutUnavailable
from core.package_access import DownloadBusy, PackageNotFound
from core.package_files import CHUNK_SIZE, ArtifactUnavailable, open_verified

MEDIA = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv; charset=utf-8",
}


def utc_now():
    return datetime.now(timezone.utc)


class _Verification:
    """Worker owns its result and slot until the request explicitly takes them.

    Direct asyncio cancellation can abandon a running threadpool await. Keep
    cleanup in the worker too, and retain its slot until the file is closed.
    """

    def __init__(self, root, artifact, release):
        self.root, self.artifact, self.release = root, artifact, release
        self._lock = threading.Lock()
        self._started = self._finished = self._abandoned = self._released = False
        self._opened = None

    def _dispose(self, opened):
        # Caller holds _lock. Closing read-only handles performs no network IO.
        try:
            if opened is not None:
                opened.close()
        finally:
            if not self._released:
                self._released = True
                self.release()

    def run(self):
        with self._lock:
            if self._abandoned:
                return
            self._started = True
        opened = None
        try:
            opened = open_verified(self.root, self.artifact)
        finally:
            with self._lock:
                self._finished = True
                if self._abandoned:
                    self._dispose(opened)
                else:
                    self._opened = opened

    def take(self):
        with self._lock:
            if self._abandoned or not self._finished or self._opened is None:
                raise ArtifactUnavailable()
            opened, self._opened = self._opened, None
            return opened

    def abandon(self):
        with self._lock:
            self._abandoned = True
            if not self._started or self._finished:
                self._dispose(self._opened)
                self._opened = None


class ArtifactResponse(StreamingResponse):
    def __init__(self, opened, release, **kwargs):
        self.opened, self.release = opened, release

        def chunks():
            while chunk := opened.stream.read(CHUNK_SIZE):
                yield chunk

        super().__init__(chunks(), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                self.opened.close()
            finally:
                self.release()


def downloads_router(store, root, identity, auth, templates):
    router = APIRouter()
    key = (
        bytes.fromhex(auth.settings.secret_key)
        if auth.settings.enabled
        else secrets.token_bytes(32)
    )
    peer_key = secrets.token_bytes(32)
    slots = threading.BoundedSemaphore(4)

    def sign(value):
        return hmac.new(
            key, b"zt:download:" + value.encode(), hashlib.sha256
        ).hexdigest()

    def valid(token):
        return re.fullmatch(
            r"[A-Za-z0-9_-]{43}\.[0-9a-f]{64}", token
        ) is not None and hmac.compare_digest(
            token.split(".")[1], sign(token.split(".")[0])
        )

    def page(request, status, message, *, login=False, grant=None):
        return templates.TemplateResponse(
            request=request,
            name="download_status.html",
            context={
                "page_title": "Pobieranie paczki",
                "message": message,
                "login": login,
                "grant": grant,
            },
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                **({"Retry-After": "60"} if status in (429, 503) else {}),
            },
        )

    async def principal(request):
        owner = await run_in_threadpool(identity, request)
        return owner if isinstance(owner, AuthenticatedCustomer) else None

    async def audit(request, token_hash, owner, outcome):
        peer = request.client.host if request.client else ""
        peer_hash = hmac.new(peer_key, peer.encode(), hashlib.sha256).hexdigest()
        agent_hash = hmac.new(
            peer_key,
            request.headers.get("user-agent", "")[:4096].encode(),
            hashlib.sha256,
        ).hexdigest()
        await run_in_threadpool(
            store.audit,
            owner.customer_id if owner else None,
            token_hash,
            request.method,
            outcome,
            peer_hash,
            agent_hash,
            now=utc_now(),
        )

    @router.post(
        "/panel/paczki/{delivery_id}/link",
        include_in_schema=False,
        name="download_link",
    )
    async def issue(request: Request, delivery_id: int):
        if (
            request.headers.get("origin") != auth.settings.origin
            or not auth.settings.enabled
        ):
            return page(
                request,
                403,
                "Nie można potwierdzić pochodzenia formularza. Wróć do panelu.",
            )
        try:
            owner = await principal(request)
            if owner is None:
                return page(
                    request, 401, "Zaloguj się, aby pobrać własną paczkę.", login=True
                )
            fmt = await _input(request, "format")
            if fmt not in MEDIA:
                raise AuthInputError(422)
            if not 1 <= delivery_id < 2**63:
                raise PackageNotFound
            value = secrets.token_urlsafe(32)
            token = value + "." + sign(value)
            await run_in_threadpool(
                store.issue,
                owner.customer_id,
                delivery_id,
                fmt,
                hashlib.sha256(token.encode()).hexdigest(),
                now=utc_now(),
            )
            return RedirectResponse(
                "/pobierz/" + token,
                status_code=303,
                headers={"Cache-Control": "no-store"},
            )
        except AuthInputError as exc:
            return page(request, exc.code, "Wybierz format XLSX albo CSV w panelu.")
        except PackageNotFound:
            return page(request, 404, "Paczka jest niedostępna dla tego konta.")
        except DownloadBusy:
            return page(
                request,
                429,
                "Wydano wiele linków. Użyj ostatniego lub spróbuj za minutę.",
            )
        except CheckoutUnavailable:
            return page(
                request, 503, "Pobieranie jest chwilowo niedostępne. Spróbuj ponownie."
            )

    @router.get(
        "/panel/paczki/{delivery_id}/link",
        include_in_schema=False,
        name="download_renew",
    )
    async def renew(request: Request, delivery_id: int):
        try:
            owner = await principal(request)
            if owner is None:
                return page(
                    request,
                    401,
                    "Zaloguj się i wróć do tej karty, aby odnowić link.",
                    login=True,
                )
            fmt = request.query_params.get("format", "")
            if fmt not in MEDIA or not 1 <= delivery_id < 2**63:
                raise PackageNotFound
            grant = await run_in_threadpool(
                store.artifact_for, owner.customer_id, delivery_id, fmt
            )
            return page(
                request,
                410,
                "Link wygasł. Wydaj nowy link ważny przez 15 minut.",
                grant=grant,
            )
        except PackageNotFound:
            return page(request, 404, "Paczka jest niedostępna dla tego konta.")
        except CheckoutUnavailable:
            return page(
                request, 503, "Pobieranie jest chwilowo niedostępne. Spróbuj ponownie."
            )

    @router.api_route(
        "/pobierz/{token}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
        name="download_package",
    )
    async def download(request: Request, token: str):
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        opened, acquired, transferred = None, False, False
        verification = None
        owner = None
        try:
            owner = await principal(request)
            if owner is None:
                await audit(request, token_hash, None, "denied")
                return page(
                    request,
                    401,
                    "Zaloguj się w nowej karcie, wróć tutaj i ponów pobranie.",
                    login=True,
                )
            if not valid(token):
                raise PackageNotFound
            grant = await run_in_threadpool(
                store.resolve, owner.customer_id, token_hash
            )
            if int(utc_now().timestamp()) >= grant.expires_at:
                await audit(request, token_hash, owner, "expired")
                if request.method == "HEAD":
                    return Response(
                        status_code=410, headers={"Cache-Control": "no-store"}
                    )
                # Strip the secret path before a native renewal form. This page
                # can use same-origin referrers without exposing the token.
                return RedirectResponse(
                    f"/panel/paczki/{grant.delivery_id}/link?format={grant.artifact.format}",
                    status_code=303,
                    headers={"Cache-Control": "no-store"},
                )
            if not slots.acquire(blocking=False):
                await audit(request, token_hash, owner, "busy")
                return page(
                    request, 429, "Trwają inne pobrania. Spróbuj ponownie za chwilę."
                )
            acquired = True
            if root is None:
                raise ArtifactUnavailable()
            verification = _Verification(root, grant.artifact, slots.release)
            await run_in_threadpool(verification.run)
            opened = verification.take()
            verification = None  # request now owns both the handle and its slot
            await audit(
                request,
                token_hash,
                owner,
                "head" if request.method == "HEAD" else "requested",
            )
            headers = {
                "Content-Disposition": f'attachment; filename="zolta-tablica-paczka-{grant.delivery_id}.{grant.artifact.format}"',
                "Content-Length": str(grant.artifact.size),
                "Cache-Control": "no-store",
            }
            if request.method == "HEAD":
                return Response(
                    b"", media_type=MEDIA[grant.artifact.format], headers=headers
                )
            response = ArtifactResponse(
                opened,
                slots.release,
                media_type=MEDIA[grant.artifact.format],
                headers=headers,
            )
            transferred = True
            return response
        except PackageNotFound:
            try:
                await audit(request, token_hash, owner, "denied")
            except CheckoutUnavailable:
                return page(
                    request,
                    503,
                    "Pobieranie jest chwilowo niedostępne. Spróbuj ponownie.",
                )
            return page(request, 404, "Paczka jest niedostępna dla tego konta.")
        except ArtifactUnavailable:
            try:
                await audit(request, token_hash, owner, "unavailable")
            except CheckoutUnavailable:
                pass
            return page(
                request,
                503,
                "Plik jest chwilowo niedostępny. Ponów pobranie po jego przywróceniu.",
            )
        except CheckoutUnavailable:
            return page(
                request, 503, "Pobieranie jest chwilowo niedostępne. Spróbuj ponownie."
            )
        finally:
            if verification is not None:
                acquired = False  # worker releases after closing its abandoned result
                verification.abandon()
            if not transferred:
                try:
                    if opened is not None:
                        opened.close()
                finally:
                    if acquired:
                        slots.release()

    return router
