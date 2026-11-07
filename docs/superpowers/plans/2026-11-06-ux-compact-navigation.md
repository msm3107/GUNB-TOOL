# UX-01 execution — compact navigation

**Spec:** 2026-11-06-ux-qol-roadmap.md / UX-01; explicitly authorized implementation after reading the new roadmap.
**BASE:** e8e918df76b3c81d57a24df879b04043cf98f218 (human merge PR30 verified).
**Branch:** codex/ux-compact-navigation; existing checkout reused, merged previous branch removed.

- [x] Public header: exactly one Konto link to /panel, native closed details Menu containing existing marketing/configurator links.
- [x] Panel navigation: Paczki/Zamówienia/Rozliczenia/Zgłoś uwagę anchors; remains available on authenticated error view, without caching private snapshots.
- [x] Smaller work headings (auth/panel/configurator), secondary native POST logout; existing brand/assets and hero identity preserved.
- [x] Customer-facing Pełna baza copy. Scope clarification: index.html also changed because those labels live on the landing page, not in the five template/CSS files listed by the roadmap; no data-selection changes.
- [x] RED old navigation/copy tests observed, then GREEN three new contracts. Existing issue/auth/Portal regressions preserved.
- [x] Native Chromium: /, /logowanie, /panel, /zamowienie at320/390/768/1280, text100% and200%, no page-level overflow. Closed header76px at390 (limit120); text200% allows taller reflowing header. Keyboard Menu Enter/Tab, panel logout and issue inputs/select/submit, auth email->submit, visible solid focus.
- [x] Visual findings corrected: nowrap header squeezed brand vertically at200%; allow row wrap. Enlarged button content and long empty-result words needed wrapping. No clipping/overflow:hidden workaround.
- [ ] Independent whole-PR review and exactHEAD CI before human acceptance; no merge/deploy.

Browser QA uses ignored synthetic preview with explicit fake identity only; automated tests use real Magic Link sessions. Temporary inspector stylesheet doubled root text for reflow and was removed by reload, viewport/cache overrides reset, owned tab/server closed. No production data, real SMTP or Stripe; not a full WCAG/screen-reader/Safari audit. No new dependencies, JS, backend, schema, filtering or billing behavior.
