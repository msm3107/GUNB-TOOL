# SMTP and email verification implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans, inline continuation as PR #7–8. Track checkboxes; one fresh independent whole-PR reviewer.

**Goal:** TLS-only SMTP adapter and durable one-time email verification.
**Architecture:** Existing Sender/worker/endpoint interfaces; one v14 table via existing migration mechanism. No runtime wiring.
**Tech Stack:** Python >=3.10 standard library, SQLite/WAL, pytest; no dependencies.
**Spec:** `docs/superpowers/specs/2026-10-09-email-smtp-design.md`.

## Global constraints

- Base f197bda2162849db95714a11720ec438b6856c9c, branch codex/email-smtp, reuse attached worktree.
- Keep bot/CLI/UI/scheduler/config gates unchanged; no real network sends, secrets or production data.
- v13 DDL unchanged; v14 migration only additive table/indices. Explain restore requirement.
- All mutable verification operations reject external transaction. SMTP outside DB transaction.
- Mask secrets/addresses/tokens; no raw provider error logging. No merge/deploy.

## Review focus

- Known SMTP rejection permits retry; ambiguous DATA cannot auto-resend.
- Cleanup after DATA 250 cannot turn acceptance into failure/unknown.
- New endpoint/address/version cannot consume an old token, including delete/recreate.
- Limits survive restarts and endpoint deletion; concurrent requests cannot bypass them.
- Migration preserves actual bot/outbox state and refuses unsafe rollback to v13.

### Task 1: SMTP transport adapter

**Files:** Create `gunb_tool/email_sender.py`, `tests/test_email_sender.py`.
**Interfaces:** `SMTPSettings(host, port, from_address, tls='starttls', username='', password='', timeout_seconds=5)`;
`SMTPEmailSender(settings)`; `send(endpoint, report, *, idempotency_key)`;
`send_verification(endpoint, token, *, expires_at, idempotency_key)` -> DeliveryResult.

- [ ] Write fake protocol tests for TLS/auth order, envelope/MIME, stable Message-ID, injection,
  SMTP codes, errors before/during DATA, cleanup, budget and repr/log privacy.
- [ ] Run SMTP tests; verify missing module/API RED.
- [ ] Implement adapter from spec with smtplib/ssl/email, no runtime wiring.
- [ ] Run SMTP and existing notification tests; diff check; commit.

### Task 2: Durable verification and v14

**Files:** Create `email_verification_schema.py`, `email_verification.py`,
`tests/test_email_verification.py`; update `storage.py` migration list,
historical `test_notification_foundation.py`, README and docs/WDROZENIE.
**Interfaces:** `EmailVerification(store, sender).request(chat_id, endpoint_id) -> DeliveryResult | None`,
`consume(chat_id, endpoint_id, token) -> bool`; sender protocol from Task 1.

- [ ] Write tests for real v13 upgrade/state/backup/DDL failure/restart/old-code refusal,
  token privacy, TTL/replay/version/address/ownership/access, attempts, provider failure,
  durable/global/address/user limits, deletion, retention and concurrent request/consume.
- [ ] Run verification tests; expected missing module/schema RED.
- [ ] Implement v14/service and pin historical v13 tests to their actual migration scope.
- [ ] Document preparatory stage, token flow, API limits, TLS timeout limits and rollback.
- [ ] Run affected tests, full pytest -rs, diff check; commit and push.
- [ ] Create/attach PR; independent full review per local prompt; save ignored reports.
- [ ] Resolve real findings with regressions/re-review; verify exact final-HEAD CI.
