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

- [ ] Write model/privacy/domain separation and real populated-v14 migration/backup/
  DDL rollback/backup refusal/old-code restore/FK/export-import tests.
- [ ] Run routing tests. Expected RED: missing model/schema.
- [ ] Implement model/table/migration and historical release fixture boundaries.
- [ ] Run routing + email verification migration tests and diff check. Expected PASS.
  Commit; complete task with that test command.

### Task 2: Dispatch proof and scoped inbox

**Files:** Modify notification_store.py, notification_worker.py, whatsapp_sender.py,
whatsapp_inbox.py; extend routing tests and update affected sender/worker/inbox helpers.
**Interfaces:** Task1 model; prepare_send(claim,*,whatsapp_routing=None), sender.routing,
required WhatsAppSettings.waba_id; inbox durable v2 payload with scope and scoped digest.

- [ ] Write RED tests for route committed before intercepted HTTP, retry immutability,
  missing/conflicting route, COMMIT failure/no HTTP, v2 exact privacy, original-number
  status after change, old failure isolation, wrong scope/owner/version, legacy/routeless
  events, early event/ACK, route cascade. Update existing synthetic ACK helpers to
  create genuine preflight routing proof; no production compatibility bypass.
- [ ] Run targeted routing/sender/inbox/worker/store tests. Expected RED for missing behavior.
- [ ] Implement narrow changes, preserving direct email behavior and all transaction limits.
- [ ] Run same targeted tests. Expected PASS; inspect diff/commit and complete task.

### Task 3: Operator docs, review and final PR

**Files:** README.md, docs/WDROZENIE.md, plan status; tests if demonstrated integration gaps.
**Interfaces:** Task1 backup/restore and Task2 routing behavior; final reviewer consumes whole diff.

- [ ] Document v15/backup rollback, routing and legacy pending limits, inactive status,
  remaining rollout and mandatory complete final token/server guide; no premature setup commands.
- [ ] Run full python -m pytest -rs and git diff --check. Expected PASS, only POSIX Windows skip.
- [ ] Commit/push own branch; create/attach PR; dispatch independent readonly whole-PR reviewer
  with exact BASE/HEAD/spec/plan/tests/CI; save ignored report and fix verified findings.
- [ ] Material corrections get same-reviewer followup and new exact-head CI. Record status,
  verify final CI and hand off without merge/deploy. Preserve ignored scratch evidence.

## Review Focus

- Provider ID alone cannot cross recipient/sender scope or original owner/version.
- Historical failure must not quarantine a reconfigured endpoint or cancel its fresh work.
- Missing/changed proof and failed COMMIT must prevent any network send, including retry.
- Legacy inbox data cannot be silently attributed to the current Meta settings.
- Additive migration must preserve all bot data and require the correct backup for v14 rollback.
