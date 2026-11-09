"""Private immutable package view and contextual feedback, with native retry."""

from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.identity import AuthenticatedCustomer
from app.routes.auth import AuthInputError
from app.routes.dashboard import ISSUE_TYPES, _issue_input
from core.checkout_store import CheckoutUnavailable
from core.issue_tracker import IssueNotAllowed, IssueRateLimitExceeded
from core.package_access import DownloadBusy, PackageNotFound
from core.package_preview import PreviewUnavailable

LABELS = {"all": "Wszystkie", "new": "Nowe", "change": "Zmiany", "full": "Pełna baza"}


def utc_now():
    return datetime.now(timezone.utc)


def _view(page, category, case_id=None):
    if (
        not isinstance(page, str)
        or not page.isascii()
        or not page.isdigit()
        or len(page) > 3
        or not 1 <= int(page) <= 200
        or category not in LABELS
    ):
        raise ValueError
    if case_id is not None and (not case_id or len(case_id) > 512):
        raise ValueError
    return {"page": int(page), "category": category, "case_id": case_id}


def packages_router(preview, issues, identity, auth, templates):
    router = APIRouter()

    async def principal(request):
        owner = await run_in_threadpool(identity, request)
        return owner if isinstance(owner, AuthenticatedCustomer) else None

    def location(delivery_id, page, category, case_id=None, notice=None):
        query = {"page": page, "category": category}
        if case_id is not None:
            query["case_id"] = case_id
        if notice:
            query["notice"] = notice
        return f"/panel/paczki/{delivery_id}?" + urlencode(query)

    async def render(
        request,
        delivery_id,
        owner=None,
        *,
        view=None,
        values=None,
        status=200,
        message="",
        login=False,
    ):
        snapshot = None
        history = None
        if owner is not None and view is not None:
            try:
                snapshot = await run_in_threadpool(
                    preview.read, owner.customer_id, delivery_id, **view
                )
                if snapshot.selected:
                    history = await run_in_threadpool(
                        preview.history, owner.customer_id, snapshot.selected.case_id
                    )
            except PackageNotFound:
                status, message = (
                    404,
                    "Paczka lub sprawa jest niedostępna dla tego konta.",
                )
            except DownloadBusy:
                status, message = (
                    429,
                    "Trwają inne odczyty. Spróbuj ponownie za chwilę.",
                )
            except (CheckoutUnavailable, PreviewUnavailable):
                status, message = (
                    503,
                    "Nie można teraz odczytać zapisanej paczki. Spróbuj ponownie.",
                )
            except ValueError:
                status, message = (
                    422,
                    "Niepoprawna strona lub filtr. Wybierz widok paczki ponownie.",
                )
        form_values = values or (
            {"case_id": snapshot.selected.case_id}
            if snapshot and snapshot.selected
            else {}
        )
        selected_view = view or {"page": 1, "category": "all"}
        context = {
            key: str(form_values.get(key, selected_view[key]))
            for key in ("page", "category")
        }
        if (
            snapshot
            and history
            and request.query_params.get("notice") == "issue"
            and not message
        ):
            message = "Zgłoszenie zapisano lub było już przyjęte. Wymaga weryfikacji."
        return templates.TemplateResponse(
            request=request,
            name="package_detail.html",
            context={
                "page_title": "Twoja paczka",
                "package": snapshot,
                "delivery_id": delivery_id,
                "message": message,
                "login": login,
                "issue_values": form_values,
                "issue_context": context,
                "issue_action": f"/panel/paczki/{delivery_id}/uwagi",
                "issue_types": ISSUE_TYPES,
                "category_labels": LABELS,
                "history": history,
                "selected_category": selected_view["category"],
                "page_link": lambda n: location(
                    delivery_id, n, selected_view["category"]
                ),
                "case_link": lambda case: (
                    location(
                        delivery_id,
                        selected_view["page"],
                        selected_view["category"],
                        case,
                    )
                    + "#issue-heading"
                ),
            },
            status_code=status,
            headers={
                "Cache-Control": "no-store",
                **({"Retry-After": "60"} if status in (429, 503) else {}),
            },
        )

    @router.get(
        "/panel/paczki/{delivery_id}", include_in_schema=False, name="package_detail"
    )
    async def detail(request: Request, delivery_id: int):
        try:
            owner = await principal(request)
            if owner is None:
                return RedirectResponse(
                    "/logowanie", status_code=303, headers={"Cache-Control": "no-store"}
                )
            if not 1 <= delivery_id < 2**63:
                raise PackageNotFound
            if any(
                len(request.query_params.getlist(key)) > 1
                for key in ("page", "category", "case_id", "notice")
            ):
                raise ValueError
            view = _view(
                request.query_params.get("page", "1"),
                request.query_params.get("category", "all"),
                request.query_params.get("case_id"),
            )
            return await render(request, delivery_id, owner, view=view)
        except PackageNotFound:
            return await render(
                request,
                delivery_id,
                status=404,
                message="Paczka jest niedostępna dla tego konta.",
            )
        except CheckoutUnavailable:
            return await render(
                request,
                delivery_id,
                status=503,
                message="Nie można teraz potwierdzić sesji. Spróbuj ponownie.",
            )
        except ValueError:
            return await render(
                request,
                delivery_id,
                status=422,
                message="Niepoprawna strona lub filtr paczki.",
            )

    @router.post(
        "/panel/paczki/{delivery_id}/uwagi",
        include_in_schema=False,
        name="package_issue",
    )
    async def report(request: Request, delivery_id: int):
        if (
            not auth.settings.enabled
            or request.headers.get("origin") != auth.settings.origin
        ):
            return await render(
                request,
                delivery_id,
                status=403,
                message="Niepoprawne pochodzenie formularza. Otwórz panel ponownie.",
            )
        values = {}
        view = None
        owner = None
        try:
            values = await _issue_input(request, contextual=True)
            if (
                len(values["case_id"]) > 512
                or len(values["description"]) > 1000
                or values["issue_type"] not in ISSUE_TYPES
            ):
                raise ValueError
            view = _view(values["page"], values["category"], values["case_id"])
            if not 1 <= delivery_id < 2**63:
                raise PackageNotFound
            owner = await principal(request)
            if owner is None:
                return await render(
                    request,
                    delivery_id,
                    status=401,
                    message="Zaloguj się w nowej karcie i ponów zapis. Pola formularza zostały zachowane.",
                    values=values,
                    login=True,
                )
            source = await run_in_threadpool(
                preview.read, owner.customer_id, delivery_id, **view
            )
            await run_in_threadpool(
                issues.report_issue,
                owner.customer_id,
                values["case_id"],
                values["issue_type"],
                values["description"],
                now=utc_now(),
                delivery_id=delivery_id,
                artifact_sha256=source.artifact_sha256,
            )
            return RedirectResponse(
                location(
                    delivery_id,
                    view["page"],
                    view["category"],
                    values["case_id"],
                    "issue",
                )
                + "#issue-heading",
                status_code=303,
                headers={"Cache-Control": "no-store"},
            )
        except AuthInputError as exc:
            return await render(
                request,
                delivery_id,
                status=exc.code,
                message="Niepoprawny lub zbyt duży formularz.",
            )
        except ValueError:
            return await render(
                request,
                delivery_id,
                status=422,
                message="Sprawdź numer sprawy, typ uwagi, opis i widok paczki.",
                values={
                    k: v[: 1000 if k == "description" else 512]
                    for k, v in values.items()
                },
            )
        except (PackageNotFound, IssueNotAllowed):
            return await render(
                request,
                delivery_id,
                status=404,
                message="Nie potwierdzono uprawnień do sprawy w tej paczce.",
                values=values,
            )
        except (DownloadBusy, IssueRateLimitExceeded):
            return await render(
                request,
                delivery_id,
                owner,
                view=view,
                values=values,
                status=429,
                message="Osiągnięto limit. Formularz zachowano; spróbuj później.",
            )
        except (CheckoutUnavailable, PreviewUnavailable):
            return await render(
                request,
                delivery_id,
                owner,
                view=view,
                values=values,
                status=503,
                message="Zapis jest chwilowo niedostępny. Formularz zachowano; ponów zapis.",
            )

    return router
