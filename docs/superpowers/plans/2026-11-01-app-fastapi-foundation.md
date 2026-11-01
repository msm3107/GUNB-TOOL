# FastAPI Foundation Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans with TDD and the repository's mandatory independent review.

**Goal:** Implement PR5 from projectplan.md: a reusable web foundation, template layout, static CSS and safe HTTP responses.

**Architecture:** Keep app.main:create_app as an ASGI factory. It builds FastAPI routes and wraps the whole application in explicit CORS and stateless security-header middleware, including server errors. The root route renders a minimal preparation notice from the reusable layout; the actual landing page remains PR6. The HTTP liveness route does not inspect the engine or database.

**Tech Stack:** Python >=3.10, FastAPI, Starlette middleware, Jinja2, Uvicorn; HTTPX2 for in-process tests only (the current Starlette test client).

**Spec:** projectplan.md, PR5. Existing app/main.py factory and /zdrowie payload remain supported.

## Global Constraints

- No HTTP/template imports in /core, no database migration or engine behavior change.
- Runtime dependencies and lock remain ASCII; retain existing engine pins and generate new pins in a clean environment.
- CSP permits same-origin resources only, blocks inline code, objects, framing and base tags. Add nosniff, DENY framing, no-referrer and a restrictive Permissions-Policy.
- HSTS max-age=31536000 only for ASGI HTTPS; do not infer scheme from untrusted forwarded headers.
- CORS denies cross-origin access by default; GUNB_WEB_CORS_ORIGINS accepts only explicit ASCII HTTP(S) origins, no wildcard/credentials/path/query/fragment. Credentials remain disabled.
- HTTP /zdrowie returns status=ok, service=gunb-web without reading the engine configuration, database or locks. Document liveness vs engine diagnostics.
- Unhandled 500 responses must be generic and carry the same CORS/security headers; disable debug and public API docs for this preparatory stage.
- Templates and static files resolve relative to the package, not the current directory. Templates use HTML autoescaping, external CSS and no external assets.
- No customer forms, checkout, authentication, live data or production deployment in this stage.

## Review Focus

- Security and CORS headers must cover 200, 404, 405, preflight and unhandled 500 responses.
- Raw X-Forwarded-Proto must not turn HTTP into HTTPS for HSTS; HTTPS still gets HSTS.
- Invalid origin configuration fails closed and disallowed origins/methods receive no permission.
- An arbitrary working directory or a static traversal request must not expose Python/configuration files or break valid assets.
- The engine CLI must work when web packages and the app package are blocked from import.

### Task 1: Dependencies and HTTP security foundation

**Files:** requirements.txt, requirements.lock, requirements-dev.txt, app/config.py, app/security.py, app/main.py, tests/test_app_foundation.py.

**Interfaces:** WebConfig(cors_origins: tuple[str, ...] = ()); WebConfig.from_env(environ: Mapping[str, str] | None = None). create_app(config: WebConfig | None = None) -> ASGIApp; SecurityHeadersMiddleware(app: ASGIApp).

- [x] Add framework runtime dependencies and HTTPX2 test dependency, preserving old runtime pins and ASCII.
- [x] Add failing tests for root rendering, liveness, security/HSTS/CORS/error policy and configuration validation.
- [x] Implement config, ASGI middleware and factory with a generic 500 handler; verify RED→GREEN.

### Task 2: Reusable templates, CSS and independence

**Files:** app/templates/base.html, app/templates/components/navbar.html, app/static/style.css, tests/test_app_foundation.py, README.md.

**Interfaces:** base.html exposes page_title/title and content blocks, includes the navbar and same-origin stylesheet. / returns only a preparation notice. /zdrowie retains its JSON shape; /static/style.css serves the packaged CSS.

- [x] Add tests for inherited templates, autoescaping, CSS, alternate working directory, traversal and web-independent engine CLI.
- [x] Implement the semantic Polish layout, keyboard skip link, responsive local CSS and factory URLs; verify focused tests.
- [x] Document starting Uvicorn, CORS configuration, proxy/HSTS trust boundary and liveness semantics.

### Task 3: Verification and PR

- [x] Run full pytest, pip check, changed-file Ruff, compileall, dependency ASCII and diff checks; smoke-test real local HTTP and inspect the layout if browser tooling is available.
- [ ] Commit, push and open a PR with exact BASE/HEAD, run independent read-only review and check CI for that HEAD.
- [ ] Fix P0/P1 and fix or justify P2, save reports locally and leave the PR open for human review.

## References checked 2026-11-01

- https://fastapi.tiangolo.com/advanced/templates/
- https://starlette.dev/middleware/ (global CORS enforcement and pure ASGI middleware)
- https://pypi.org/project/fastapi/
- https://pypi.org/project/uvicorn/
- https://pypi.org/project/Jinja2/
- https://pypi.org/project/httpx/
- https://starlette.dev/testclient/
- https://pypi.org/project/httpx2/
