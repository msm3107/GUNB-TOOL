# PR28 / stage10 — test subscriptions and Customer Portal

Scope: `projectplan.md` stage10, continue test-only gates from PR26/27.
Execution: inline TDD, independent whole-branch review before PR, persona audit.

## Contracts and rulings

- Authentication is stage11. `AuthenticatedCustomer(customer_id)` and a trusted
  server-side identity resolver form its interface; default resolver denies all.
  No header/cookie/body/email identity shortcut or public account-linking endpoint.
- `POST /panel/portal`: test-only, authenticated owner of an existing trusted
  `b2b_customers.stripe_customer_id`, exact configured Origin, empty body. Fixed
  server return URL to existing `/zamowienie`; validate test SDK response and
  HTTPS billing.stripe.com. Read mapping before API and recheck before redirect;
  no DB transaction/guard during API. Timeout5s, no automatic retries, bounded work.
- Guest Checkout never binds Stripe customer by email. Paid subscription receipt
  records an order-scoped Stripe customer/subscription correlation only; it grants
  no account ownership or subscription access. Trusted account provisioning and
  production authentication remain blocked until stage11 integration.
- Subscription updated/deleted must match that paid Checkout correlation plus
  existing trusted customer mapping. Unknown customers are durably ignored;
  missing paid receipt for known owner returns503 for provider retry.
- Updated event: claim a30s fenced refresh in a short transaction, retrieve current
  test Subscription with SDK outside DB/guard, revalidate IDs/metadata, atomically
  update state and event ledger. Fresh authoritative state handles out-of-order
  snapshots. Concurrent refresh returns503, expired worker cannot commit.
- Deleted event: signed terminal cancellation recorded atomically without network,
  invalidates in-flight refresh and cannot be resurrected by delayed updates.
- `active` with future period_end and no pause_collection grants test entitlement;
  past_due/unpaid/canceled/paused/trialing/incomplete do not. Cancel-at-period-end
  preserves access until period_end; multiple subscriptions aggregate per customer.
  Access is computed from current time and trusted mapping, not only cached status.
- Schema21: retain paid Checkout billing links, subscription state/leases/index;
  rebuild event ledger to allow subscription outcome, preserving all v20 rows.
  Backup-before-DDL/rollback tests; web modules never migrate/create DB.

## Tasks

- [x] RED/GREEN migration/payment-correlation and subscription-store tests:
  cancel, delinquency, expiry, multiple subs, mapping isolation, ordering/fencing.
- [x] RED/GREEN SDK subscription verification + signed webhook tests; real SDK HTTP
  interception verifies version, payload, headers and unlocked network boundary.
- [x] RED/GREEN Portal identity/service/route tests: unauthorized/spoofing/CSRF,
  tenant isolation, malformed output/network failures, mapping change during API.
- [ ] Full suite/lint/ASCII/diff checks, self-review, three-persona audit; commit,
  push, independent review/fixes and exact-HEAD CI; open PR with `<audit>` block.

## Review focus

No email-derived ownership; delayed deleted/updated events and concurrent refresh;
rollback of ledger/state; default-deny auth; fixed redirect and no token logging;
expiry and multiple subscriptions; v20 ledger preservation; no effect on paid
one-off orders, delivery memory, file generation or public sales claims.

Sources checked2026-11-02: [Portal API](https://docs.stripe.com/api/customer_portal/sessions/create?api-version=2024-06-20),
[subscription webhooks](https://docs.stripe.com/billing/subscriptions/webhooks),
[Subscription retrieval](https://docs.stripe.com/api/subscriptions/retrieve?api-version=2024-06-20).
