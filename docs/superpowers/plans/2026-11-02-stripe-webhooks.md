# PR9 — test-only Stripe webhooks

Approved scope: stage 9 of `projectplan.md`; continue the test-only boundary of PR26.
Execution: inline implementation, TDD, one independent whole-branch reviewer.

## Contract and decisions

- `POST /api/webhook/stripe`: raw-body SDK HMAC verification, 300-second past/future
  tolerance, 128 KiB body and bounded concurrency. Disabled without explicit test
  Checkout configuration, signing secret and private package directory.
- Only account-local snapshot events, API `2024-06-20`, `livemode=false`;
  `checkout.session.completed` requires `status=complete`, `payment_status=paid`,
  local metadata/client reference/session match and the frozen positive PLN amount.
  Unpaid/unsupported test events are recorded as ignored; malformed/mismatched
  completions cannot mutate payment state. No external API is needed.
- Schema v20: minimal `stripe_events` ledger and one `paid_package_jobs` row per
  order. Event deduplication, pending→paid and enqueue share one short transaction.
  Different event IDs for the same session/order cannot enqueue twice.
- Return 200 after durable commit. Invoke the generator as best-effort background
  work; recover interrupted/pending work with an explicit bounded core module CLI.
  This is preparation, not SMTP/dispatch/download/auth/subscription activation.
- Renderer: claim with random fencing token and 10-minute lease; read a bounded
  snapshot using the estimator's exact scope and customer delivery memory; close
  DB/guard, write private XLSX/CSV, then fence/publish paths and items in one short
  transaction. `doreczono_utc` stays NULL. At most 10,000 matching records per job;
  larger jobs fail visibly for operator intervention, never truncate silently.
- No raw payload, email, signing secret, card data or Stripe customer binding in
  event ledger; browser return remains informational. No prices/live switch added.

## Tasks

- [x] Add failing signed-request/payment/ledger tests; implement `PaymentStore`
  and migration v20 with backup and rollback regressions.
- [x] Add failing renderer tests; implement shared scope selection and
  `PaidPackagePreparer.run_pending(limit=10)` / `prepare(order_id)` with lease,
  real exports, recovery, bounded input and no write lock during rendering.
- [x] Add failing endpoint tests; implement SDK verifier, configuration and route,
  generic errors, disabled mode, body/signature limits and background preparation.
- [ ] Run full pytest, lint, diff/ASCII checks; audit three personas; commit/push,
  independent review, resolve findings and verify CI for final HEAD; open/update PR.

## Review focus

Forged/modified/stale/future signatures and malformed JSON; cross-order or amount
substitution; callback before local session save; duplicate IDs and separate IDs
for one session; SQLite rollback/lock contention; crash after ACK/claim/file-write;
expired worker fencing; private files and unchanged delivered memory; core imports
without Stripe/web frameworks. No real provider, customer DB or live sale testing.

Sources checked 2026-11-02: [Stripe webhooks](https://docs.stripe.com/webhooks),
[fulfillment](https://docs.stripe.com/checkout/fulfillment.md?ui=full-page),
[snapshot Session](https://docs.stripe.com/api/checkout/sessions/object?api-version=2024-06-20).
