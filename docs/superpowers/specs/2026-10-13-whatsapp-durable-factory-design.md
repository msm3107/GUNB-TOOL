# Request-owned durable WhatsApp inbox factory

Date: 2026-10-13. Next authorized staged PR after merged #15,
base `060646352194d629c4994cbee812ec9cb415d075`.

## Scope and choice

Close PR15 SEC-01's storage integration gate without activating WhatsApp. Reuse
LeadRepository/NotificationStore/WhatsAppInbox and the WSGI callable. Add an opt-in
existing-only repository opening mode, then a small request context factory.
Default bot/email repository initialization remains unchanged. No listener,
runtime/config/env wiring, dependency, schema migration or live sends/deployment.
This is a bounded extension of the existing intake flow, not a server redesign.
Repeated user authorization to execute staged PRs applies; execute inline and
obtain the mandatory independent whole-PR review before handoff.

Avoid a second hand-written repository (duplicates transactions/clock APIs), and
avoid merely checking the file/version before ordinary initialization (TOCTOU can
still create/migrate the DB). Enforce existing-only at SQLite open and bypass
migrations on the same connection used by the request.

## Repository mode

`LeadRepository(..., existing_only: bool = False)` preserves all defaults.
The opt-in mode rejects :memory:, opens a path-escaped absolute file URI with
`mode=rw`, never mkdir/create/migrate/backup, and requires exactly SCHEMA_VERSION
and existing journal_mode=wal. Missing/empty/old/future/non-WAL databases fail;
no automatic schema or journal conversion. Normal mode still initializes and
migrates with its current backups, WAL/NORMAL and error types. No public URI input
contract is added. Common connection setup errors close the opened connection.
All opened connections retain foreign_keys=ON, busy_timeout=5000ms and thread
ownership. Existing-only does not mean read-only: ordinary inbox DML is allowed.
SQLite may manage WAL/SHM files; do not claim byte-for-byte filesystem immutability.

## Factory contract

`WhatsAppInboxFactory(db_path, settings, *, max_events=10000)` stores a pinned
absolute ordinary local file path, immutable WhatsAppWebhookSettings and bounded
capacity (exact int 1..100000). Reject empty/:memory:/file: URI/UNC input and invalid
settings/capacity with generic ValueError. Constructor opens no database.
Path and secrets must not appear in repr/logs/public failures.

Calling it returns a context manager. Enter creates a fresh existing-only
LeadRepository in the caller's thread, outside a transaction sets synchronous=FULL
and reads back synchronous==2 and journal_mode==wal before yielding WhatsAppInbox.
Store/Inbox use that repository and exactly the supplied settings. No shared
connection/cache/thread, no transaction kept open around HTTP. Close on every
normal/exceptional exit and initialization error. Do not suppress receive/COMMIT
or context errors. Ingress retains generic 503 and ACK-after-receive/exit behavior;
HMAC/body validation must still precede invocation of the real factory.

Hold a bot-like second connection open in tests: request FULL must not change
that connection's NORMAL. Confirm each request FULL before receive, actual COMMIT
visibility before HTTP start_response and closure before response, replay and
independent concurrent thread ownership. Faults (missing/stale DB, FULL setup,
receive/COMMIT/close) must never produce 200. No OS/power-loss test is claimed.
WAL/FULL still requires a local filesystem/OS/disk honoring synchronization;
rejecting obvious UNC cannot identify every network mount.

Primary references checked 2026-10-13:
[SQLite URI mode=rw](https://www.sqlite.org/uri.html),
[SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous).

## Remaining gates and rollback

Schema stays v15. Returning to PR15 is code-only (normal backup/restart procedures);
return to v14 still needs pre-v15 backup and loses later data. No active bot change.
Remaining server gate: trusted HTTPS/WSGI/proxy, framing/body buffering/limits,
deadlines/global limits/log redaction, startup migration before serving, opt-in,
number proof/revocation, compact reports/pacing/night policy, own worker/retention,
reconciliation/operator recovery, current Meta requirements and pilot.
Final rollout PR MUST contain ALL token/ID/secret setup (SMTP/Meta/Telegram and
optional integrations) and bot-preserving server install/update, backup,
permissions, one poller, controlled restart, health/activation and rollback with
restore data-loss limits. No real credentials needed in this internal stage.
