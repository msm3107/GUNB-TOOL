# Customer Dashboard Implementation Plan

> Use superpowers:executing-plans to implement task-by-task; implementation in current dedicated branch, independent read-only reviewer before human acceptance.

**Goal:** Stage12 of projectplan.md: authenticated customers see only their orders/packages and subscription state and can report delivered cases.

**Architecture:** Core projections and existing IssueTracker run on short guarded SQLite operations; FastAPI resolves identity, validates bounded same-origin forms and renders existing Jinja shell. No new schema, dependency, network on GET, payment or fulfillment behavior.

**Tech Stack:** Python, SQLite WAL v22, FastAPI/Jinja2, existing local CSS/assets.

**Spec:** projectplan.md, stage12; previous stage11 interfaces and user-approved branding.

## Design decisions
- Continue existing Jinja layout/nav/logo, navy/yellow/cream and Georgia/Manrope; no redesign or framework replacement.
- /panel requires actual Magic Link identity; anonymous redirects to /logowanie, storage errors503/no-store.
- Paginate independent orders and deliveries (20/page via keyset, limit+1); bounded subscription projection. Each query scopes ownership in SQL, including aggregations. Show counts new/change/full from immutable delivery items; prepared and delivered distinguished.
- No filepaths, provider IDs, raw filters, raw tokens in HTML. No download placeholder links; secure downloads are stage13.
- Subscription data explicitly last locally confirmed state, recheck expiry/trusted mapping/paid order/terminal/pause for active label. Explain test-only state, not guaranteed fresh provider truth.
- Native forms for existing POST billing proof/Portal and logout, preserve same-origin CSRF protection. Never echo UUID; PR29 recovery/retry unchanged.
- Issue form uses existing core authorization, deduplication,20/hour limit and plain-text normalization; no web-side migration/LeadRepository constructor. Error rerenders preserve benign issue fields, never billing secret; success303 PRG.

## Review focus
- Cross-customer IDs/cursors/forged HTTP identity must never expose or write another customer's data.
- Unpaid/prepared deliveries and stale/disconnected subscriptions must not claim delivered/active rights.
- Bad input, malformed old filters, unavailable DB and lock contention must fail safely without migration or stale account data.
- Large tenants and concurrent WAL writes require bounded read time, rows and no N+1.
- Native forms remain usable after errors, mobile layout and keyboard navigation; no sensitive values in URLs/cache.

## Tasks
- [x] 1. RED core tests: two tenants, delivery category counts, prepared vs delivered, cursor bounds, read-only WAL behavior, inactive subscription boundaries and issue ownership/idempotency. Implement guarded read store/projections and IssueTracker adapter; GREEN focused tests.
- [x] 2. RED HTTP tests: auth redirect/real login, tampering, bounded forms/Origin, safe error and PRG, billing/Portal integration. Implement routes/template/styles/login navigation; GREEN focused tests.
- [x] 3. Review SQL/query bounds and own diff; full pytest/lint/compile and browser synthetic mobile/native-form QA; document current behavior/limits and Product/QA audit.
- [ ] 4. Commit/push/open PR with audit block; mandatory independent full-diff review; fix verified findings and same-reviewer follow-up; CI exact final HEAD. Never merge/deploy.

## Verified implementation
- RED404->303/real session200; issue ownership initially surfaced503 through inherited OS-error translation, now403 after preserving permission outside that translation. SAVEPOINT still rolls back all denied writes.
- 29 new dashboard tests; full826 passed16 skipped35.21s, Ruff/compile/diff PASS. Schema22 unchanged.
- Native browser synthetic QA: save303 confirmation, deny undelivered403 while preserving description; no console errors;1280 and390px without horizontal overflow. Existing source branding/local assets preserved; design draft52186bba-5420-4a4d-915f-66d689a22736v2 is visual guidance, production uses current Jinja and truthful data.
- Exact billing.stripe.com form-action exception only on rendered dashboard routes, to support Chromium POST redirects; scripts/styles unchanged. Portal returns to /panel. Existing Stripe proof/API validation unchanged.

## UX-PORTAL-01 review follow-up
- Independent reviewer confirmed native Portal failure503/429/403 rendered raw JSON without navigation. Reproduced RED503 vs expected303.
- HTML Accept now redirects only to fixed /panel?notice=portal-error, no-store and retry hints; panel shows generic retry/pairing guidance. API JSON status/body unchanged. No provider URL or proof echoed.
- Three regression cases cover SDK failure, process quota and lost trusted mapping, native return+panel and JSON compatibility. Same-reviewer recheck and exact newHEAD CI required.

## UX-ISSUE-01 review follow-up
- External reviewer confirmed that snapshot failure after authenticated issue submission removed the form and unsaved fields from503 HTML. Real OS replacement lock acquired after genuine session verification reproduces the missing form (RED).
- Keep authenticated state separate from snapshot availability. Share the existing escaped issue form between ordinary panel and authenticated fallback; do not retain/render account snapshots or billing proofs during failure.
- Regression parses retained503 form values, retries those exact fields after unlock and repeats the POST; one issue is stored. Also check session-verification failure hides the form, unavailable GET shows an empty authenticated form, case/textarea injection remains escaped and billing UUID is never reflected.
- No core/SQL/schema/dependency/CSRF changes; same-reviewer follow-up and exactHEAD CI required.
