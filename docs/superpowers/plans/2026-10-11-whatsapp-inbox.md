# WhatsApp inbox implementation plan

> REQUIRED SUB-SKILL: superpowers:executing-plans. Continue the authorized rollout
> inline; one independent whole-PR reviewer per local AGENTS.

**Goal:** Durable authenticated status intake and monotonic reconciliation while WhatsApp stays inactive.
**Architecture:** Existing SQLite v14 tables, bounded batches, independent inbox module.
**Tech Stack:** Python >=3.10, SQLite, stdlib, existing pytest; no dependencies.
**Spec:** `docs/superpowers/specs/2026-10-11-whatsapp-inbox-design.md`
**Base:** `66d6edf383b754beb0d59e9e8da5c1d6028f2751`

## Global Constraints

- Preserve v14, Telegram/email sending, public store APIs, CLI/config activation gate.
- Authenticate raw bytes before writes; return only after commit; no raw body/recipient/secrets in storage/logs.
- Default whole-table capacity 10000 (1..100000); parser 64 KiB/100 statuses; reconciliation limit 1..100.
- Owned short transactions; unique provider ID + channel/owner/current recipient proof, never phone-only guessing.
- Accepted < delivered < read; failed never overrides delivered/read; no automatic reenable or resend.
- Seven-day first-receipt retention, at most 2000 deletions nightly; keep ignored scratch/review records locally.
- No HTTP listener, activation, merge, deployment, real sends or published reviews.

### Task 1: Authenticated durable intake

**Files:** Create `gunb_tool/whatsapp_inbox.py`, `tests/test_whatsapp_inbox.py`.
**Interfaces:** Consume WhatsAppWebhookSettings/WhatsAppWebhook and NotificationStore;
produce `WhatsAppInbox(store, settings, *, max_events=10000).receive(raw_body, signature) -> int`
and `WebhookInboxFull`. Private v1 payload contains outcome/timestamp/recipient_digest only.

- [ ] Write real SQLite tests for signed minimal storage, durable reopen, replay, invalid batch/auth
  rejection, no plaintext private data, capacity including duplicates/full batch, outer transaction
  rejection, trigger-induced rollback and concurrent independent connections.
- [ ] Run `python -m pytest tests/test_whatsapp_inbox.py -rs`. Expected RED: absent inbox module.
- [ ] Implement bounded authenticated ingress, own transaction, novelty/capacity and atomic inserts.
- [ ] Run same tests, inspect output and `git diff --check`. Expected PASS. Commit intake/tests.
- [ ] Complete task with same pytest verification command.

### Task 2: Correlation and monotonic status transitions

**Files:** Extend inbox and its tests; consume existing NotificationStore quarantine and strict report model.
**Interfaces:** Consume Task1 persisted rows; produce `.reconcile(*, limit=100) -> int`,
bounded keyset traversal and atomic delivery/outbox/event bookkeeping.

- [ ] Add tests for all status order pairs, failure quarantine/late success, early event then ACK,
  late ACK, lost ACK without ID, wrong/changed recipient, ownership, email/ambiguous provider IDs,
  revoked/pause with unchanged address, malformed rows, no fabricated histories, worker races,
  restart, concurrent reconcilers, fair sweep/limits and DB rollback including cursor.
- [ ] Run inbox tests. Expected RED: reconciliation absent.
- [ ] Implement spec matching/ordering/quarantine/history updates and fair cursor after commit.
- [ ] Run inbox and notification store/worker/webhook tests. Expected PASS; diff clean.
- [ ] Commit implementation/tests; complete task with those tests.

### Task 3: Retention, integration evidence and PR

**Files:** Modify `gunb_tool/email_runtime.py`, `tests/test_email_runtime.py`, README.md,
docs/WDROZENIE.md; extend inbox tests if needed.
**Interfaces:** Consume Task1 first receipt and Task2 monotonicity; extend existing nightly
`prune_notifications` with one <=2000 deletion batch for WhatsApp events older than seven days.

- [ ] Add retention tests (pending/processed/replay/future provider timestamps/email isolation/
  batch bound/account deletion) and run them. Expected RED: old events survive current maintenance.
- [ ] Implement bounded retention; run inbox/email runtime tests. Expected PASS.
- [ ] Inspect EXPLAIN plans for provider lookup and pending keyset; measure synthetic 10000-row
  fair reconciliation and save ignored results. Document scope, privacy, limits, changed-number
  limitation, ACK contract, rollback and remaining activation gates honestly.
- [ ] Run `python -m pytest -rs` and `git diff --check`. Expected PASS with only Windows POSIX skip.
  Commit, push feature branch, create/attach PR.
- [ ] Dispatch fresh readonly whole-diff reviewer with exact BASE/HEAD/PR/spec/plan/tests/CI;
  save report, verify/fix findings with regressions and same-reviewer followup where material.
- [ ] Record completion, verify exact final HEAD CI and hand off without merge/deployment.

## Review Focus

- A valid provider ID is not enough if channel, owner or recipient differs; old-address status
  must never update a new recipient or enable sending.
- Repeated failures after delivered/read and late worker acknowledgements cannot downgrade
  evidence, delete reservations or trigger duplicate messages.
- A DB/commit failure must leave no acknowledged partial batch, processed status or advanced cursor.
- Orphan pending events cannot monopolize a limited batch, defeat capacity or extend their own retention.
- Signed but unrelated events and corrupt persisted rows cannot inject raw errors/private data into logs,
  mutate Telegram/email history or create unearned marketing metrics.
