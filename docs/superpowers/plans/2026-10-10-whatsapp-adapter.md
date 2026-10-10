# Meta adapter — implementation plan

> REQUIRED SUB-SKILL: superpowers:executing-plans. Inline continuation of the
> authorized staged rollout; independent whole-PR reviewer per local AGENTS.

**Goal:** Add a template Sender and authenticated status parser without enabling WhatsApp.
**Spec:** `docs/superpowers/specs/2026-10-10-whatsapp-adapter-design.md`
**Base:** `a88cf7642a136de104de072e8c7a3dfe2fa3e6d7`
**Stack:** Python >=3.10, existing requests/SQLite, no new dependencies or migration.

## Global Constraints

- Preserve v14, existing Telegram/email paths, CLI/config gate and public store APIs.
- Secrets/addresses/body/provider errors never in repr/logs/exceptions; synthetic tests only.
- One POST, no retries or redirects; ambiguity unknown; no delivery claims from send ACK.
- Raw-body HMAC before JSON; bounded body/structure; parser performs no writes or commands.
- No production activation, HTTP listener, merge, real sends or external review publication.

### Task 1: Template sender and bounded response

**Files:** Create `gunb_tool/whatsapp_models.py`, `gunb_tool/whatsapp_sender.py`,
`tests/test_whatsapp_sender.py`.
**Interfaces:** Consumes existing NotificationEndpoint/ReportPart/DeliveryResult;
produces `WhatsAppSettings(api_version, phone_number_id, access_token, template_name,
language='pl', timeout_seconds=5)` and `WhatsAppSender(settings).send(endpoint,
report, *, idempotency_key)`. Common `strict_json(bytes, limit)`, ID validators.

- [x] Write tests with literal response/payload expectations. Parameterize invalid settings,
  wrong channel/owner/version/consent, overlong text, Unicode/control chars. Assert failed
  and no HTTP. Capture real sender's outgoing boundary through a fake requests transport.
- [x] Run `python -m pytest tests/test_whatsapp_sender.py -rs`.
  Expected RED because new adapter is absent.
- [x] Implement exact spec: fixed HTTPS, private session, TLS/no env/redirect/retry,
  two flattened BODY parameters <=900 combined characters, one POST, streamed <=16KiB,
  30s cooperative budget and validated single contact/message, generic result/errors.
  Tests cover HTTP200/4xx/429/408/409/5xx, malformed/duplicate/deep JSON, oversize,
  network and cleanup failure, no raw secrets in logs/repr. No hidden test-only API.
- [x] Run same test command. Expected all PASS; `git diff --check` clean.
- [x] Commit adapter + tests. Task verification: same pytest command.

### Task 2: Signed webhook boundary

**Files:** Create `gunb_tool/whatsapp_webhook.py`, `tests/test_whatsapp_webhook.py`;
extend `gunb_tool/whatsapp_models.py` with WhatsAppStatus.
**Interfaces:** Consumes Task1 strict_json/ID validators;
produces `WhatsAppWebhookSettings(waba_id, phone_number_id, app_secret, verify_token)`,
`WhatsAppWebhook(settings).challenge(mode, token, challenge) -> str`,
`.parse_statuses(raw_body: bytes, signature: str|None) -> tuple[WhatsAppStatus,...]`.
Status fields: provider_message_id, recipient_id, outcome, timestamp (UTC ISO), event_key.

- [x] Write tests: known HMAC vector using literal signed body; changed bytes/wrong key,
  missing/duplicate/non-ASCII header; authentication before JSON; separate GET token,
  own WABA/phone only; sent/delivered/read/failed literal outcomes; ignore inbound;
  timestamp/ID/shape limits, duplicate keys/NaN/UTF8/depth/64KiB/100 events, batch dedup,
  stable key across requests and privacy of error/repr. All no DB/network.
- [x] Run `python -m pytest tests/test_whatsapp_webhook.py -rs`.
  Expected RED because webhook module is absent.
- [x] Implement bounded settings, compare_digest on raw bytes before JSON, generic
  WebhookError, scoped parser, typed minimal statuses/hash event key per spec.
- [x] Run both new test modules. Expected all PASS; `git diff --check` clean.
- [x] Commit webhook boundary + tests. Task verification: both new test modules.

### Task 3: Worker evidence, docs and PR review

**Files:** Extend sender tests; update README.md and docs/WDROZENIE.md.
**Interfaces:** Consume Task1 Sender and existing real NotificationStore/Worker;
no production wiring. Final reviewer consumes BASE..HEAD and spec/plan/ledger.

- [x] Write/run integration tests with real temporary SQLite and fake HTTP:
  accepted persists correct provider ID/history, timeout unknown quarantines only WA,
  429 retry preserves immutable payload/key, no transaction during transport.
  Expected PASS using existing worker contracts (or fix a demonstrated adapter defect).
- [x] Document internal-only stage, exact template contract/limits, no schema change,
  accepted vs delivery, no provider idempotency, no live config, future activation gates.
- [x] Run `python -m pytest -rs`, `git diff --check`. Expected all PASS, only Windows
  POSIX permissions skip. Commit tests/docs, push feature branch, create and attach PR.
- [x] Dispatch fresh read-only independent reviewer (local CODE_REVIEWER_PROMPT.local.md)
  with exact BASE/HEAD/PR/spec/plan/tests; save ignored report; verify/fix findings with
  regressions and same reviewer followup for material changes. Check exact final HEAD CI.
- [x] Mark plan complete and hand off accepted PR without merging/deploying.

## Review Focus

- Misclassification of HTTP failures must not cause a second potentially accepted send.
- Mixed signed batches and malformed structures must not cross WABA/phone ownership.
- Provider body/secret must not escape through chained exceptions, cleanup or debugging.
- Requests default auth/proxy/retry/redirect must not alter the fixed trust boundary.
- Documented inactive status/template capacity must match actual CLI/config and behavior.

## Completion — 2026-10-10

PR [#11](https://github.com/msm3107/GUNB-TOOL/pull/11). Independent review of code
HEAD `bfed31ce60d7391bbe8d2284f22c8974f496ed7a`: AKCEPTUJ, no P0–P2 or requested
fixes. Full pytest: 1356 passed, 1 skipped (POSIX permissions on Windows),20.87s;
reviewer independently ran 134 tests and checked real requests transport defaults.
Code HEAD CI: 12/12 SUCCESS, Python3.10–3.14/deploy-scripts, push+PR.
This final commit records status only; its own CI is required before handoff.
WhatsApp remains inactive; activation gates in the spec/operator guide remain.
No merge, deploy or real messages. Review/CI evidence is stored locally, ignored.
