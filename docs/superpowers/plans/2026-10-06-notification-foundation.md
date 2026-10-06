# Notification Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this PR inline; one independent whole-branch review before opening the PR.

**Goal:** Prepare schema v13 and disabled e-mail/WhatsApp configuration without changing Telegram behavior.

**Architecture:** Append one additive SQLite migration. Preserve all existing delivery paths; no new senders, workers or credentials in this PR.

**Tech Stack:** Existing Python >=3.10, sqlite3, PyYAML and pytest; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-06-notification-foundation-design.md`

## Global Constraints

- Start from `646ad22` on an isolated `codex/notification-foundation` branch.
- Existing tables, parameter names, Telegram behavior and deployment layout stay intact.
- Migration v13 uses the existing backup and transactional migration mechanism.
- New channels default off; enabling an unavailable sender must fail explicitly.
- No production database, secrets, external messages, deployment or merge in this task.

## Review Focus

- Updating a populated v12 must retain every old row, including subscriptions and delivery state.
- Failure halfway through migration must leave v12 usable and a complete pre-migration backup.
- New channel state must be isolated and removable with the owner account.
- Existing config and CLI must continue to work with both flags absent or false.
- State export/import and restore must retain new data and preserve the rollback guard.

### Task 1: Build and verify the preparatory release

**Files:**
- Create: `gunb_tool/notification_schema.py`, `tests/test_notification_foundation.py`.
- Modify: `gunb_tool/storage.py`, `gunb_tool/config.py`, `config.yaml`.
- Adapt: `tests/test_sales_store.py` to expect the current schema and backup filename.
- Document: `docs/WDROZENIE.md`, `README.md`, the spec and this plan.

**Interfaces:**
- Consumes: `LeadRepository`, `BotStore`, `load_config`, `export_state`, `import_state`, `restore_backup`.
- Produces: `NOTIFICATION_SCHEMA: str`; appended migration v13; immutable `EmailConfig` and
  `WhatsAppConfig` with `enabled: bool = False`; `AppConfig.email` and `AppConfig.whatsapp`.
- Validation: both new sections use existing boolean parsing; true raises a safe `ConfigError`
  indicating that sending is unavailable in the preparatory release.

- [x] Write behavioral tests for migration preservation, backups, atomicity, independent
  endpoints/deliveries, constraints, deletion, state round-trip and config/CLI compatibility.
- [x] Run `python -m pytest tests/test_notification_foundation.py -q`; observe failures caused
  by missing new tables/configuration, not test setup errors.
- [x] Implement the additive schema and config gate, using existing migration/config patterns.
- [x] Run the foundation tests; expected: all pass.
- [x] Document the bridge-release update, rollback and scope of the next PR.
- [x] Run `python -m pytest -rs`; expected: zero failures; report any skip.
- [x] Run `git diff --check`, review the complete diff, commit, and request an independent review.
- [x] Address important review findings with reproducing tests, then run affected checks.
  Independent review: no Critical, Important or Minor findings; 86 additional tests passed.
  Future store/sender invariants remain prerequisites for later PRs, as documented in the spec.
- [x] Push the feature branch, create a PR against `main`, attach it and inspect CI.
  PR: https://github.com/msm3107/GUNB-TOOL/pull/7; Python 3.10–3.14 and deployment-script checks.
