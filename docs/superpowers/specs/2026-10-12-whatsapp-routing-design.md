# Durable WhatsApp routing proof

Date: 2026-10-12. Continue the authorized staged rollout after merged PR #12,
base `9b6e0eff0e5eed9785e039a66db655810728c677`.

## Intent and scope

Preserve the working Telegram/email architecture. Close the explicit inbox gate:
prove which recipient and Meta sender a particular outbox part targeted before
HTTP, rather than infer it from a mutable endpoint. Keep WhatsApp disabled. This
PR does not add a server, API secrets, runtime service, commands or real sending.

Append migration v15 creating `whatsapp_dispatch_routes`, keyed by outbox_id with
ON DELETE CASCADE. Fields: sender_scope hash, recipient_digest hash, original
chat_id, endpoint_version and created_at. No clear phone, WABA/phone IDs, tokens,
provider body or report content. Constraints validate lowercase 64-hex digests,
nonzero integer owner and positive version. Reuse the repository's migration
backup/atomic versioning. Do not edit released v13/v14 DDL or backfill invented
routing for old outbox rows. Retention/account deletion inherit outbox cascade.

`WhatsAppRouting(waba_id, phone_number_id)` is frozen; IDs are validated and hidden
from repr. Scope is SHA256 of compact JSON `['gunb-whatsapp-route-v1',waba,phone]`.
Recipient digest is SHA256 of compact JSON
`['gunb-whatsapp-recipient-v1',scope,canonical_recipient_id]` (ASCII, no plus).
These are pseudonyms, not anonymization or encryption. Domain separation prevents
accidental reuse of the legacy event digest's meaning.

## Dispatch boundary

WhatsAppSettings gains required waba_id (local routing context, no new Graph API
request field). WhatsAppSender exposes its validated routing value. Workers with
a WhatsApp sender require a valid routing value; email-only workers are unchanged.
The worker passes that route to `prepare_send(claim, *, whatsapp_routing=None)`.

Inside the existing last preflight transaction, after owner/version/access/
consent/report checks, a WhatsApp job requires routing proof. Insert it once, or
verify all saved values exactly match the same original snapshot and route.
Never overwrite a previous attempt's route during retry/config rotation. Missing
or conflicting proof prevents HTTP; invalid jobs fail and quarantine the endpoint.
The prepared immutable endpoint is still the one sent. No transaction spans HTTP.
Route COMMIT errors propagate before send; roll back the store-owned transaction
if COMMIT leaves it open, without affecting caller-owned transactions.

The route records intended dispatch, not proof of provider acceptance. Provider ID
still comes only from a validated API ACK through complete(); timeout without ID
stays unknown and cannot be guessed from phone/hash. No automatic resends added.

## Authenticated status and reconciliation

New receives persist event payload v2: version/outcome/timestamp/recipient_digest/
sender_scope, using the authenticated parser's configured WABA/phone. Event key
and first receipt/dedup/capacity behavior remain unchanged. No raw body/phone.

Decode legacy v1 records as legacy, not malformed: they have no independent scope
proof and stay pending until retention/operator reconciliation. Do not silently
upgrade them using current settings. New v2 statuses require a saved dispatch
route: a unique provider ID across the whole outbox, WhatsApp channel, original
report owner/version matching route and current endpoint owner, and exact event
scope+recipient digests matching the saved route. Routeless old jobs stay pending.

Read the original route instead of the current phone. A status for a previous
number updates its own outbox and original history even after number/consent
changes. It never authorizes sending or changes the new address/proofs/consent.
Failure may quarantine/cancel siblings only when the endpoint still has the exact
dispatch version; after reconfiguration it marks only the old job failed. This
prevents a previous recipient's failure disabling a newly verified endpoint.

Preserve monotonic accepted<delivered<read, all collision/owner checks, no fabricated
history, bounded fair batches, isolated malformed records and atomic bookkeeping.
Old queued/accepted data survives migration unchanged, but lacks proof for
automatic WhatsApp status reconciliation. There was no enabled production channel
in previous releases; document this conservative behavior rather than fabricate
historical attribution.

## Validation and rollback

Real SQLite tests: populated v14 migration with exact data preservation and one
restorable backup, broken DDL/backup refusal, old-code refusal and restore, FK and
export/import; preflight proof before HTTP/COMMIT failure, immutable retry/config
change, missing route/no network, independent email; v2 privacy/scoping, old-number
success/failure isolation, early webhook then ACK, legacy/routeless/ambiguous IDs,
account/outbox retention cascade. Run full suite and exact-final-head CI; mandatory
independent whole-PR review and same-reviewer followup for material corrections.

v14 code refuses v15: rollback requires the pre-v15 backup, with loss of changes
since that backup. Never instruct copying old code over the live v15 database.
Migration adds a small empty table; backup duration depends on database size.
Follow existing controlled server update/backup/health procedure and preserve
Telegram polling/state. No promise of zero interruption or live migration testing.

## Remaining rollout and final handoff requirement

Next: bounded HTTPS/body/ACK ingress and supervised separate connections, verified
number/opt-in/revocation, compact reports, pacing/night policy and operational
unknown/failed recovery. Current Meta API/template/policy checks and a real pilot
remain required before activation. No new external platform claims in this PR.

User requirement of 2026-10-12: **the final rollout PR must include a complete
operator guide** covering every needed token/ID/secret and where to obtain/set it,
SMTP TLS, Meta app/token/WABA/phone/template and separate webhook secrets, secret
permissions and backups, HTTPS/DNS/reverse proxy, staged disabled-channel server
deployment, migration backup, health checks, test/pilot activation and rollback.
The procedure must protect existing bot operation, call out necessary controlled
restart and recovery, and never publish real credentials. This is a release gate,
not authorization to deploy now; no credentials are needed for this internal PR.
