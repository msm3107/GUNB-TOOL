# UX-06 — logowanie, powrót i rozliczenia Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans, test-first, one independent whole-branch reviewer at the end.

**Goal:** klient wraca do własnej paczki po logowaniu i obsługuje nowe testowe zamówienie/rozliczenia bez przepisywania UUID.
**Architecture:** allowlista lokalnych celów i związanie celu z hashem jednorazowego tokenu; istniejący checkout_attempts przechowuje serwerowy znacznik uwierzytelnionego właściciela w niezmiennym offer_json. Natywne formularze tworzą/utrzymują ukryty UUID próby i używają istniejącej idempotencji oraz weryfikacji płatności.
**Tech Stack:** Python/FastAPI/SQLite WAL/Jinja2, stdlib; istniejący Stripe SDK wyłącznie z atrapami w testach.
**Spec:** UX-06 w docs/superpowers/plans/2026-11-06-ux-qol-roadmap.md, projectplan.md.
**BASE:** 29e2f3885c924c02dcfafe2de44d2b2c7f61d2f8; branch codex/ux-account-recovery; PR36 merged by human.

## Global Constraints
- Brak merge/deploy, sprzedaży/live, nowych zależności i migracji; nie zmieniać selekcji/pamięci dostaw/publikacji.
- Core bez HTTP; sieć poza blokadami DB. Dane testowe/synthetic i loopback, brak .env/prawdziwego Stripe/SMTP.
- Powrót wyłącznie /panel i /panel/paczki/{id} z ograniczonymi, znanymi parametrami; każdy endpoint ponownie sprawdza właściciela.
- GET nie zużywa tokenu; TTL15min, browser binding, same-origin i antyenumeracja pozostają.
- Uwierzytelnione konto z sesji, nie z emaila/ID przesłanego przez klienta; stary anonimowy JSON Checkout i legacy UUID recovery nie stają się automatycznie zaufane.
- Nieznany wynik Checkout zachowuje dokładnie ten sam request_token, filtry i wariant; nie tworzyć drugiego zamówienia/paczki na retry.

## Review Focus
- Próba podmiany celu po wystawieniu maila, kodowanie/duplikaty query, link w innej przeglądarce i dwie równoległe wiadomości.
- Konto/email/oferta zmienione pomiędzy odczytem a rezerwacją/uzgodnieniem; cofnięta płatność podczas IO.
- Stara anonimowa próba o tym samym emailu nie uzyskuje zaufania nowej ścieżki.
- Błąd po utworzeniu Session i przed zapisem URL, podwójny submit, ponowienie po wygaśnięciu sesji WWW.
- Native checkout przejmuje aktualne filtry także przy działającym estymatorze JS; brak JS nie blokuje ścieżki.

### Task 1: Bezpieczny powrót po logowaniu
**Files:** app/navigation.py (new), app/auth.py, app/routes/auth.py, app/routes/packages.py, app/routes/dashboard.py, auth/package templates, tests/test_auth_navigation.py (new).
**Interfaces:** safe_return_path(str)->str; login_url(str)->str; AuthService.prepare(...,return_to='/panel'), verify(...,return_to='/panel') wiążą cel z token_hash, zachowując stare domyślne tokeny.
- [ ] RED: return do paczki/page/category/case, odrzucenie zewnętrznych/zakodowanych/duplikowanych celów, tamper nie zużywa linku, scannerGET, TTL/browser/retry, domyślny legacy flow.
- [ ] Implementacja minimalna, target auth suite GREEN, commit.
Expected: tests/test_auth_navigation.py i istniejące auth_routes/service/store PASS.

### Task 2: Natywne uwierzytelnione zamówienie i retry
**Files:** app/routes/checkout.py, app/stripe_service.py, core/checkout_store.py, app/routes/orders.py, app/main.py, checkout_summary.html (new), order_configurator.html, tests/test_owned_checkout.py (new).
**Interfaces:** CheckoutService.start_owned(customer_id,kind,filters,request_token)->str; CheckoutStore.reserve(...,owner_id=None); POST /zamowienie/podsumowanie z aktualnymi polami konfiguratora -> formularz z ukrytym UUID; POST /zamowienie/checkout obsługuje natywny formularz właściciela i zachowuje go przy nieznanym wyniku.
- [ ] RED: auth/id/email tampering, native filtry, token zachowany po timeout/utracie sesji, retry/double-submit ten sam order/provider key, brak DB lock w sieci, disabled bez działań.
- [ ] Implementacja minimalna, istniejące checkout/service tests GREEN, commit.
Expected: tests/test_owned_checkout.py i tests/test_checkout_routes.py/test_stripe_service.py PASS.

### Task 3: Rozliczenia z konta, persony i review
**Files:** core/auth_store.py, app/auth.py, app/routes/dashboard.py, core/customer_dashboard.py, dashboard.html; tests/test_owned_billing.py i browser/test_account_recovery.py (new), README/projectplan.
**Interfaces:** AuthService.bind_order(principal,order_id,subscriptions); claim_billing(...,require_owned=False) ponownie sprawdza serwerowy marker w transakcji. Dashboard pokazuje przycisk uzgodnienia własnego nowego zamówienia; legacy proof pozostaje oddzielny.
- [ ] RED: paid owned order bez UUID -> zweryfikowany billing, legacy/obcy/pending odrzucone, changed proof po IO, retry bez nowych płatności/paczek; browser JSon/off i powrót do paczki.
- [ ] Implementacja i pełny unit/browser/Ruff/ASCII/diff-check; heurystyka trzech person; independent wholePR review, poprawki/regresje i CI dokładnego HEAD; PR z audit.
Expected: wskazane w spec auth/billing/legacy/checkout/service plus cały unit/browser PASS; bez merge/deploy.
