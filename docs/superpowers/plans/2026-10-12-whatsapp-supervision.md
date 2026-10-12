# Supervised WhatsApp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound a WhatsApp operation outside the bot thread and preserve conservative delivery outcomes.

**Architecture:** One disposable spawn child, following SMTP's existing pattern without changing it. Parent owns no network call and receives bounded validated JSON after child exit. Existing worker consumes `.routing` and `send` unchanged.

**Tech Stack:** Python >=3.10, multiprocessing/stdlib, existing requests/SQLite/pytest.

**Spec:** `docs/superpowers/specs/2026-10-12-whatsapp-supervision-design.md`

## Global Constraints

- Preserve Telegram, email, schema v15, provider classification and durable preflight proof.
- Config still rejects `whatsapp.enabled: true`; no listener, scheduler or command.
- No new dependencies, secrets/config/env, production reads, sends, merge or deployment.
- Production uses spawn; no repository/connection/bot in the child.
- Budget finite `0 < timeout_seconds <= 45`, poll <=0.2s, cleanup joins <=1s each.
- Result <=2048 bytes, exact outcome/provider_message_id/retry_after_seconds keys.
- Accepted requires wamid; retry/failed/unknown require null provider ID; no delivered/read.
- Final rollout PR includes complete token and bot-preserving server handoff guide.

## Review Focus

- Child suspended mid-frame: parent must time out without blocking on a partial pipe read (Task 1 test).
- Stop/crash near HTTP ACK: ambiguous outcomes must never authorize a second potentially accepted send (Task 1 tests).
- Provider/debug exceptions or malformed IPC: no secret/body leakage or fabricated accepted/delivered/read (Task 1 tests).
- Process cleanup/start failures: resources are bounded and cleanup cannot erase a validated ACK (Task 1 tests).
- Documentation must distinguish an internal adapter from activated service and avoid an absolute OS deadline promise (Task 2 inspection).

### Task 1: Supervisor and end-to-end process boundary

**Files:**
- Create: `gunb_tool/whatsapp_process.py`
- Create: `tests/test_whatsapp_process.py`
- Create: `tests/whatsapp_process_helpers.py`

**Interfaces:**
- Consumes: `WhatsAppSender(settings).send(endpoint, report, *, idempotency_key) -> DeliveryResult`, `WhatsAppRouting`, `strict_json(raw, limit)`, existing worker/store/fixtures.
- Produces: `SupervisedWhatsApp(settings, *, should_stop=lambda: False, timeout_seconds=45, _context=None)` with `.routing` and `.send(endpoint, report, *, idempotency_key) -> DeliveryResult`.

- [x] Write tests for valid ACK/retry/failed/unknown, malformed/oversized/partial IPC, invalid outcomes/provider IDs/Retry-After, invalid budget/settings, stop/start/crash/deadline, cleanup/privacy, repeated calls without resource reuse, real spawn and real worker committed proof/unknown no resend. Literal accepted ID `wamid.synthetic_1=` and retry delay 17; no network in spawn helpers.
- [x] Run `python -m pytest tests/test_whatsapp_process.py -q`; expected failure because `gunb_tool.whatsapp_process` is absent. Save RED evidence locally.
- [x] Implement the supervisor/child and strict private result codec exactly as the spec; wait for exit before reading bytes. Fault injection belongs only in `_context` and test helpers.
- [x] Run `python -m pytest tests/test_whatsapp_process.py tests/test_whatsapp_sender.py tests/test_whatsapp_routing.py tests/test_notification_worker.py tests/test_smtp_process.py -q`; expected PASS. Then full `python -m pytest -rs`; expected PASS except existing Windows POSIX skip.
- [x] Inspect `git diff --check`; expected exit 0. Commit module/tests. Complete task with the focused command above through task-done.

### Task 2: Operator documentation and independently reviewed PR

**Files:**
- Modify: `README.md`, `docs/WDROZENIE.md`, this plan.

**Interfaces:**
- Consumes: Task 1 public supervisor contract and measured tests.
- Produces: truthful stage documentation and accepted open PR with exact-head CI evidence, local ignored review/ledger.

- [x] Document internal supervisor, budget/cleanup/OS limitations and future >=60s lease/thread requirement, classification, partial frame handling, no activation/schema changes, rollback to PR #13 and final token/server-guide gate.
- [x] Inspect whole PR for scope/privacy, run `git diff --check`; expected exit 0. Commit docs; full suite evidence remains valid if only prose changed.
- [x] Push feature branch and create/attach PR; dispatch independent readonly reviewer with exact base/head, whole diff, spec/plan/ledger and tests/CI. Save report in ignored `.local-review/`; verify findings and same-reviewer followup if material changes.
- [x] Check exact final HEAD CI; expected all jobs SUCCESS with hasMore false. Save evidence and mark plan complete. A bookkeeping-only final commit requires exact-head CI and code-unchanged verification rather than another full suite.
- [x] Run local final verifier: accepted implementation unchanged apart from this plan, clean checkout/diff, saved passing full suite and complete exact-head CI. Expected PASS; task-done uses this verifier. Hand off without merge/deploy.

## Acceptance snapshot — 2026-10-12

PR [#14](https://github.com/msm3107/GUNB-TOOL/pull/14). Independent readonly review
of `f5bcc75964e7fba3981b5537bcde80b67f83a466`: **AKCEPTUJ**, no confirmed defects
or requested fixes. Reviewer independently ran all 55 supervisor tests (PASS,
3.07s) and confirmed exact-head CI 12/12 SUCCESS, hasMore false. Author's full
suite: 1568 passed, 1 skipped (Windows POSIX permissions), 24.90s; focused boundary
suite: 254 passed, 6.21s. All reports remain local and ignored.

This completion commit changes only plan bookkeeping. Its own exact-head CI and
code-unchanged verification are required before handoff; the local final ledger
records their results. WhatsApp remains inactive and the final token/server guide
is still a release gate. No merge, deployment or real notification was performed.
