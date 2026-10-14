# Private WhatsApp webhook service

Date 2026-10-14; base merged PR16 `f03e35ec5d972ac0ff4b43c5351f19e728c3b908`.
Authorized staged inline continuation of email/WhatsApp rollout. Extend the existing
WSGI/factory flow into a separate opt-in Linux service; preserve Telegram runtime.

## Intent and decisions

Use optional pinned Gunicorn 26.2.0, not a hand-written HTTP server. It supports
Python >=3.10/UNIX and sync process workers. Two sync workers bound active intake;
a timed-out worker can be killed without blocking Telegram. Keep it on fixed
127.0.0.1:8765, not public HTTP. No nginx/TLS activation, sends, number proof, schema
or bot config changes here. Production proxy and final install/update lifecycle
remain explicit activation gates. Service template is .example, never auto-installed.
Do not extend main.py or start another Telegram poller. Optional dependency file
is separate from normal bot lock; CI installs it to validate actual server config.

## Service boundary

`python -m gunb_tool.whatsapp_service [--check]` reads only explicitly supplied
process environment, no .env auto-discovery, config.yaml, Telegram or SMTP setup.
`WHATSAPP_WEBHOOK_ENABLED=true` is required literally; missing/false/other values
fail before any DB/server access. Required nonempty environment:
WHATSAPP_WEBHOOK_DB_PATH (absolute ordinary local path), WHATSAPP_WABA_ID,
WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_APP_SECRET, WHATSAPP_VERIFY_TOKEN. Existing
WhatsAppWebhookSettings and WhatsAppInboxFactory perform validation. Secrets stay
out of args, repr, logs and errors. No access token needed for receiving statuses.

`build_application(environ=None)` creates settings/factory and performs one startup
factory context preflight: exact existing schema/WAL, FULL verified, and required
inbox table readable. Close connection before returning WSGI app or forking; never
initialize/migrate/backup. Failure is a generic WebhookServiceError without raw
path/values/traceback. Body/HMAC-before-request-connection remains in the app.
`--check` returns 0 only after preflight, opens no socket/imported server, does not
prove HTTP/TLS/health/disk durability. Other config errors return 2; runtime faults
return 1 with fixed messages. Windows supports check, serving fails with 2 before
DB/server import (this profile serves on Linux only). Unsupported arguments never echo their values.

## Gunicorn profile and trust

Own BaseApplication subclass; do not parse Gunicorn CLI, GUNICORN_CMD_ARGS or
gunicorn.conf.py. Clear inherited Gunicorn defaults and FD/socket-activation variables
before import (private service process only), preventing an inherited listener from
bypassing the fixed bind. Override its print-on-config-error path. Fixed profile:
bind127.0.0.1:8765, workers2, worker_class=sync, threads1, timeout30,
graceful_timeout10, backlog32, max_requests1000+jitter100, keepalive0, HTTP1 only,
pure Python HTTP parser, no reload/daemon/control socket/debug/spew, no access log.
Request line4094, fields32, field size4096. header_map=refuse, forwarder_headers=[],
PROXY protocol off. Trusted scheme only X-FORWARDED-PROTO=https from127.0.0.1;
never wildcard trust or SCRIPT_NAME/PATH_INFO header overrides. Future local proxy
must overwrite that header after real TLS. Loopback processes are a trust boundary:
do not expose port/container mapping or use on an untrusted shared host. HMAC still
required. Gunicorn timeout is worker supervision, not an absolute OS-time guarantee;
proxy buffering/read/write/global limits remain required before public exposure.
Gunicorn's own HTTP parse-error responses may reflect caller-supplied malformed
input; the future public proxy must normalize those error bodies without changing
the error status. This PR claims private application/server diagnostics, not a
verified public error-response policy.

Use private logger mixin/subclass: discard all raw messages/args/exception traces
and access records; fixed lifecycle/warning/error messages only, debug discarded.
Do not use stock run/config wrappers that print raw errors. Main catches startup
and runtime errors generically. Expected signals/clean SystemExit retain exit0,
other SystemExit returns1 generically. No real server started on local Windows.

## Deployment template

Example systemd unit uses gunb user/group and separate
/var/lib/gunb-tool/whatsapp-webhook.env (600), PYTHONPATH to installed release,
fixed module command, no bot secrets file. UMask0077, standard project sandbox,
write state only, timeout stop30, restart on failure except config2. A separate
.env example defaults false, placeholders empty, no real credentials. Templates
are not copied/enabled by installer. Do not start it until proxy/lifecycle/retention
gates are finished. Current updater stops only bot; final rollout must coordinate
webhook stop/start with migration, release switch, backup/restore and rollback.

## Validation and handoff

TDD synthetic env/temp SQLite and fake server boundary; validate default-disabled
before DB, malformed/missing secrets private, preflight close/no migration, real
signed WSGI POST through constructed app, check no bind, Windows serving guard,
runtime errors/SystemExit/privacy, fixed profile vs hostile Gunicorn env/config,
logger privacy. POSIX CI imports pinned actual dependency and validates real
Gunicorn config/logger without bind. Full pytest and exact-head CI, independent
whole-PR security/performance/marketing review, local ignored reports. No claim of
tested public HTTPS, live Meta, OS power-loss or production systemd service.

Primary sources checked 2026-10-14: [Gunicorn settings](https://gunicorn.org/reference/settings/),
[release metadata](https://pypi.org/project/gunicorn/26.2.0/). Pinned wheel source
inspected for BaseApplication/logging/config behavior. No normal dependencies.

Schema stays v15. Returning to PR16 needs disabled/stopped optional service and
existing code backup/restart procedure, no schema rollback; v14 requires pre-v15
backup with loss of later data. Remaining opt-in/number proof/revocation, reports,
pacing/night policy, worker/retention/reconciliation/operator recovery and Meta pilot.
Final rollout MUST contain ALL tokens/API/IDs/secrets (SMTP, Meta, Telegram and
optional integrations) and bot-preserving server install/update: permissions,
backup, one poller, coordinated restart, health/activation and rollback/data-loss.
