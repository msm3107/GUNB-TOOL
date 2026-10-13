# Bounded WhatsApp WSGI ingress

Date: 2026-10-12. Authorized next PR after merged #14, base
`2bda45b8a75c33e8af0eec8c09ea2654717adbcf`.

## Intent and boundary

Translate HTTP into the existing authenticated committed inbox without changing
Telegram/email, schema v15 or enabling WhatsApp. Use a small WSGI callable and
stdlib only; a new framework or custom socket/HTTP server is unnecessary here.
This PR adds no listener, runtime wiring, config/env keys or real network sends.

`WhatsAppWebhookApplication(settings, inbox_factory, *, max_inflight=4)` is a
WSGI callable at `/webhooks/whatsapp`. Settings must be WhatsAppWebhookSettings,
factory callable, capacity an exact integer 1..16. Factory returns a context
manager yielding a WhatsAppInbox for this request, with the same settings.
The inbox exposes immutable `webhook_settings` for this equality check (all four
fields); a mismatch returns 503 before receive, preventing a false ACK for a
different sender scope. Future
runtime must create/close a separate SQLite connection in the calling thread and
initialize/migrate the database before serving; never share the bot connection.
The app does not open or migrate a database itself. Validate/authenticate the
entire body before invoking the factory. The inbox then revalidates via its
existing public receive method; two bounded parses preserve its trust boundary.

## Request contract

- Require server-supplied `wsgi.url_scheme == 'https'`; never infer it from client
  Forwarded/X-Forwarded-Proto headers. Reject insecure requests with 403.
- Exact PATH_INFO only, otherwise 404. GET and POST only, otherwise 405 with Allow.
- Reject nonempty Transfer-Encoding with 400; reject content encoding except
  absent/identity with 415. The front server must reject ambiguous/duplicate
  framing before normalizing into WSGI, which cannot recover raw header lines.
- GET accepts an absent/zero Content-Length and exactly three query fields:
  hub.mode, hub.verify_token, hub.challenge. Raw query is ASCII <=2048 characters,
  valid percent escapes, strict UTF-8 decoding, no duplicates or unknown fields.
  Bad structure/body length is 400, bad verification is 403. Existing challenge
  validator controls numeric echo. GET never reads a body or opens a database.
- POST has no query string, application/json (optional UTF-8 charset only), and
  decimal Content-Length in 1..65536. Missing length is 411, malformed/zero is
  400, oversize 413; do not read input or open a DB for these rejections.
- Read exactly the declared length in chunks <=8192, supporting short reads.
  Never read beyond Content-Length or close the server-owned input. Early EOF,
  non-bytes or oversized read result is 400; stream failure is 503. Use raw bytes
  for HMAC; never parse and reserialize before authentication. Parser rejection is
  generic 403, including bad signed JSON. No database is touched on rejection.
- A per-application, nonblocking bounded semaphore covers POST body read through
  factory exit, default four requests. Overflow is 503 without reading input.
  Release on every path, including errors; this is not a global rate limit.

## Durable response and privacy

Inside the request's context manager call inbox.receive(raw, signature). Only its
successful return AND successful context exit authorize 200 with `OK\n`; duplicates
and authenticated irrelevant batches also ACK. Wrong factory type, DB/COMMIT/full
inbox/open/close failure return 503 with Retry-After: 5; never turn them into ACK.
An error after a successful commit may cause replay, which the inbox deduplicates.
Do not reconcile statuses, send messages or update consent in the HTTP request.

Durability boundary (SEC-01): existing file-backed LeadRepository uses WAL/NORMAL.
Committed transactions survive application crashes, but may roll back after OS
crash/power loss. Request connection exit alone does not force a last-connection
checkpoint while the bot connection remains open. This stage tests COMMIT/visibility,
not host-failure durability. Before activation the factory/runtime integration owner
must configure and verify file-backed local-disk WAL plus synchronous=FULL (PRAGMA
value 2) on EACH request connection outside a transaction before receive, or provide
equivalent durable storage. Keep global bot settings unchanged. Acceptance must
hold another bot-like connection open, check FULL before receipt, and confirm ACK
after COMMIT/context exit; reopening alone is not a power-loss test. Storage/OS must
honor synchronization. Source checked 2026-10-13:
[SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous).

All responses are fixed plain-text bytes (except validated numeric GET challenge),
with Content-Length, Cache-Control: no-store and X-Content-Type-Options: nosniff.
No raw exception, URL/query, signature, secret or body in logs, responses or repr.
Do not emit start_response until the result is known and all application resources
are released. Do not catch transport errors from start_response and retry it.

## Deployment gates and sources

WSGI alone cannot impose a hard deadline on input.read. Future server/proxy must
buffer and bound request bodies, bound header/query sizes, enforce read/write and
worker deadlines, bound global concurrency/rate, terminate TLS and set the scheme
only from a trusted proxy. Suppress/redact query strings (GET verification token),
headers and bodies in access/error logs. Never expose wsgiref/http.server as a
production listener. These are activation gates, not a security claim for an
unconfigured public endpoint. No actual HTTPS/proxy/server test in this PR.

Interface source checked 2026-10-12: [PEP 3333](https://peps.python.org/pep-3333/)
(callable/environ, byte responses, server-owned input limited by Content-Length).
Existing GET challenge behavior is corroborated by Meta's [SDK webhook guide](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/).
Current Graph Webhooks guide was unavailable to the browsing tool; no fresh policy,
pricing, template or API-version compliance claim is made. Verify current Meta
requirements and a controlled pilot before activation.

## Tests and final handoff

TDD: WSGI validator/response headers, exact GET challenge and invalid queries,
framing/media/scheme/method/path/size limits with no read/DB, short raw reads/HMAC,
real SQLite commit-before-start_response, duplicates, invalid HMAC before factory,
full inbox and deferred COMMIT failure, open/close faults, per-thread ownership,
semaphore overload/release, private diagnostics. Synthetic in-process requests;
no sockets or production data. Full pytest, exact-head CI and independent review.

No schema rollback needed to return to PR #14 (v15); older-schema restore rules
remain. Remaining work includes production HTTPS/runtime wiring, number proof and
opt-in/revocation, compact reports, pacing, reconciliation/recovery and pilot.
The final rollout PR must provide ALL token/ID/secret setup and bot-preserving
server install/update, backup, controlled restart, health/activation and rollback
instructions. This remains a release gate; this PR does not deploy or send.
