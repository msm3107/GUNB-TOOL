# Private WhatsApp Webhook Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Separate opt-in loopback webhook process with private startup checks and bounded sync workers.

**Architecture:** Environment-only service builds existing WAL/FULL factory/app after preflight; optional pinned Gunicorn serves fixed loopback profile. No Telegram runtime or public TLS activation.

**Tech Stack:** Python>=3.10, optional Gunicorn26.2.0, SQLite, pytest/systemd example.

**Spec:** `docs/superpowers/specs/2026-10-14-whatsapp-service-design.md`

## Global Constraints

- Schema v15, no bot config/main.py/default dependencies changes, no live sends or deployment.
- Separate env with literal WHATSAPP_WEBHOOK_ENABLED=true; disabled errors before DB/server.
- Fixed loopback127.0.0.1:8765, two sync workers, timeout30/graceful10/backlog32, limits as spec.
- No raw query/body/args/secrets/path/traceback diagnostics or stock print-error wrapper.
- Existing-only WAL/FULL preflight connection closed before fork; no request-time migration.
- Check works Windows/no listener; serving Linux only, other platforms rejected before DB.
- Templates .example not installed/enabled, final proxy/update lifecycle/retention activation gates remain.
- Final ALL-token/API/server guide and bot-preserving rollout remain mandatory.

## Review Focus

- Disabled/invalid config must not touch DB or bind/import server (Task1 env-order tests).
- Inherited Gunicorn env/config must not override fixed bind/trust/logging (Task1 actual POSIX config validation).
- Raw provider/query/exception data can leak via server logger or wrappers (Task1 logger/CLI/runtime privacy tests).
- Startup must close all SQLite before forking and reject stale/missing table (Task1 tracked preflight).
- .example service must not be installed or raced against existing updater/migration (Task2 static installer/template docs gate).

### Task 1: Startup boundary and private server profile

**Files:** create `gunb_tool/whatsapp_service.py`, `gunb_tool/whatsapp_server.py`, `requirements-webhook.lock`, `tests/test_whatsapp_service.py`; modify `.github/workflows/tests.yml` (install optional lock for Linux tests).

**Interfaces:** build_application(environ=None) -> WSGI app; main(argv=None)->exitcode; PrivateLogMixin; server make_server(app) -> actual BaseApplication subclass with fixed profile, serve(app) calls Arbiter.run without unsafe wrapper.

- [x] Write tests for complete env/CLI/privacy/order matrix, preflight closure/signature/replay, fixed config/logger and actual POSIX adapter without bind.
- [x] Run RED absent modules, save evidence; implement only after valid RED.
- [x] Implement minimal service/server and optional pin, CI install. Verify no core runtime imports/changes and no auto configuration/credential logging.
- [x] Focused service+factory+ingress PASS and full pytest PASS with documented OS skips. Diff/privacy check, commit, task-done focused PASS.

### Task 2: Inactive service template, documentation and review

**Files:** create `deploy/gunb-whatsapp-webhook.service.example`, `deploy/whatsapp-webhook.env.example`; modify `.gitignore` (real sidecar secrets/backups), `README.md`, `docs/WDROZENIE.md`, plan.

**Interfaces:** faithful operational boundaries/template, reviewed PR; no install/start.

- [x] Document env/check/private runner/limits/trust and optional dependency, disabled templates, update coordination gate, rollback and final all-token server instruction.
- [ ] Static template/installer assertions, whole diff/privacy/gitdiffcheck, commit/push/create/attach PR.
- [ ] Independent readonly whole-PR review exact BASE/HEAD/spec/plan/ledger/tests/CI; save report, verify/fix findings and same-reviewer material follow-up.
- [ ] Accepted review/final exact-head CI allSUCCESS/hasMorefalse; final plan-only status commit with unchanged accepted-code check.
- [ ] Task-done clean verifier of accepted code/full evidence/final CI; save local artifacts, hand off without merge/deploy.
