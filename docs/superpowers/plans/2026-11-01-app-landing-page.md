# Public B2B Landing Page Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans with TDD and mandatory independent whole-branch review.

**Goal:** Implement PR6 from projectplan.md: public Polish landing page explaining the three pillars, three safe interactive examples, package comparison and FAQ.

**Architecture:** GET / renders index.html extending the existing base. Public examples are explicitly synthetic, static template content without database access. Native details/summary provides keyboard-accessible interaction without JavaScript or changes to CSP. Reuse local CSS and the ASGI factory/security boundary.

**Tech Stack:** Existing FastAPI, Jinja2, semantic HTML and vanilla CSS; no additional dependencies or external assets.

**Spec:** projectplan.md PR6; user requested the next PR after merging PR23. User confirmed comparison without numeric prices until sales launch.

## Global Constraints

- Preserve autonomous /core, SQLite schema, filtering and delivery memory unchanged.
- Keep HTTP liveness, security headers, explicit CORS and HTTPS-only HSTS unchanged.
- Use only fictional examples, visibly labeled individually and globally. No real investment IDs, names, addresses, phone numbers, coordinates, maps or private data.
- Explain strict fit, customer-level memory and explicit quality without promising guaranteed clients, contact permission, current construction progress or verified contact information.
- Registry status is a fact within the fictional scenario; trade timing is an estimate, never confirmation of work starting.
- Present Paczka jednorazowa vs Abonament as planned offers. Price: Cena przed uruchomieniem sprzedaży. No invented amounts or VAT terms.
- Orders and online payments are not yet available; links navigate only to existing page sections. No dead purchase/login links or forms.
- Follow the existing system font, ink/teal/light palette, reusable shell and local assets. Native disclosures function without JavaScript. No trackers or webfonts.

## Review Focus

- Examples must not reveal real data or be mistaken for actual leads: test labels/count and no DB access.
- Sample status and timing/quality caveats must survive collapsed details: label each summary and include explicit explanation in its body.
- Local links/assets resolve, identifiers are unique and sections have accessible names: parse rendered HTML and smoke-test every URL.
- Long text/mobile/keyboard use must remain usable: inspect desktop and narrow viewport, toggle examples and FAQ with keyboard, check console/assets/overflow.
- Replacing base.html on the root route must retain inherited layout, escaping, security and working-directory independence: update the existing foundation contract and rerun web/full suites.

### Task 1: Landing content and semantic contracts

**Files:** tests/test_landing_page.py, tests/test_app_foundation.py; app/main.py; app/templates/index.html, base.html, components/navbar.html; app/static/style.css.

**Interfaces:** create_app(config: WebConfig | None = None) remains unchanged. GET / renders index.html with page_title. Section IDs: jak-to-dziala, przyklady, oferta, pytania. Sample details use class lead-example and an individual Fikcyjny przykład label. Pricing uses a real table with caption and scoped headers.

- [x] Add tests for three synthetic native disclosures, explicit timing/data caveats, planned price comparison, no ordering, unique IDs, headings, local navigation/assets, no DB access or remote resources. Inspect the three pillar explanations during content review.
- [x] Run new/changed tests and verify failures against the preparation page.
- [x] Implement index.html and adapt shell/nav/CSS while preserving factory/security behavior.
- [x] Run landing and foundation tests; confirm inherited autoescaping and security cases remain green.

### Task 2: Verification and publication

**Files:** README.md and local ignored review ledger/report.

- [x] Document the current public page, fictional examples and sales limitation.
- [x] Run full pytest, changed-file lint, compile and diff checks; inspect real Uvicorn output and desktop/mobile browser interactions/console/assets.
- [ ] Commit and push, open PR with exact BASE/HEAD, verify CI for current HEAD.
- [ ] Obtain independent read-only review per local instructions, fix P0/P1 and fix or justify P2; save report locally and leave PR open for human evaluation.
