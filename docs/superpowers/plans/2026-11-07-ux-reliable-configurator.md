# UX-02 — reliable configurator execution

**Spec:** 2026-11-06-ux-qol-roadmap.md UX-02, explicitly authorized after human merge of UX-01/PR31.
**BASE:** 98be3a270f33b998b1f5bec084235e98e6375fba; branch codex/ux-reliable-configurator.

## Contracts and decisions
- County checkboxes show names before TERYT, optional search/count and explicit clear/whole-region actions; no JS leaves native checkboxes usable. Region change retains foreign selected counties and requires correction, never silent removal/expansion.
- API success four fields unchanged; error detail preserved, optional field_errors. Core PackageFilterError only adds field identity to existing ValueError messages/conditions, no selection/window/SQL change.
- Bounded raw native selection is HTML-only, not an input to estimation. Invalid scalar/list values are escaped and retained, unknown choices can be corrected explicitly. Unreadable input shows blank required scope, not valid region defaults. Coordinate errors open advanced fields and preserve invalid text for correction.
- Version1 draft contains exactly seven allowed filter fields; optional sessionStorage failures never block estimates. Native POST takes precedence over old draft; missing county metadata restores selected codes, unknown version/choice prevents automatic calculation. No account IDs/proofs/emails in draft.
- Logout uses a pending local marker and clears filters only after server-confirmed anonymous login GET; failed logout retains work. Shared script is loaded only on auth/dashboard/configurator, public landing remains script-free.
- Monotonic request sequence and per-request AbortController/10s timer discard late responses even when abort is ignored. On failure/timeout hide result, retain filters and offer manual retry. Zero shows current scope including types/point/radius and no import-completeness promise.
- Browser tooling explicit in roadmap: isolated .venv-browser (local exclude), pinned requirements-browser files, existing production files unchanged; Chromium/Firefox pytest job blocks acceptance. Unit matrix ignores tests/browser. No browser runtime dependencies in production.

## Verification checklist
- [x] RED API field_errors missing and native bad-number scope loss before implementation; GREEN regression.
- [x] API/selection/native metadata-lock tests and malformed input/XSS retention; validation mapping only.
- [x] Chromium+Firefox: late success/error, timeout/late arrival, exact known/missing-metadata draft restoration, denied storage, invalid draft, county search/region change, server field errors, native JS-off parity, real Magic Link + failed/successful logout.
- [x] Browser negative control removing draft load must fail restoration regression; source restored byte-for-byte.
- [x] Review regression: failed logout followed by GET `/auth/verify` must retain the draft while the session remains valid; only successful session resolution on `/logowanie` can confirm anonymous state. Native mobile status reflects server-rendered results with JS disabled. Both regressions failed before the fixes.
- [x] Review regression: collect all invalid/nonfinite native coordinate numbers before further validation, so browsers retain every raw value instead of sanitizing later number inputs to empty; JS-off browser regressions fail before the fix and verify correction/retry with the selected county, including a foreign county plus nan/inf/-inf.
- [x] Raw native coordinate strings render as text independently of validation order; a duplicate-region early error plus invalid coordinates retains all raw fields and supports correction/retry. The browser regression failed before this final rendering guard.
- [x] UX02-DRAFT-03: a structurally valid draft with a county/region conflict restores all seven fields. The existing estimate guard retains the conflict and blocks requests through reload, trade edits and manual retry until explicit correction. Chromium/Firefox regression failed on the previous HEAD before this fix; no business validation was relaxed.
- [x] Browser fixture synchronizes on the completed synthetic auth background task, not an early fake-sender message append; a held real OS receipt lock independently reproduces 503 during immediate verification. No test retry or production auth change.
- [x] Local unit gate: 841 passed, 16 skipped; browser gates across Chromium/Firefox include keyboard/zero-scope behavior and native correction/retry. Chromium layout probes: no horizontal overflow at 320/390/768/1280 px; visual inspection at 390/1280 px. Exact counts for final HEAD are recorded in the PR.
- [ ] Independent whole-PR review, exactHEAD CI, human acceptance; never merge/deploy.

Out of scope: UX-03 place catalog, new filtering, real checkout/sales, download/publication/outbox. Browser tests use synthetic DB/loopback and fake SMTP only; Safari/screen-reader/fullWCAG and deployed monitoring remain separate checks.
