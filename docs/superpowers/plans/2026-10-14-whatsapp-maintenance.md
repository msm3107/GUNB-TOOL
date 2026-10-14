# Managed WhatsApp maintenance implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Stop optional managed webhook writers before release/database maintenance.

**Architecture:** Small matching Bash barrier in both standalone entry points; existing bot lifecycle retained. Webhook stays stopped/disabled until a later checked activation procedure.

**Tech Stack:** Bash, systemd, Python>=3.10 pytest subprocess harness.

**Spec:** `docs/superpowers/specs/2026-10-14-whatsapp-maintenance-design.md`

## Global Constraints

- No schema/runtime/channel/dependency/default activation changes; no real sends/deployment/merge.
- Managed unit gunb-whatsapp-webhook.service only; absent not-found/inactive is no-op.
- Unknown manager state or any failure aborts before database/release mutation.
- Never start/enable webhook; retain bot recovery after failed backup.
- Current admin guards target installers, including older versions; direct old installers/CLI are outside barrier.
- One operator; no independent reactivation/manual writer. Disable is not a privileged-start lock.
- Final ALL-token/API/server guide and bot-preserving rollout remain mandatory.

## Review Focus

- Absent optional unit and manager failure must be distinguished (Task1 state matrix).
- Bash errexit can be lost inside conditional function calls (Task1 explicit failure tests).
- Update to old installer must be guarded by current admin (Task1 stub target invocation).
- Backup failure must recover bot without reactivating webhook (Task1 actual switch function).
- Live export/backup must retain online semantics, final export must stop writers (Task1 admin matrix).

### Task 1: Maintenance barrier and behavioral regression tests

**Files:** modify `deploy/install.sh`, `deploy/gunb-admin`; create `tests/test_deploy_maintenance.py`.

**Interfaces:** `quiesce_webhook()`: fail via die, success leaves optional managed unit inactive/disabled; both entry points self-contained. Tests source declarations only, replace systemctl and external boundaries, never run privileged commands.

- [ ] Write subprocess matrix for both barriers and mutation ordering/failures, admin update/rollback/stop/restore/import/final-vs-live export; run RED for absent behavior.
- [ ] Implement exact contract and matching helpers, preserve no-hook default and existing backup recovery. Tests assert no webhook start/enable.
- [ ] Run focused tests and full pytest; Bash syntax and diff check; commit and task-done focused pytest PASS.

### Task 2: Operator boundaries, review and handoff

**Files:** modify `README.md`, `docs/WDROZENIE.md`, inactive service comment and this plan.

**Interfaces:** truthful managed-maintenance documentation and reviewed PR; no activation commands offered prematurely.

- [ ] Document interruption/disabled recovery, current admin vs old installers/direct CLI, one operator/no independent starts, remaining activation gates and final all-token guide.
- [ ] Verify complete diff and tests, commit/push/create/attach PR. Independent fresh whole-PR reviewer receives exact SHA/spec/plan/ledger/evidence.
- [ ] Save local ignored report, verify/fix findings; material follow-up to same reviewer. Check exact final HEAD CI, hand off without merging/deploying.
