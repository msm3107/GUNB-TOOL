# PR29 / stage11 — test Magic Link and billing ownership

Continue approved `projectplan.md` stage11, inline TDD plus independent review.
No sales activation, dashboard/downloads or bot runtime.

## Contract

- Explicit disabled/test auth configuration, separate HMAC secret (64hex), fixed
  HTTPS origin (HTTP only localhost test), TLS-only supervised SMTP; no startup IO.
- `GET /logowanie`, `POST /auth/magic-link`: known-customer login only; identical
  generic202 for known/unknown/rate-limited syntactically valid addresses. Durable
  limits3/address/hour,10/peer-IP/hour,100/global/hour; no proxy-header identity.
  Mail after ACK, outside DB/guard, at most2 SMTP children, bounded supervision.
- Signed random token, purpose-separated HMAC-SHA256,15min, browser-bound, single
  use. Only hashes in DB. Email URL uses fragment, never query token in access logs.
  GET `/auth/verify` is inert; local JS moves fragment into hidden confirmation POST
  and clears URL; explicit button prevents mail-scanner consumption. No-JS manual
  code entry. Same browser requirement is explained before requesting the link.
- Atomic consume + new opaque signed session (8h absolute); no fixation. Cookie
  HttpOnly/SameSite=Lax, Secure and __Host prefix on HTTPS, Path/, no Domain. Session
  lookup verifies customer/email/expiry; POST logout revokes and clears cookie.
  All state-changing endpoints require exact configured Origin, bounded strict body.
- Real auth resolver wires Portal. Verification never auto-links Stripe by email.
  Explicit `POST /auth/billing-link` additionally requires original secret UUIDv4
  Checkout request token, matching verified customer/email, paid order and signed
  Checkout billing correlation. Never overwrite a different trusted mapping.
- After binding, reconcile current subscription through SDK outside DB/guard using
  a clearly tagged local receipt ID; failure denies fresh access and allows retry.
  Existing stage10 fences/current state checks remain in force.
- Schema22: hashed links/sessions, verified-email snapshots, durable request limits,
  bounded cleanup/indexes; backup-before-DDL/rollback; web never creates/migrates DB.
- UI reuses approved Żółta Tablica assets/styles; no external scripts or new deps.

## Tasks

- [x] RED/GREEN core auth store + migration: TTL/single use/race/email change,
  rate limits/retention/session revocation and capability-based billing ownership.
- [x] RED/GREEN service: HMAC purposes/tamper/future/expiry, unknown-address handling,
  SMTP supervisor outside locks, bounded delivery, no raw token persistence.
- [x] RED/GREEN HTTP/UI: same-origin forms, inert GET, fragment confirmation,
  cookies/fixation/logout, resolver→Portal and binding→authoritative reconciliation.
- [ ] Full suite/lint/compile/ASCII, browser QA with fake SMTP/Stripe, persona audit,
  self-review, commit/push, independent review/fixes, exact-HEAD CI, PR<audit>.

## Review focus

Login CSRF/browser binding; scanner GET and fragment/log hygiene; enumeration and
mail flooding; replay/concurrent consumption; session fixation/email mutation;
cross-customer billing proof and no guest-email binding; SMTP/Stripe outside locks;
crash/retention/migration rollback; default-disabled mode, no real outbound tests.

Primary references checked2026-11-02: [OWASP tokens](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html),
[OWASP sessions](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).
