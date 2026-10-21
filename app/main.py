"""Punkt wejścia aplikacji FastAPI + Jinja2.

Moduł szkieletowy przygotowany pod implementację:
- /: strona główna / panel klienta
- /zamowienie: konfigurator paczki B2B (filtry branżowe, zasięg terytorialny)
- /pobierz/{token}: bezpieczne pobieranie paczek leadów (Nowe, Zmiany, Pełna baza)
- /api/webhook/stripe: obsługa zdarzeń płatności Stripe
- /zdrowie: endpoint liveness/readiness
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("app")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app():
    """Fabryka aplikacji FastAPI z konfiguracją szablonów Jinja2 i plików statycznych."""
    try:
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
    except ImportError:
        raise RuntimeError(
            "FastAPI nie jest zainstalowane. Zainstaluj zależności webowe: pip install fastapi uvicorn jinja2"
        )

    app = FastAPI(
        title="GUNB Leads B2B",
        description="Portal B2B dla handlowców – inwestycje budowlane z rejestru RWDZ",
        version="0.1.0",
    )

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/zdrowie")
    def health_check() -> dict[str, str]:
        return {"status": "ok", "service": "gunb-web"}

    return app


if __name__ == "__main__":
    try:
        import uvicorn
        uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=8000, reload=True)
    except ImportError:
        print("Uruchomienie wymaga pakietu uvicorn: pip install uvicorn")
