"""Web foundation tests use in-process ASGI requests and synthetic failures only."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_ORIGIN = "https://portal.example.test"


@pytest.fixture(autouse=True)
def isolated_web_config(monkeypatch):
    monkeypatch.delenv("GUNB_WEB_CORS_ORIGINS", raising=False)


@pytest.fixture
def client():
    with TestClient(create_app(), raise_server_exceptions=False) as http:
        yield http


def configured_app(origins=(ALLOWED_ORIGIN,)):
    from app.config import WebConfig

    return create_app(WebConfig(cors_origins=origins))


def assert_security_headers(response):
    policy = response.headers["content-security-policy"]
    for directive in (
        "default-src 'self'", "object-src 'none'", "frame-ancestors 'none'", "base-uri 'none'"
    ):
        assert directive in policy
    assert "'unsafe-inline'" not in policy and "'unsafe-eval'" not in policy
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"


def test_liveness_retains_payload_without_opening_database(monkeypatch, tmp_path):
    def forbidden_database(*args, **kwargs):
        raise AssertionError("HTTP liveness must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden_database)
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as http:
        response = http.get("/zdrowie")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "gunb-web"}
    assert response.headers["cache-control"] == "no-store"
    assert not (tmp_path / "data").exists()


def test_root_renders_reusable_polish_layout(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.template.name == "index.html"
    assert '<html lang="pl">' in response.text
    assert "Portal w przygotowaniu" in response.text
    assert "<nav" in response.text and "<main" in response.text and "<footer" in response.text
    assert 'href="#main-content"' in response.text
    assert '/static/style.css' in response.text
    assert "<script" not in response.text and 'style="' not in response.text


@pytest.mark.parametrize("path,status", (
    ("/", 200), ("/zdrowie", 200), ("/missing", 404), ("/static/missing.css", 404),
))
def test_security_headers_cover_success_and_missing_resources(client, path, status):
    response = client.get(path)
    assert response.status_code == status
    assert_security_headers(response)
    assert "strict-transport-security" not in response.headers


def test_method_errors_keep_security_headers(client):
    response = client.post("/zdrowie")
    assert response.status_code == 405
    assert_security_headers(response)


def test_hsts_is_https_only_and_ignores_untrusted_forwarded_header():
    with TestClient(create_app(), base_url="https://testserver") as http:
        response = http.get("/zdrowie")
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    with TestClient(create_app(), base_url="http://testserver") as http:
        response = http.get("/zdrowie", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in response.headers


@pytest.mark.parametrize("path", ("/docs", "/redoc", "/openapi.json"))
def test_public_api_docs_are_disabled(client, path):
    assert client.get(path).status_code == 404


def test_static_files_and_templates_are_independent_of_working_directory(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as http:
        home = http.get("/")
        css = http.get("/static/style.css")
    assert home.status_code == css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert_security_headers(css)
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("path", ("/static/%2e%2e/main.py", "/static/%2e%2e/%2e%2e/config.yaml"))
def test_static_mount_does_not_expose_files_outside_assets(client, path):
    response = client.get(path)
    assert response.status_code == 404
    assert_security_headers(response)


def test_real_base_template_and_child_content_autoescape_untrusted_values(client):
    import app.main as web_main

    response = client.get("/")
    payload = '<script>alert("synthetic")</script>'
    context = dict(response.context, page_title=payload)
    rendered = response.template.render(context)
    assert payload not in rendered and "&lt;script&gt;" in rendered
    child = web_main.templates.env.from_string(
        "{% extends 'base.html' %}{% block content %}<p>{{ body }}</p>{% endblock %}"
    )
    rendered = child.render(dict(response.context, body=payload))
    assert payload not in rendered and "&lt;script&gt;" in rendered
    assert "<nav" in rendered and "<main" in rendered


def test_cors_is_closed_by_default(client):
    response = client.get("/zdrowie", headers={"Origin": ALLOWED_ORIGIN})
    assert "access-control-allow-origin" not in response.headers
    preflight = client.options("/zdrowie", headers={
        "Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "GET"
    })
    assert preflight.status_code == 400
    assert "access-control-allow-origin" not in preflight.headers
    assert_security_headers(preflight)


def test_cors_explicit_origin_and_read_methods_only():
    with TestClient(configured_app()) as http:
        allowed = http.get("/zdrowie", headers={"Origin": ALLOWED_ORIGIN})
        denied = http.get("/zdrowie", headers={"Origin": "https://other.example.test"})
        preflight = http.options("/zdrowie", headers={
            "Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "GET"
        })
        unsafe_method = http.options("/zdrowie", headers={
            "Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "POST"
        })
    assert allowed.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "origin" in allowed.headers["vary"].lower()
    assert "access-control-allow-credentials" not in allowed.headers
    assert "access-control-allow-origin" not in denied.headers
    assert preflight.status_code == 200 and unsafe_method.status_code == 400
    assert_security_headers(preflight)
    assert_security_headers(unsafe_method)


def test_server_error_is_generic_and_preserves_cors_and_security_headers(monkeypatch):
    import app.main as web_main

    def fail_rendering(*args, **kwargs):
        raise RuntimeError("synthetic-private-details")

    monkeypatch.setattr(web_main.templates, "TemplateResponse", fail_rendering)
    with TestClient(configured_app(), raise_server_exceptions=False) as http:
        response = http.get("/", headers={"Origin": ALLOWED_ORIGIN})
    assert response.status_code == 500
    assert response.json() == {"detail": "Wewnętrzny błąd serwera"}
    assert "synthetic-private-details" not in response.text and "Traceback" not in response.text
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["cache-control"] == "no-store"
    assert_security_headers(response)


@pytest.mark.parametrize("origin", (
    "https://portal.example.test", "http://localhost:3000", "http://[::1]:8000"
))
def test_web_config_accepts_explicit_origins(origin):
    from app.config import WebConfig

    assert WebConfig(cors_origins=(origin,)).cors_origins == (origin,)


@pytest.mark.parametrize("origin", (
    "*", "null", "https://*.example.test", "ftp://example.test", "https://example.test/",
    "https://example.test/path", "https://user:fake@example.test", "https://example.test?q=x",
    "https://example.test#fragment", "https://example.test:bad", "https://example.test:99999",
    "https://example.test\r\nX-Test: x", "https://example.test\\bad", "https://łódź.example.test",
    "", 123,
))
def test_web_config_rejects_non_origin_or_wildcard_values(origin):
    from app.config import WebConfig

    with pytest.raises(ValueError):
        WebConfig(cors_origins=(origin,))


def test_configured_origins_load_from_environment(monkeypatch):
    from app.config import WebConfig

    raw = " https://portal.example.test, http://localhost:3000 "
    config = WebConfig.from_env({"GUNB_WEB_CORS_ORIGINS": raw})
    assert config.cors_origins == (ALLOWED_ORIGIN, "http://localhost:3000")
    monkeypatch.setenv("GUNB_WEB_CORS_ORIGINS", raw)
    with TestClient(create_app()) as http:
        response = http.get("/zdrowie", headers={"Origin": ALLOWED_ORIGIN})
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    monkeypatch.setenv("GUNB_WEB_CORS_ORIGINS", "*")
    with pytest.raises(ValueError):
        create_app()


def test_engine_cli_imports_without_web_stack_or_app_package():
    script = """
import importlib.abc
import runpy
import sys
class BlockWeb(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'app', 'fastapi', 'starlette', 'jinja2', 'uvicorn', 'httpx', 'httpx2'}:
            raise ImportError('Forbidden web import: ' + fullname)
sys.meta_path.insert(0, BlockWeb())
import core.issue_tracker
import core.export_package
import core.package_selection
sys.argv = ['main.py', '--help']
runpy.run_path('main.py', run_name='__main__')
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", script], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--fetch" in result.stdout and "Forbidden web import" not in result.stderr
