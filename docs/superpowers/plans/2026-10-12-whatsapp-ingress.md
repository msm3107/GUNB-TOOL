# WhatsApp WSGI Ingress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** ACK HTTP status batches only after authenticated durable inbox commit.

**Architecture:** A bounded WSGI callable wraps existing parser/inbox; a request-owned context factory supplies its connection. No active listener or framework.

**Tech Stack:** Python >=3.10, stdlib WSGI/threading, existing SQLite/pytest.

**Spec:** `docs/superpowers/specs/2026-10-12-whatsapp-ingress-design.md`

## Global Constraints

- Preserve Telegram/email, schema v15 and disabled WhatsApp config gate.
- No new dependencies, listener, runtime/config/env changes, real sends or deployment.
- `/webhooks/whatsapp`, HTTPS scheme from WSGI only, GET/POST; exact request contract in spec.
- Body 1..65536 bytes, reads <=8192 and never beyond Content-Length; query <=2048.
- Per-app POST semaphore default 4, exact integer 1..16; nonblocking overflow 503.
- Authenticate before factory, own connection per request thread, 200 only after commit/context exit.
- Generic private errors, no logging/query/body leak; 503 includes Retry-After: 5.
- Final rollout must include complete token/API/secret and bot-preserving server guide.

## Review Focus

- WSGI cannot detect original duplicate framing headers; front-server requirements must be explicit (Task 1/2).
- A body stream may short-read, fail or never return; enforce byte limits here and document external deadline gate (Task 1/2).
- Factory enter/receive/exit failure after commit must not create a false ACK or leak details (Task 1).
- Concurrent requests must own separate SQLite connections, release capacity on all paths and ACK duplicates safely (Task 1).
- Verification query tokens must never appear in app diagnostics or be trusted as TLS proof; external log redaction remains mandatory (Task 1/2).

### Task 1: Bounded application and durable request tests

**Files:** create `gunb_tool/whatsapp_ingress.py`, `tests/test_whatsapp_ingress.py`; modify `gunb_tool/whatsapp_inbox.py` (readonly settings property).

**Interfaces:**
- Consumes `WhatsAppWebhook(settings).challenge/parse_statuses` and `WhatsAppInbox.receive(raw, signature)`.
- Validate `WhatsAppInbox.webhook_settings` against all application settings before receive; mismatched scope must not silently ACK ignored statuses.
- Produces `WhatsAppWebhookApplication(settings, inbox_factory, *, max_inflight=4).__call__(environ, start_response)` with byte-list responses and request-scoped inbox context.

- [x] Write tests proving the spec's request/response/error matrix, raw byte integrity, no reads/DB on early rejection, real SQLite commit before ACK, capacity/COMMIT faults and concurrent ownership. Use literal expected HTTP statuses and synthetic signed batches; no server launch.
- [x] Run `python -m pytest tests/test_whatsapp_ingress.py -rs`; expected failures for absent application. Save RED evidence locally.
- [x] Implement minimal WSGI application and private helpers, preserving parser/inbox public boundary and request ownership.
- [x] Run `python -m pytest tests/test_whatsapp_ingress.py tests/test_whatsapp_webhook.py tests/test_whatsapp_inbox.py -rs`; expected PASS. Full `python -m pytest -rs`; expected PASS except existing Windows POSIX skip.
- [x] Check diff and commit module/tests; task-done runs focused suite, expected PASS.

### Task 2: Operator contract, independent review and handoff

**Files:** modify `README.md`, `docs/WDROZENIE.md`, this plan.

**Interfaces:** consumes Task 1 callable contract; produces truthful inactive-stage docs and accepted open PR.

- [x] Document request limits/statuses/durable ACK, factory ownership, external HTTPS/body/header/deadline/global limit/logging requirements, v15 rollback and final all-token server handoff gate.
- [ ] Inspect complete diff/privacy and `git diff --check`; expected exit 0. Commit docs, push and create/attach PR.
- [ ] Dispatch independent readonly whole-PR reviewer with exact SHA, spec/plan/ledger/tests/CI. Save ignored report; verify/fix findings, same reviewer follows material fixes.
- [ ] Confirm exact final HEAD CI all SUCCESS/hasMore false. Record accepted review and completion; bookkeeping-only commit requires new exact-head CI and unchanged implementation verification.
- [ ] Task-done runs local verifier for clean checkout/diff, unchanged accepted code, saved full-suite and final-head CI. Expected PASS; preserve local evidence and hand off without merge/deploy.
