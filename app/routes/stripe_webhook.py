"""Bounded raw-body webhook ingress, durable receipt before HTTP acknowledgement."""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from app.stripe_webhook_service import StripeWebhookService
from core.checkout_store import CheckoutConflict, CheckoutUnavailable
from core.paid_packages import PaidPackagePreparer

log = logging.getLogger(__name__)
BODY_LIMIT = 128 * 1024


def stripe_webhook_router(
    service: StripeWebhookService, preparer: PaidPackagePreparer | None
) -> APIRouter:
    router = APIRouter()

    def response(code: int, background=None):
        return JSONResponse(
            {"received": code == 200},
            status_code=code,
            background=background,
            headers={
                "Cache-Control": "no-store",
                **({"Retry-After": "60"} if code == 503 else {}),
            },
        )

    def prepare(order_id: int) -> None:
        try:
            if preparer is not None:
                preparer.prepare(order_id)
        except Exception:  # noqa: BLE001 - response has been sent; durable lease enables recovery
            # ACK already sent; a durable pending/expired job is recovered by CLI.
            log.warning(
                "Test package background preparation interrupted; order_id=%s", order_id
            )

    @router.post("/api/webhook/stripe", include_in_schema=False)
    async def webhook(request: Request):
        if not service.enabled:
            return response(503)
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return response(415)
        signatures = request.headers.getlist("stripe-signature")
        if len(signatures) != 1 or len(signatures[0]) > 4096:
            return response(400)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > BODY_LIMIT:
                return response(413)
            raw.extend(chunk)
        try:
            order = await run_in_threadpool(service.receive, bytes(raw), signatures[0])
        except CheckoutUnavailable:
            return response(503)
        except CheckoutConflict:
            return response(409)
        except ValueError:
            return response(400)
        return response(
            200, BackgroundTask(prepare, order) if order is not None else None
        )

    return router
