# Order Configurator Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans with TDD and independent whole-PR review.

**Goal:** Deliver PR7: Żółta Tablica package configurator and dynamic aggregate estimate from existing SQLite data, without creating orders or taking payments.

**Architecture:** Framework-free core/package_selection.py defines validated PackageFilters and aggregate selection using the v18 covering package index. app/estimator.py opens an explicitly configured existing WAL database read-only under the canonical replacement guard, one connection per operation. app/routes/orders.py exposes HTML form and bounded JSON API, with same-origin external JavaScript enhancing a native POST fallback. Reuse current brand and assets.

**Tech Stack:** Existing Python/FastAPI/Jinja/Pydantic/SQLite, standard-library form parsing, vanilla JS; no new dependencies; v18 adds a covering expression index through the existing backed-up CLI migration.

**Spec:** projectplan.md PR7, user instruction to continue after merged PR24. BASE caf2da6a29d6c9c8904bd235a30ccd13bce18269.

## Global Constraints

- /core never imports web packages. Schema v18 adds an index only; engine import/filter/delivery data and behavior stay compatible. HTTP never migrates.
- Require one explicit wojewodztwo TERYT code; at most20 selected powiaty, matching its prefix. Optional category subset uses existing LEAD_CATEGORIES. No dynamic SQL identifiers.
- branza uses existing TRADES keys. Materials/structural work have no timing restriction; other trades count cases in their estimated current stage_window, including existing big-building factor. Timing is an estimate, not proof of construction.
- Optional radius1–200km requires both WGS84 coordinates of a base in Poland (lat49–55.1, lon14–24.5). Radius counts only parcel coordinates without known municipality mismatch. Region/county/category constraints still apply.
- Count stored non-noise cases with positive/in-progress registry states; no customer delivery-history access and no claim that these are NEW or billable leads.
- Return count, parcel-coordinate quality counts and last_seen for matching imported records. Unconfigured/missing/locked/incompatible DB means unavailable503, never zero. Empty actual matches are zero.
- GUNB_WEB_DB_PATH is opt-in, independent of engine .env/config. No DB creation/migration at HTTP startup. Retain public root and liveness without DB.
- Short read transaction; mode=ro and query_only. Canonical database_replacement_lock protects connection lifetime from restore, without requiring engine lock. Concurrent read calls may fail fast503 under existing exclusive guard; no shared connection.
- Bound request body to8KiB, list lengths, SQL execution to500ms and worker concurrency; no remote I/O. Target representative100k-row estimate below100ms; verify query plan and measure, do not assert unmeasured performance.
- Brand/layout/local assets remain Żółta Tablica. Numeric prices/checkout remain unavailable. Form stores nothing, native POST and JSON POST only estimate counts.

## Review Focus

- Injection, unexpected fields, wrong types/NaN/infinite values, mismatched counties and incomplete radius must reject before querying.
- Unknown/missing DB, active replacement, wrong schema or timeout must never look like an empty market.
- Radius excludes municipal centroids and known wrong-parcel matches; exact distance refines a conservative bounding box.
- Timing boundaries must agree with core.stages.stage_window, including large buildings and fallback from decision date to application date.
- Counts must reflect the current selection; abort/ignore stale JS responses and clear stale counts on errors or edits.
- Restore guard, connection cleanup and WAL reader behavior require actual OS-lock/SQLite regressions, not mocks alone.

### Task1: Domain selection and performance

**Files:** core/package_selection.py, core/package_sql.py, core/storage.py; tests/test_package_selection.py, tests/test_storage.py.
**Interfaces:** PackageFilters(wojewodztwo, branza, powiaty=(), kategorie=LEAD_CATEGORIES, lat=None, lon=None, promien_km=None); estimate_package(conn, filters, *, today) -> PackageEstimate; list_counties(conn) -> tuple of code/name pairs. No writes.

- [x] Write failing synthetic-data tests for scoped counts/noise/status/categories, trade windows, radius/precision/mismatch and invalid filters.
- [x] Implement parameterized aggregate query with indexed TERYT ranges and bounded per-row work.
- [x] Verify EXPLAIN and representative100k-row timings plus full domain tests.

### Task2: HTTP service and validation

**Files:** app/config.py, estimator.py, routes/orders.py, main.py; tests/test_order_configurator.py.
**Interfaces:** GET /zamowienie; POST /zamowienie (native form); POST /api/estimate (JSON). WebConfig adds optional database_path from GUNB_WEB_DB_PATH.

- [x] Add failing tests for routes, read-only success, unconfigured/missing DB, guard contention, concurrent WAL writer, schema, validation/body limits, generic errors and no business writes.
- [x] Implement per-operation readonly service and bounded request handling in worker threads; preserve security/CORS behavior and liveness independence.
- [x] Verify route/service tests, including native form and protected responses.

### Task3: Branded form and publication

**Files:** app/templates/order_configurator.html, base.html, index.html, components/navbar.html; app/static/order-configurator.js, style.css; README.md; existing landing tests.

- [x] Implement semantic native controls for region/counties/trade/categories/optional radius, non-purchasing CTA and explicit availability/quality/timing explanation. Reuse logo/palette/typography; update old availability copy now that configurator exists.
- [x] Add accessible live status/error, same-origin dynamic POST, abort/ignore stale requests, and native form fallback. No inline scripts/styles.
- [x] Verify full pytest/lint, actual browser desktop/mobile/native fallback/dynamic changes/errors/assets, perf and locks. Save ignored local ledger.
- [ ] Commit/push/open next PR, verify CI exactHEAD and independent review; repair findings and leave PR open for human evaluation.

## Review corrections (UX-01, PERF-01)

- Preserve selected county options when the separate metadata read is blocked; warn by the county list while retaining a successful result. Real OS-lock regression resubmits the rendered selection.
- Profiling reproduced >100 ms on 100k parcel rows. Use a partial covering expression index for eligibility, quality, dates and geometry (v18, backup before DDL), no cache of business results and no new dependencies. SQL CASE guards the spherical predicate; a conservative interior diamond skips trig, and the remaining haversine comparison precomputes base/radius terms.
- Reproducible synthetic benchmark includes clustered/unique coordinates and a near-boundary ring. Verify unchanged radius boundaries, malformed geometry, index maintenance, v17 backup/data preservation/restart, full tests and exact-HEAD CI.
