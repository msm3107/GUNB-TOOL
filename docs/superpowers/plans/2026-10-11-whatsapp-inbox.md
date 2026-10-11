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

- [x] Write real SQLite tests for signed minimal storage, durable reopen, replay, invalid batch/auth
  rejection, no plaintext private data, capacity including duplicates/full batch, outer transaction
  rejection, trigger-induced rollback and concurrent independent connections.
- [x] Run `python -m pytest tests/test_whatsapp_inbox.py -rs`. Expected RED: absent inbox module.
- [x] Implement bounded authenticated ingress, own transaction, novelty/capacity and atomic inserts.
- [x] Run same tests, inspect output and `git diff --check`. Expected PASS. Commit intake/tests.
- [x] Complete task with same pytest verification command.

### Task 2: Correlation and monotonic status transitions

**Files:** Extend inbox and its tests; consume existing NotificationStore quarantine and strict report model.
**Interfaces:** Consume Task1 persisted rows; produce `.reconcile(*, limit=100) -> int`,
bounded keyset traversal and atomic delivery/outbox/event bookkeeping.

- [x] Add tests for all status order pairs, failure quarantine/late success, early event then ACK,
  late ACK, lost ACK without ID, wrong/changed recipient, ownership, email/ambiguous provider IDs,
  revoked/pause with unchanged address, malformed rows, no fabricated histories, worker races,
  restart, concurrent reconcilers, fair sweep/limits and DB rollback including cursor.
- [x] Run inbox tests. Expected RED: reconciliation absent.
- [x] Implement spec matching/ordering/quarantine/history updates and fair cursor after commit.
- [x] Run inbox and notification store/worker/webhook tests. Expected PASS; diff clean.
- [x] Commit implementation/tests; complete task with those tests.

### Task 3: Retention, integration evidence and PR

**Files:** Modify `gunb_tool/email_runtime.py`, `tests/test_email_runtime.py`, README.md,
docs/WDROZENIE.md; extend inbox tests if needed.
**Interfaces:** Consume Task1 first receipt and Task2 monotonicity; extend existing nightly
`prune_notifications` with one <=2000 deletion batch for WhatsApp events older than seven days.

- [x] Add retention tests (pending/processed/replay/future provider timestamps/email isolation/
  batch bound/account deletion) and run them. Expected RED: old events survive current maintenance.
- [x] Implement bounded retention; run inbox/email runtime tests. Expected PASS.
- [x] Inspect EXPLAIN plans for provider lookup and pending keyset; measure synthetic 10000-row
  fair reconciliation and save ignored results. Document scope, privacy, limits, changed-number
  limitation, ACK contract, rollback and remaining activation gates honestly.
- [x] Run `python -m pytest -rs` and `git diff --check`. Expected PASS with only Windows POSIX skip.
  Commit, push feature branch, create/attach PR.
- [x] Dispatch fresh readonly whole-diff reviewer with exact BASE/HEAD/PR/spec/plan/tests/CI;
  save report, verify/fix findings with regressions and same-reviewer followup where material.
- [x] Record completion, verify exact final HEAD CI and hand off without merge/deployment.

## Review Focus

- A valid provider ID is not enough if channel, owner or recipient differs; old-address status
  must never update a new recipient or enable sending.
- Repeated failures after delivered/read and late worker acknowledgements cannot downgrade
  evidence, delete reservations or trigger duplicate messages.
- A DB/commit failure must leave no acknowledged partial batch, processed status or advanced cursor.
- Orphan pending events cannot monopolize a limited batch, defeat capacity or extend their own retention.
- Signed but unrelated events and corrupt persisted rows cannot inject raw errors/private data into logs,
  mutate Telegram/email history or create unearned marketing metrics.

## Completion — 2026-10-11

PR [#12](https://github.com/msm3107/GUNB-TOOL/pull/12). Whole-PR independent review
of `64cb221beb3ea4a44e99d07aa094c0292fcab5bb` found one P2, REL-01: timezone
overflow in a corrupt durable event/report could halt a batch. Four regressions
failed before the two local validation boundaries were corrected. Same-reviewer
follow-up of code HEAD `dce176e7e56aa35f1830aebcbcacf880a3a66a2a`: AKCEPTUJ,
REL-01 closed, eight independent targeted tests passed, no new findings.

Final implementation: 1443 passed, 1 skipped (POSIX permissions on Windows),23.27s;
inbox/runtime 116 passed. Code HEAD CI: 12/12 SUCCESS, Python3.10–3.14 and
deploy-scripts, push+PR. No further implementation changes follow that SHA.
This final commit only records plan status; its own exact-head CI must also pass
before handoff. Review/CI/decision/performance records remain local and ignored.

The v14 schema and configuration gate remain unchanged. WhatsApp is still
inactive; no HTTP listener, sending service, merge or deployment. Former-number
status/lost ACK routing, production HTTPS/ACK, opt-in, supervision and real Meta
pilot remain explicit activation gates in the spec/operator guide.
