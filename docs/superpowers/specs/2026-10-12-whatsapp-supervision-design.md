# Supervised WhatsApp dispatch

Date: 2026-10-12. Authorized continuation after merged PR #13, base
`774f8dbb346765a98aecbdac4778c3775817e20a`.

## Intent and scope

Close the sender's DNS/hung HTTP activation gate using the existing SMTP pattern:
one disposable `multiprocessing` spawn child per operation, with no database in
the child. Preserve Telegram, email, schema v15, provider classification and the
durable preflight proof. This is an internal adapter, not activation: config still
rejects `whatsapp.enabled: true`, and there is no listener, scheduler or command.

An in-thread timeout cannot stop DNS or a blocked request. A persistent pool adds
shared lifecycle/secret state without helping this sequential worker. A small
WhatsApp-specific supervisor avoids refactoring the working SMTP supervisor and
can enforce WhatsApp ACK semantics at its own boundary. No new dependencies.

## Process and result boundary

`SupervisedWhatsApp(settings: WhatsAppSettings, *, should_stop=lambda: False,
timeout_seconds=45, _context=None)` implements the existing Sender `send` method
and exposes validated `WhatsAppRouting` through `.routing`. Validate settings,
callable stop and a finite numeric budget `0 < timeout_seconds <= 45`; errors are
generic and do not echo credentials. Production always uses spawn, not fork.

Start the monotonic deadline before creating resources. A stop before starting a
child returns retry with no network; once starting is attempted, errors, deadline,
stop, abnormal exit or missing/malformed IPC return unknown. There is no automatic
retry after ambiguity. A valid child result preserves the existing accepted,
retry, failed or unknown outcome; delivered/read cannot originate at this boundary.

Pass only validated settings, immutable prepared endpoint/report and the stable
idempotency key. Never pass a store, repository, connection, bot or environment
dump. The child calls WhatsAppSender once, catches exceptions without printing
private details and sends only JSON outcome/provider ID/Retry-After through a
one-way pipe, then closes it. No pickle result, raw response, exception or body.

Cap result IPC at 2048 bytes, exact three-key object, strict JSON (duplicates,
nonfinite numbers, excessive depth and invalid UTF-8 rejected). Accepted requires
a valid wamid; all other outcomes require null provider ID. Retry-After must obey
DeliveryResult's existing range/type and belong to retry only. The child and the
parent validate this contract. Invalid results become unknown, never success.

Poll child completion in <=0.2s joins within the remaining budget. Read result
bytes only after the child has exited and the parent's writer is closed: readable
pipe alone does not prove a complete frame, and a suspended writer must not hang
the supervisor in recv_bytes. Small result fits the pipe; no persistent queue.

Always close both pipe ends. Cleanup uses terminate, a join of <=1s, kill if still
alive, another join of <=1s, then closes the dead process handle. Cleanup failures
do not erase a validated ACK. The 45s wait plus up to two 1s cleanup joins is below
the existing default 120s lease under normal OS operation. Spawn/start, OS calls
and scheduling have no absolute real-time guarantee; future runtime must reserve
at least a 60s lease and keep this operation outside Telegram's polling thread.
Do not claim exactly-once or automatic recovery of unknown without provider ID.

## Verification

Write failing tests first for strict IPC/classification, stop before/after start,
deadline and stuck writer, process startup/crash, cleanup fallback/ACK preservation
and privacy. Controlled process doubles exercise OS faults; real spawn tests
cover invalid recipient without HTTP, a genuinely stalled child and valid compact
ACK transport. Test-only process targets live in test helpers, not production.

A real SQLite worker test intercepts the process boundary after committed routing,
then verifies accepted vs unknown persistence, quarantine and no second dispatch.
Use synthetic data only. Full pytest and CI on exact final HEAD, plus mandatory
independent readonly whole-PR security/performance/marketing review.

## Rollout and handoff

No schema/config/env change, server operation or real Meta request. Code rollback
to PR #13 stays on v15; existing earlier schema rollback rules still apply.
Remaining gates: bounded HTTPS ingress with its own connection owner, number
verification/opt-in/revocation, compact reports, pacing/night policy, runtime
supervision, operator recovery, current Meta policy/template validation and pilot.

The final rollout PR must include the user's complete token/ID/secret and server
deployment guide: SMTP and Meta plus existing integrations, secret permissions,
HTTPS/DNS/proxy, disabled-channel installation, backup, controlled restart of one
Telegram poller, health/pilot activation and rollback with its data-loss limits.
This remains a release gate, not authorization to deploy or send now.
