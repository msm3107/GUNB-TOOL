# Durable WhatsApp status inbox

Date: 2026-10-11. Continuation after merged PR #11 (`66d6edf383b754beb0d59e9e8da5c1d6028f2751`).
The user's staged rollout authorizes implementation and a feature PR; merges and
production activation remain with the user.

## Boundary and architecture

Add `WhatsAppInbox(store, settings, *, max_events=10000)` in
`gunb_tool/whatsapp_inbox.py`. Reuse NotificationStore's repository and existing
v14 tables. No dependency, migration, HTTP server, config/CLI changes or runtime
worker registration. Telegram and email sending remain unchanged. The only active
maintenance change is bounded deletion of old WhatsApp webhook records.

`receive(raw_body: bytes, signature: str | None) -> int` authenticates and parses
the entire batch through WhatsAppWebhook before opening its own BEGIN IMMEDIATE
transaction. It inserts only new `(channel, event_key)` rows and returns the
number inserted **after commit**. A future HTTP adapter may acknowledge only a
successful return (including zero duplicates); authentication, capacity and DB
failures must not receive a success ACK. This API accepts no externally supplied
status DTO. Existing parser bounds (64 KiB, 100 statuses) apply.

The durable payload has exactly `version=1`, `outcome`, provider `timestamp` and
`recipient_digest=SHA256(wamid + NUL + recipient_id)`. Provider message ID and event
key use existing columns. No raw body, clear recipient, message content, provider
error, secret, contact or billing data is stored or logged. The digest is still
pseudonymous data. `received_at` is local receipt time, preserved on replay.

The whole inbox table has a configurable hard row budget, integer 1..100000,
default 10000. Count at most budget+1 rows; check novelty before capacity so that
duplicates succeed at capacity. A batch either fits completely or raises generic
`WebhookInboxFull` without partial writes. Serialize competing writers; no silent
INSERT OR IGNORE masking database errors. The future service must bound HTTP
ingress before reading and schedule retention/reconciliation.

## Reconciliation

`reconcile(*, limit=100) -> int` consumes at most 1..100 pending rows per call in
one short owned transaction, returning the number marked processed. It uses the
existing partial pending index in `(received_at, rowid)` order and an in-memory
keyset cursor. Each subsequent call advances past unmatched records, wraps on
the next call at end, and never processes more than its budget. On restart the
cursor resets, but pending work remains durable; an uninterrupted sweep is at
most ceil(10000/limit)+1 calls at the default capacity. Cursor advances only
after successful commit. Competing workers serialize and recheck pending rows.

Correlate by provider ID against **all** outbox rows, reading at most two matches;
ambiguous IDs must never be resolved by guessing an endpoint. The single match
must be WhatsApp, state accepted/delivered/read/failed/unknown, its strict report
snapshot must belong to the current endpoint owner, and the digest must match
the current canonical WhatsApp address. No matching by phone alone, no guessed
assignment for a lost API ACK. An event received before the API response remains
pending and becomes matchable after the worker persists the provider ID.

Conservative limitation: there is no historical recipient snapshot in outbox v1.
After a number changes, statuses for the former number remain unmatched until
retention; pause/revocation with an unchanged number still permits bookkeeping.
Persisted routing proof for changed numbers/multiple sender accounts is a gate
before live activation. Never bypass recipient matching to hide this limitation.

Successful progress is `accepted < delivered < read`, independent of arrival
order/provider timestamps. A failed status can change accepted/unknown to failed
and reuse NotificationStore's endpoint quarantine (cancel queued/retry siblings).
It cannot downgrade delivered/read. An accepted status cannot clear failed;
delivered/read can resolve failed/unknown, but **never reenable** an endpoint.
No webhook retries a send or changes consent, verification, account or bot state.

Update only history rows for the matched outbox, endpoint and original immutable
lead revisions, with monotonic outcomes; never recreate deleted investments or
invent delivery history. Failed preserves the existing accepted reservation.
Outbox, histories, endpoint quarantine and processed event commit atomically.
Malformed stored records are marked processed without delivery mutations; valid
unmatched/ambiguous/mismatched events remain pending for the bounded retention.

## Retention, compatibility and performance

Existing nightly `prune_notifications` deletes at most 2000 WhatsApp events whose
**first receipt** is more than seven days old, processed or pending, even when
channels are disabled. It leaves email webhook rows alone. Replays do not extend
receipt time. After retention a replay may create a new event; monotonic state
and matching still prevent resending or downgrade. This is bounded deduplication,
not permanent exactly-once delivery. Linked events cascade on endpoint deletion;
unmatched pseudonymous events expire through retention.

Use the existing provider and partial pending indexes; verify plans and measure
synthetic 10000-event reconciliation rather than add a migration speculatively.
Retention can scan the capped table; its DELETE batch remains bounded. Larger
capacity/throughput requires measurement and possibly an indexed schema migration.
Rollback to PR #11 reads the same schema; it simply does not process the inbox.

## Acceptance and deferred activation

Real temporary SQLite tests cover durable reopen, duplicate concurrent ingress,
capacity/atomic rollback, invalid authentication before writes, external transaction
refusal, event-before-ACK, unknown without ID, ambiguous/cross-channel/owner/address
matches, all ordering pairs, failure quarantine, revoked endpoints, competing
reconcilers, malformed persisted data, fair bounded sweeps and retention/replay.
Tests use synthetic bodies and no network. Full suite and exact-head CI must pass;
independent whole-PR review follows the local mandatory reviewer prompt.

No user-facing claim that WhatsApp is available. Before activation still require
HTTPS ingress/body limit/ACK integration, isolated thread/connection and supervision,
verified number and distinct opt-in/revocation, recipient/sender routing proof,
short template reports, pacing/night policy, operational unknown/failed recovery,
current Meta template/API/policy checks and real pilot delivery evidence. This PR
implements local database behavior and makes no new external platform claims.
