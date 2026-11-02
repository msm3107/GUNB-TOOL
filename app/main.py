"""ASGI factory for the B2B web foundation, without engine startup or database access."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from core.auth_store import AuthStore
from core.checkout_store import CheckoutStore
from core.paid_packages import PaidPackagePreparer
from core.payment_store import PaymentStore
from core.subscription_store import SubscriptionStore

from .auth import AuthService
from .config import WebConfig
from .estimator import Estimator
from .identity import IdentityResolver
from .portal_service import PortalService
from .routes.auth import auth_router
from .routes.checkout import checkout_router
from .routes.orders import order_router
from .routes.portal import portal_router
from .routes.stripe_webhook import stripe_webhook_router
from .security import SecurityHeadersMiddleware
from .stripe_service import CheckoutService
from .stripe_webhook_service import StripeWebhookService
from .subscription_service import SubscriptionService

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


async def _server_error(_request: Request, _exception: Exception) -> JSONResponse:
    return JSONResponse(
        {"detail": "Wewnętrzny błąd serwera"},
        status_code=500,
        headers={"Cache-Control": "no-store"},
    )


def create_app(
    config: WebConfig | None = None,
    *,
    checkout_service: CheckoutService | None = None,
    subscription_service: SubscriptionService | None = None,
    portal_service: PortalService | None = None,
    identity_resolver: IdentityResolver | None = None,
    auth_service: AuthService | None = None,
) -> ASGIApp:
    """Build FastAPI with CORS/security policy covering errors as well as routes.

    The returned ASGI application runs with ``uvicorn app.main:create_app --factory``.
    HTTP liveness deliberately does not inspect engine configuration, locks or SQLite.
    """
    config = WebConfig.from_env() if config is None else config
    auth_service = auth_service or AuthService(
        AuthStore(config.database_path), config.auth
    )
    subscriptions = subscription_service or SubscriptionService(
        SubscriptionStore(config.database_path), config.checkout
    )
    api = FastAPI(
        title="Żółta Tablica",
        description="Portal B2B dla handlowców – inwestycje budowlane z rejestru RWDZ",
        version="0.1.0",
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    api.add_exception_handler(Exception, _server_error)
    # Windows registry mappings can otherwise serve these assets as octet-stream.
    mimetypes.add_type("image/webp", ".webp")
    mimetypes.add_type("font/woff2", ".woff2")
    api.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    api.include_router(order_router(Estimator(config.database_path), templates))
    api.include_router(auth_router(auth_service, subscriptions, templates))
    api.include_router(
        portal_router(
            portal_service
            or PortalService(SubscriptionStore(config.database_path), config.checkout),
            identity_resolver or auth_service.resolve,
        )
    )
    api.include_router(
        checkout_router(
            checkout_service
            or CheckoutService(CheckoutStore(config.database_path), config.checkout),
            templates,
        )
    )
    api.include_router(
        stripe_webhook_router(
            StripeWebhookService(
                PaymentStore(config.database_path),
                config.webhook_secret,
                enabled=config.checkout.test_mode,
                subscription_service=subscriptions,
            ),
            PaidPackagePreparer(config.database_path, config.test_package_dir)
            if config.database_path is not None and config.webhook_secret
            else None,
        )
    )

    @api.get("/", response_class=HTMLResponse, name="home", include_in_schema=False)
    def home(request: Request) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"page_title": "Inwestycje z RWDZ dla Twojej branży"},
        )

    @api.api_route("/zdrowie", methods=["GET", "HEAD"], include_in_schema=False)
    async def health_check(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "no-store"
        return {"status": "ok", "service": "gunb-web"}

    cors = CORSMiddleware(
        api,
        allow_origins=config.cors_origins,
        allow_methods=("GET", "HEAD"),
        allow_headers=(),
        allow_credentials=False,
    )
    return SecurityHeadersMiddleware(cors)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=8000)
