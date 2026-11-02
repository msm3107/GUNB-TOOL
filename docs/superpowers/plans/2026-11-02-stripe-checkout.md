# Stripe Checkout Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline with TDD and one independent whole-PR review.

**Goal:** PR8: create Stripe test Checkout sessions for one-off packages and subscriptions, while public sales stay disabled until prices/terms and later delivery/auth stages are ready.

**Architecture:** Framework-free core checkout ledger records pending orders and immutable attempt snapshots in schema v19. App service reserves an attempt in a short transaction, calls Stripe outside all DB transactions/connections, then persists its session. Bounded same-origin JSON route redirects to Stripe; generic success page neither asserts payment nor grants access. Existing UI stays informational.

**Tech Stack:** Python/FastAPI/Jinja/SQLite; official stripe>=10,<11 as required by projectplan.md, pinned10.12.0. Existing requests/typing dependencies suffice.

**Spec:** projectplan.md PR8; continuation after merged PR25 (BASE5c137c3dd43f74ecee7df929de2e6029bd35ba3d). User has approved comparison without amounts; no approved sales prices/VAT yet.

## Global constraints / rulings

- No live keys/charges, no webhook/paid-state/fulfillment or authentication implementation in this stage. Only explicit test mode can create sessions; disabled by default. No real Stripe calls during agent verification.
- Server-configured final test amounts in integer PLN grosze, quantity1; subscription interval explicitly configured, no invented production prices. Frozen snapshot per attempt makes retries independent of changed config.
- Request: email, package kind, nested existing EstimateInput filters and UUIDv4 request token. No customer/order IDs, amount, currency, URL, metadata or Stripe identifiers accepted from browser.
- Same-origin check uses configured base origin, not Host/Forwarded. Secrets excluded from repr. Body8KiB, rate/concurrency bounds and SDK network timeout/no automatic retries.
- schema v19 adds checkout_attempts only; existing backed-up CLI migration. Web mode=rw existing-only must never create/migrate DB, canonical replacement guard during each short DB operation, no network under it.
- Hashed unguessable request token identifies retry, fingerprint prevents changed request reuse. Store immutable email/offer/origin/time, customer/order correlation, session ID/URL. Retry after ambiguous provider response uses the same Stripe idempotency key; no retry creation after30min, no use after expiry1h. Never mark paid on return or session creation.
- Public configurator and price comparison stay unchanged; app tests use synthetic data and provider fakes.

## Review focus

- API error or DB failure after provider accepted must preserve pending order and same retry parameters, not produce another payable order.
- Two requests with one token must share one local order; reuse with altered email/filters/kind rejects409. Cached URL only returned to bearer of token; email alone never authorizes access or attaches an existing Stripe customer.
- Provider response must be a test session with an HTTPS checkout.stripe.com URL, no credentials/control characters; invalid response never redirects.
- Missing config/DB, live key, wrong schema or replacement guard must fail closed without file creation/network/secret leakage.
- Amounts reject bool/float/zero/negative; subscription/one-off modes and immutable metadata/expiry use exact server values.

### Task1: Pending checkout persistence

**Files:** core/checkout_store.py, core/storage.py; tests/test_checkout_store.py.
**Interfaces:** CheckoutStore.reserve(key_hash,fingerprint,email,filters_json,kind,offer_json,origin,now)->CheckoutAttempt; complete(key_hash,session_id,url)->None. Attempt contains server-generated order/customer IDs, frozen request fields, timestamps and optional cached session. Per-operation connection and short BEGIN IMMEDIATE.

- [x] RED tests for idempotent reserve/conflict, existing customer safety, pending-only state, completed retry, real replacement/WAL/write transaction cleanup, v18→v19 backup/preservation/rollback.
- [x] Implement ledger and v19 migration; verify tests and CLI independence.

### Task2: Stripe service and disabled configuration

**Files:** app/stripe_service.py, app/config.py, requirements.txt/lock; tests/test_stripe_service.py.
**Interfaces:** CheckoutOffer(amount_grosze,interval=None); CheckoutSettings(test_mode=False,secret_key repr=False,origin,package_offer,subscription_offer); CheckoutService.start(email,kind,filters,request_token)->URL. Existing StripeClient.checkout.sessions.create(params,options) behind injected adapter for unit tests.

- [x] RED tests payment/subscription arguments, int amounts, live/config rejection, stable idempotency/retry/config change, no DB lock during fake network, malformed responses, cached and expired attempts.
- [x] Implement SDK10 adapter with timeout5s/no automatic retries, no mutable global API key. Verify real SDK request serialization against a fake HTTP client.

### Task3: Bounded route, success view and publication

**Files:** app/routes/checkout.py, app/main.py, app/templates/checkout_return.html, README.md; tests/test_checkout_routes.py, existing web tests.
**Interfaces:** POST /zamowienie/checkout accepts JSON above and redirects303 in explicit test mode; disabled503. GET /zamowienie/sukces generic informational page ignoring untrusted session/payment query params.

- [x] RED tests default-disabled/no network or writes, strict/oversized/foreign-origin requests, test redirect, return never marks paid/reveals order, secret-safe errors/headers and no new public buy control.
- [x] Implement routes/config docs; preserve existing brand/assets and current navigation. Test full suite, lint/ASCII/diff/SDK install compatibility.
- [ ] Commit/push/open PR, independent review exactBASE/HEAD, fix findings and verify current HEAD CI. No merge/deploy.
