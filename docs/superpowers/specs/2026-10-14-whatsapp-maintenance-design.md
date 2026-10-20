# Managed WhatsApp webhook maintenance barrier

## Scope and decision

PR18 closes a concrete server lifecycle gap: the managed webhook may retain SQLite
handles while the installer switches releases or an operator replaces a database.
Preserve the working bot updater and its backup/restart policy. Before maintenance,
stop and disable the optional `gunb-whatsapp-webhook.service`, verify it is inactive,
then proceed. Never restart or enable the webhook automatically in this release.

This deliberate pause also covers rollback to code without Gunicorn or a webhook
module. Installing dependencies and automatically resuming after schema/health
checks would be a larger activation feature; a warning-only check would not protect
the database. The chosen barrier is conservative and independently testable.

## Contract

- Both standalone shell entry points contain the same small `quiesce_webhook`
  function. Duplication keeps curl-piped install and an updater fetched from another
  version usable without sourcing an unavailable helper.
- Read only LoadState and ActiveState with `systemctl show --value`. An absent unit
  (`not-found`, `inactive`) is a no-op. A manager error, empty/unknown load state,
  disable/stop failure, or final state other than inactive aborts maintenance.
- For loaded/masked units disable first, stop synchronously, then verify inactive.
  No raw systemctl diagnostics or secret environment files are printed.
- Installer: barrier before stopping the bot, backup, current/previous switch and
  pruning; rollback after compatibility checks but before stopping/switching code.
  On backup failure existing bot recovery remains; webhook stays disabled/stopped.
- Admin: `stop` quiesces webhook before stopping bot. Update quiesces before executing
  the target installer (including older versions); rollback before invoking installer.
  Restore, import and final export require stopped bot and quiesce before incoming
  copies/database operations. `eksport --na-zywo`, online backup and read-only checks
  leave the webhook alone. Start/restart still start only the bot.
- No copying/enabling service templates, dependencies, credentials, database schema,
  Python runtime, notification channels or actual server operations change here.

## Boundaries and recovery

Managed systemd service only. One operator/maintenance command at a time; no socket,
timer, dependency or external supervisor may reactivate the webhook, and no manual
webhook/database writer may run. Disable removes boot enablement, it is not a lock
against an independent privileged start. Direct Python migration commands and older
standalone installers are not protected by this shell barrier. Use the current
gunb-admin entry point and stop all writers for direct maintenance.

Any partial failure can leave webhook disabled; keep HTTPS unavailable and inspect
service state before retry. Bot behavior follows existing updater semantics, including
explicit restore for incompatible schemas and data loss since a backup. No promise
of automatic webhook recovery or zero downtime. Do not activate templates yet:
public proxy, retention/health, checked dependency installation, coordinated restart,
real Linux/systemd validation and opt-in/sending/pilot gates remain.

## Verification and rollout

Synthetic Bash subprocess tests exercise real script functions with fake systemctl,
temporary release links/files and fake Python/network boundaries. Cover absent,
loaded inactive/active/activating/deactivating/failed/masked states, manager errors,
disable/stop/final-state failures, ordering before mutation, backup failure, old-target
admin update, non-live/live export, restore/import and bot-only restart. CI runs Bash
syntax and shellcheck as well as Python3.10–3.14. No test touches host services.

Final rollout PR MUST include ALL tokens/API/IDs/secrets (SMTP, Meta WABA/phone/template/
access token/app secret/verify token, Telegram and optional Discord/Sheets), permissions,
backup, trusted HTTPS/proxy/DNS, disabled installation, one Telegram poller, coordinated
controlled restart, health/pilot activation and rollback/data-loss limits.
