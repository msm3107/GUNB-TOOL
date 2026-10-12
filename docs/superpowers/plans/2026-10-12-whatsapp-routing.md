# WhatsApp routing implementation plan

> REQUIRED SUB-SKILL: superpowers:executing-plans. Inline continuation; mandatory
> fresh independent whole-PR review per local AGENTS.

**Goal:** Persist original WhatsApp recipient/sender proof before dispatch and use it for statuses.
**Architecture:** One additive v15 table; bounded local changes to preflight/worker/inbox.
**Tech Stack:** Python>=3.10, SQLite, existing requests/pytest; no dependencies.
**Spec:** `docs/superpowers/specs/2026-10-12-whatsapp-routing-design.md`
**Base:** `9b6e0eff0e5eed9785e039a66db655810728c677`

## Global Constraints

- No WhatsApp activation/listener/real sends/merge/deploy; preserve Telegram/email.
- v15 migration only; no edits to v13/v14 schemas or invented historical backfill.
- Hash-only route/event storage; no secrets/phones/raw errors in repr/logs/artifacts.
- Proof commits before HTTP; never overwrite retry routing; preserve unknown without ID.
- Original owner/version+sender+recipient proof; old-number failure cannot disable new version.
- Legacy v1/routeless statuses remain pending; preserve monotonic status, bounded batches/retention.
- Final rollout PR must deliver the complete token/HTTPS/server update and rollback guide requested by user.

### Task 1: Routing model and additive schema

**Files:** Create gunb_tool/whatsapp_routing.py, gunb_tool/whatsapp_routing_schema.py,
tests/test_whatsapp_routing.py; extend storage.py migration list. Pin four historical
v14 migration tests in tests/test_email_verification.py to their released step.
**Interfaces:** Produce frozen WhatsAppRouting(waba_id,phone_number_id), .scope,
.recipient_digest(recipient_id), and v15 whatsapp_dispatch_routes keyed by outbox_id.

- [x] Write model/privacy/domain separation and real populated-v14 migration/backup/
  DDL rollback/backup refusal/old-code restore/FK/export-import tests.
- [x] Run routing tests. RED: missing model.
- [x] Implement model/table/migration and historical release fixture boundaries.
- [x] Run routing + email verification migration tests and diff check. PASS (80 cases).
  Commit; complete task with that test command.

### Task 2: Dispatch proof and scoped inbox

**Files:** Modify notification_store.py, notification_worker.py, whatsapp_sender.py,
whatsapp_inbox.py; extend routing tests and update affected sender/worker/inbox helpers.
**Interfaces:** Task1 model; prepare_send(claim,*,whatsapp_routing=None), sender.routing,
required WhatsAppSettings.waba_id; inbox durable v2 payload with scope and scoped digest.

- [x] Write RED tests for route committed before intercepted HTTP, retry immutability,
  missing/conflicting route, COMMIT failure/no HTTP, v2 exact privacy, original-number
  status after change, old failure isolation, wrong scope/owner/version, legacy/routeless
  events, early event/ACK, route cascade. Update existing synthetic ACK helpers to
  create genuine preflight routing proof; no production compatibility bypass.
- [x] Run targeted routing/sender/inbox/worker/store tests. RED for missing behavior.
- [x] Implement narrow changes, preserving direct email behavior and all transaction limits.
- [x] Run same targeted tests. PASS: 327 tests, 5.03s; clean diff check.

### Task 3: Operator docs, review and final PR

**Files:** README.md, docs/WDROZENIE.md, plan status; tests if demonstrated integration gaps.
**Interfaces:** Task1 backup/restore and Task2 routing behavior; final reviewer consumes whole diff.

- [x] Document v15/backup rollback, routing and legacy pending limits, inactive status,
  remaining rollout and mandatory complete final token/server guide; no premature setup commands.
- [x] Run full python -m pytest -rs and git diff --check. 1513 passed, 1 POSIX Windows skip, 21.10s.
- [x] Commit/push own branch; create/attach PR; dispatch independent readonly whole-PR reviewer
  with exact BASE/HEAD/spec/plan/tests/CI; save ignored report and fix verified findings.
- [x] Material corrections get same-reviewer followup and new exact-head CI. Record status,
  verify final CI and hand off without merge/deploy. Preserve ignored scratch evidence.

## Acceptance snapshot

PR #13: https://github.com/msm3107/GUNB-TOOL/pull/13.
Independent readonly whole-PR review: **AKCEPTUJ**, no P0/P1/P2/P3 findings,
BASE `9b6e0eff0e5eed9785e039a66db655810728c677`,
implementation HEAD `2de1f958a7347f4570ea12e5c462c2c50e123a7e`.
Reviewer independently ran 81 focused tests (all passed) and inspected query plans.
That HEAD CI: 12/12 SUCCESS (Python 3.10–3.14 + deploy-scripts, push and PR).
No correction/followup required. Reports and CI evidence stay in ignored local review files.

Only plan bookkeeping follows that snapshot. Before handoff the coordinator must
verify unchanged accepted code, clean diff/worktree and exact final-HEAD CI;
no redundant full suite for the status-only commit. Full suite evidence:
1513 passed, 1 POSIX-on-Windows skip, 21.10s. No real Meta/server test, activation,
merge or deployment. Final token/HTTPS/server operator guide remains a release gate.

## Review Focus

- Provider ID alone cannot cross recipient/sender scope or original owner/version.
- Historical failure must not quarantine a reconfigured endpoint or cancel its fresh work.
- Missing/changed proof and failed COMMIT must prevent any network send, including retry.
- Legacy inbox data cannot be silently attributed to the current Meta settings.
- Additive migration must preserve all bot data and require the correct backup for v14 rollback.
