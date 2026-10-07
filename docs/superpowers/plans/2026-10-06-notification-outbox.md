# Notification Outbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement inline, as in PR #7. Track checkbox steps; one independent whole-branch review.

**Goal:** Add bounded reports, durable endpoint/outbox operations and an injectable worker.
**Architecture:** New Python modules using the existing v13 SQLite tables and transaction API. No runtime hook or provider adapter; preserve all existing Telegram behavior.
**Tech Stack:** Python >=3.10, standard library, SQLite/WAL, existing pytest; no dependencies.
**Spec:** `docs/superpowers/specs/2026-10-06-notification-outbox-design.md`.

## Global Constraints

- Start from merged main `92a70ea1f64d7c496d59766cef6823cfae885bc2`, branch `codex/notification-outbox`.
- Keep schema v13, Python >=3.10 and dependencies; no production DB, secrets or messages.
- Keep CLI, scheduler, UI and both unavailable-sender config gates unchanged.
- No merge/deployment. Use local reviewer prompt and save feedback in ignored `.local-review/`.
- API callers must open their repository in the consuming thread; never keep a transaction during dispatch.
- NotificationStore writes must reject an external transaction before mutation; each operation owns its transaction.

## Review Focus

- Retrying a job after restart retains the first message, not freshly rendered data.
- A lead delivered by Telegram remains eligible for each new endpoint independently.
- New address/paused account/revoked consent/stale revision never dispatches an old queued snapshot.
- Timeout/crash after possible acceptance never causes automatic resend; late/foreign ACK is handled safely.
- Queue, payload and loop limits isolate one recipient; concurrent writers cannot reserve the same revision twice.

### Task 1: Reports, endpoints and durable enqueue

**Files:** Create `gunb_tool/notification_models.py`, `notification_reports.py`,
`notification_store.py`, `tests/test_notification_store.py`.
**Interfaces:**
- `NotificationEndpoint`, `LeadRef`, `ReportPart`, `Claim`, `DeliveryResult`: frozen dataclasses.
- `build_report(endpoint, investments, *, now, part_size=20) -> tuple[ReportPart, ...]`.
- `NotificationStore(repo, *, admins=(), access='approval', max_pending_parts=40, max_age_days=30, lease_seconds=120)`.
- `add_endpoint(chat_id, channel, address, *, mode='rano') -> NotificationEndpoint`.
- `get_endpoint(chat_id, endpoint_id) -> NotificationEndpoint` or generic `NotificationError`.
- `record_verification(chat_id, endpoint_id, *, expected_version)` and
  `record_consent(chat_id, endpoint_id, *, expected_version, source)` return updated endpoint.
- `set_enabled(chat_id, endpoint_id, enabled, *, expected_version)`,
  `change_address(chat_id, endpoint_id, address)`, `revoke_consent(chat_id, endpoint_id)` return updated endpoint;
  `delete_endpoint(chat_id, endpoint_id)` removes only owned data.
- `candidates(chat_id, endpoint_id, since: datetime, *, limit=20) -> list[Investment]`.
- `enqueue(chat_id, endpoint_id, event_key, parts, *, expires_at: datetime) -> tuple[int, ...]`;
  empty tuple means no new reservation; repeat event returns original IDs.

- [x] Write `test_report_round_trip_keeps_facts_and_revisions`, validation/ownership/proof tests,
  `test_telegram_delivery_does_not_suppress_channels`, filter-before-limit, snapshot idempotency,
  pending/history dedupe, owner/version/revision/TTL checks, queue caps and concurrent enqueue.
- [x] Run `python -m pytest tests/test_notification_store.py`; expected missing new modules/API.
- [x] Implement the models/report builder/store per spec. Reuse `UserFilters.matches`,
  `BotStore.get_user` and `LeadRepository.transaction`; leave their implementation untouched.
- [x] Run store tests and full `python -m pytest -rs`; 54 store tests and 996 full-suite tests passed,
  1 POSIX-rights test skipped on Windows. Unicode splitting and imported-proof regressions RED→GREEN.
- [x] `git diff --check`; commit models, reports, store and tests.

### Task 2: Safe claim, dispatch and completion

**Files:** Extend `notification_store.py`; create `gunb_tool/notification_worker.py`,
`tests/test_notification_worker.py`; update `README.md`, `docs/WDROZENIE.md`.
**Interfaces:**
- Consumes Task 1 models, `NotificationStore`, `ReportPart`.
- `claim(channels: Sequence[str]) -> Claim | None`.
- `prepare_send(claim) -> tuple[NotificationEndpoint, ReportPart] | None`.
- `complete(claim, result: DeliveryResult, *, max_attempts=5) -> bool`.
- `Sender.send(endpoint, report, *, idempotency_key: str) -> DeliveryResult` protocol.
- `NotificationWorker(store, senders: Mapping[str, Sender], *, max_attempts=5)`;
  `run_once(*, limit=25) -> int` returns handled claims, bounded 1–100.

- [x] Write claim concurrency/per-endpoint serialization, retry/Retry-After/idempotency, accepted≠read,
  stale ACK, lease/crash/late ACK, revalidation, provider exception, disabled-channel, corruption,
  bounded loop, receiver isolation and no-transaction-during-dispatch tests.
- [x] Run `python -m pytest tests/test_notification_worker.py`; expected missing worker/API.
- [x] Implement claim/revalidation/completion and bounded worker per spec; no provider adapters.
- [x] Document current preparatory stage, operational limits and pre-enable requirements.
- [x] Run affected notification tests, full `python -m pytest -rs`, `git diff --check`; commit.
  130 notification tests passed; full suite: 1029 passed, 1 POSIX-rights test skipped on Windows.
  After INT-01: 141 notification tests and 1040 full-suite tests passed; same single Windows skip.
- [ ] Push feature branch, create/attach PR against main; verify exact-HEAD CI.
- [ ] Fresh reviewer of complete PR per ignored local prompt; resolve real findings with regressions
  and save review feedback before handing the PR over for acceptance.
  Initial review: INT-01/P2 requires standalone write transactions and regressions for partial enqueue/receipt.
  PERF-01/P3 remains a documented measurement gate before production scheduler integration, owned by its implementer.
