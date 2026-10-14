# WhatsApp Durable Inbox Factory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Request-owned WAL/FULL inbox connections without request-time DB initialization.

**Architecture:** Opt-in existing-only LeadRepository mode, consumed by a context factory for the existing WSGI app. Defaults unchanged; no listener/runtime activation.

**Tech Stack:** Python >=3.10, SQLite/std lib, pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-whatsapp-durable-factory-design.md`

## Global Constraints

- Schema v15, no migration/dependency/config/env/listener/sends; WA flag still rejected.
- Existing-only uses escaped absolute file URI mode=rw, no mkdir/create/migration/backup, exact current version and existing WAL required.
- Each factory call owns its calling-thread connection; FULL==2 verified outside TX before receive.
- Factory constructor opens no DB, accepts ordinary local file path/settings, capacity exact int 1..100000.
- Generic private errors; no raw path/secret in logs/repr/public responses.
- Preserve default bot/email WAL/NORMAL and migration/backup behavior.
- Final rollout all-token/API/server guide remains mandatory.

## Review Focus

- Missing or concurrently removed DB must never be recreated (Task 1, SQLite mode=rw tests).
- Old/future/non-WAL DB must not be migrated/converted on HTTP intake (Task 1, schema snapshots and migration sentinel).
- URI metacharacters must name the actual path, not inject SQLite options (Task 1, path roundtrip).
- Open/setup/receive/COMMIT/close faults must close resources and produce no false ACK (Task 1, fault injection and real deferred FK).
- Second bot connection and parallel requests must retain settings/ownership independently (Task 1, FULL/NORMAL and threads).

### Task 1: Existing-only opening and durable request factory

**Files:** modify `gunb_tool/storage.py`; create `gunb_tool/whatsapp_inbox_factory.py`, `tests/test_whatsapp_inbox_factory.py`.

**Interfaces:** LeadRepository(existing_only=False) default compatible; WhatsAppInboxFactory(db_path, settings, max_events=10000) returns context manager consumed by WhatsAppWebhookApplication(settings, factory).

- [x] Write synthetic tests for existing-only rejection/no migration/creation, normal defaults, escaped paths, cleanup, factory FULL/ownership and WSGI ACK/error/replay integration.
- [x] Run new tests RED and save evidence; no product implementation before RED.
- [x] Implement minimal existing-only branch and context factory; generic failures/cleanup, no global setting changes.
- [x] Run factory+ingress+storage/migration tests, full pytest; expect pass with existing Windows POSIX skip. Check diff and commit.
- [x] Task-done focused tests PASS; record reviewed assumptions/rulings in ledger.

### Task 2: Operator contract and independent handoff

**Files:** modify `README.md`, `docs/WDROZENIE.md`, this plan.

**Interfaces:** document completed storage gate and remaining server/activation gates; open reviewed PR.

- [x] Document factory/default preservation/local storage and failure/rollback boundaries; preserve final all-token server guide requirement.
- [x] Inspect whole diff/privacy, git diff --check; commit/push/create/attach PR.
- [x] Independent readonly full-PR security/performance/marketing reviewer with exact BASE/HEAD, spec/plan/ledger/test/CI. Save full ignored report, verify/fix findings and same reviewer follow-up for material changes.
- [x] Final exact-head CI all SUCCESS/hasMore false; accepted review and plan/ledger evidence. Status-only commit requires new exact-head CI and unchanged accepted-code check.
- [x] Task-done verifier: clean checkout/diff, accepted code unchanged, full suite evidence, exact-head CI; save local artifacts and hand off without merge/deploy.

## Completion evidence

PR #16 review accepted HEAD92020166 with no findings. Full1669passed1WindowsPOSIXskip33.63s; focused166passed; reviewer101passed1.75s plus emptyfile/concurrentcapacity/lockrecovery probes. Reviewed-head CI12/12SUCCESS/hasMorefalse. SEC-01 storage factory delivered; runtime consumption and server/deployment gates remain mandatory. Final commit updates only plan status; exact final-head CI and unchanged-accepted-code verifier required for handoff. Reports and ledger local/ignored; no merge/deploy.
