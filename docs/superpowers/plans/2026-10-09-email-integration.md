# Email integration implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans, inline continuation of the authorized staged rollout. Track checkboxes; one fresh independent whole-PR reviewer per local AGENTS.

**Goal:** Opt-in e-mail reports managed in the existing Telegram bot without blocking it.
**Architecture:** Optional EmailCommands, dedicated EmailThread with its own repository,
existing outbox/worker and a supervised spawn process per SMTP call. No schema change.
**Tech Stack:** Python>=3.10 standard library, SQLite/WAL, pytest, existing SMTP adapter.
**Spec:** docs/superpowers/specs/2026-10-09-email-integration-design.md.

## Global constraints

- Base079559949fc9d9296dc4aca228404ca77243c4ee, branchcodex/email-integration; existing managed worktree.
- v14 unchanged; email defaultFalse, WhatsApptrue stillConfigError. Preserve Telegram/pipeline behavior.
- SMTP credentials only env, no raw errors/addresses/tokens/credentials in logs/repr/export.
- No SQLite connection shared across threads/processes; no SMTP in DB transactions or UI thread.
- No real sends, secrets or production data; no merge/deploy.
- Limits:45s supervised SMTP,120slease;25verification requests/60s/oneowner;10endpoints/200leads/cycle,
  parts20, pending40, TTL24h; quiet22–06 Warsaw; retention7/30/max(90,maxage+7)days, batches2000.

## Review focus

- Accepted SMTP result survives process cleanup; deadline/crash cannot trigger automatic duplicate.
- Stop, queue saturation/restart and a hung SMTP cannot block Telegram polling or leak token data.
- Revocation/access loss/address/version change prevents queued dispatch; own unsubscribe remains reachable.
- Daily/instant scheduling and restart/DST do not duplicate or starve endpoints under bounded batches.
- Default-off and export/import remain safe; retention never erases ambiguous outcomes or dedup evidence in window.

### Task 1: Validated SMTP configuration and privacy

Files: config.py, main.py logging, config.yaml, .env.example;
tests/test_email_config.py, historical test_notification_foundation.py descriptions.
Produces EmailConfig(enabled:bool=False,smtp:SMTPSettings|None=None),
_email(data:dict,env:Mapping)->EmailConfig. Allowed keys and values as spec;
SMTP_USERNAME/PASSWORD required when active, no YAML credentials. RedactingFormatter
must include short SMTP private values. Existing export sanitizer verified with real package.

- [ ] Write tests default/valid STARTTLS+implicit/missing auth/invalid values/unknown credentials,
  no private repr/errors, short-log masking and exported package without SMTP values.
- [ ] Observe RED; implement parsing/privacy; pin old email gate tests to incomplete config semantics.
- [ ] Run config/foundation/migration/main tests, diffcheck; commit.

### Task 2: Supervised SMTP

Create smtp_process.py, tests/test_smtp_process.py.
Produces SupervisedSMTP(settings,*,should_stop=lambda:False,timeout_seconds=45,_context=None)
with send/send_verification matching existing adapters. _smtp_child communicates only bounded
JSON result bytes; spawn/default, daemon child, no DB. Stop before start retry; post-start
ambiguity unknown. poll<=0.2s; terminate/join1s/kill/join1s cleanup, accepted preserved.

- [ ] Write fake-process tests all outcomes, malformed/EOF/start failure, deadline/stop,
  ack+cleanup, no input logging; real spawn invalid recipient fails before any SMTP.
- [ ] Observe RED; implement; run sender/process/worker tests; diffcheck; commit.

### Task 3: Owned bot commands and verification request queue

Create email_bot.py, tests/test_email_bot.py. Modify notification_store.py and bot.py only hooks/APIs.
Produces VerificationRequests.submit(chat_id,endpoint_id)->bool, take()->tuple|None,
done(chat_id)->None; bounded/TTL/dedup owner. EmailCommands(store,requests,send).
handle(user,args)->None implements exact /email verbs in spec; no provider calls.
Consumes EmailVerification.consume; NotificationStore list_endpoints, set_mode,
has_unresolved APIs, standalone writes/version checks. LeadBot optional email_commands hook.

- [ ] Write owner/channel/private/access/pause/consent/code/mode/address/version/off/delete,
  unknown-quarantine and generic-response tests; queue bounded/TTL/restart/no plaintext.
- [ ] Observe RED; implement helper/store hooks; unchanged default bot menu/flows.
- [ ] Run bot/email/store/verification tests, diffcheck; commit.

### Task 4: Scheduled worker, retention, health and runtime wiring

Create email_runtime.py, tests/test_email_runtime.py; modify main.py lifecycle,
health.py, README and WDROZENIE. EmailJobs(store,requests,sender,settings,*,max_leads=200,
should_stop=lambda:False).run_once()->int; EmailThread(config,requests,stop) owns repo
and creates SupervisedSMTP. Retention prune_notifications(repo,max_age_days)->None
called by existing nightly maintenance even if email disabled. Main --bot wires
only enabled email; --bot-once one cycle; dry-run+bot rejected before DB/network.
Health reads heartbeat email_worker and ambiguous/failure counts only for enabled email.

- [ ] Write report/DST/quiet/TTL/recheck/restart/paging/backpressure/stop/thread isolation tests;
  retention boundaries/dedup/unknown preservation, enabled/disabled lifecycle and health read-only.
- [ ] Observe RED; implement using existing state and outbox. Deterministic event keys and
  per-endpoint schedule markers; no unrelated bot/pipeline refactor or schema mutation.
- [ ] Benchmark PERF-01 on100k fictitious records; document measured limits and operator gates.
- [ ] Document config, commands, rollout/off/rollback, no exactly-once/read claims and remaining Meta stage.
- [ ] Run affected and full pytest, diffcheck; commit/push; create/attach PR.
- [ ] Fresh independent security/performance/marketing review; save ignored reports; resolve real
  findings with regressions and same-reviewer follow-up. Exact final-HEAD CI; handoff without merge.
