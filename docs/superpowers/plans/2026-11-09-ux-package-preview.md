# UX-05 — podgląd własnej paczki i kontekstowe zgłoszenia

> Wykonanie: superpowers:executing-plans, test-first, niezależny whole-PR reviewer na końcu.

**Goal:** właściciel czyta zapisany stan swojej paczki i zgłasza uwagę przy konkretnej inwestycji bez przepisywania ID i utraty opisu po błędzie.
**Spec:** UX-05 w docs/superpowers/plans/2026-11-06-ux-qol-roadmap.md oraz projectplan.md. BASE f02844983256ef67f460804b4d33b2e249d14646 po ludzkim merge PR35; branch codex/ux-package-preview.
**Architecture:** core/package_preview.py czyta autoryzowany CSV przez resolver etapu13, bez dołączania aktualnego investments. Ledger delivery_items służy wyłącznie walidacji tożsamości/kategorii oraz liczby pozycji. app/routes/packages.py obsługuje sesję, natywne GET/POST i bezpieczny retry; wspólny issue_form zachowuje zgodność dashboardu.

## Global Constraints
- Brak merge/deploy, nowych zależności/migracji i zmian selekcji/pamięci dostaw; core niezależne od HTTP.
- Dostęp jak etap13: właściciel/paid/historical delivered lub publication; gotowość pliku nie wystarcza. Odczyt pliku poza DB transaction/replacement lock.
- 50 pozycji/stronę, maks.10000 wierszy i200stron, limit pola128KiB znaków, plik64MiB,4aktywne odczyty/proces. Worker sam zamyka uchwyt/zwalnia slot, także gdy caller porzuci await.
- CSV UTF8/BOM, dokładny produkcyjny nagłówek/liczba kolumn, poprawna kategoria, unikatowe ID i zgodność z ledgerem; błąd503, nigdy odtworzenie z aktualnego investments ani fałszywe zero.
- CSV zabezpiecza formuły apostrofem, więc tożsamość odtwarzana przez encoder istniejącego eksportera i niezmienny ledger; kolizja dwóch zakodowanych ID daje503 zamiast zgadywania.
- Wyświetlane wartości pochodzą wyłącznie z CSV, z escapowaniem; istniejące mapy tylkoHTTPS na ścisłych host/path/query zgodnych z obecnym generatorem, GoogleMaps także z poprawnym zapisanym punktem.
- Podgląd skraca pola do2000znaków i oznacza skrócone pozycje; oryginalne pliki pozostają pełne. Ogranicza to pamięć strony/DOM dla źródłowych pól do128KiB.
- Zgłoszenie: membership w autoryzowanym CSV oraz ponowne owner/paid/delivery/item sprawdzenie w krótkim zapisie; brak plikowegoIO w transakcji. Case_id może być readonly, ale serwer zawsze ponownie je sprawdza.
- Obsługa istniejących publikacji w IssueTracker jest rozszerzeniem odczytu uprawnień, nie publikowaniem; historyczne reguły manualnych doręczeń zachowane. Etap15 nadal tworzy publikacje/dyspozytor.
- 503/429 zachowuje bezpiecznie escapowane pola i page/category/case bez wymogu udanego ponownego snapshotu; jeśli sesji nie można potwierdzić, echo dotyczy wyłącznie przesłanych pól, bez danych serwera.
- Historia własnych zgłoszeń przy wybranej sprawie: maks.4rodzaje, data/treść/przyjęto do weryfikacji, bez obietnicy rozstrzygnięcia. Idempotencja pozostaje istniejąca.

## Review Focus
- Nowy import/zmiana statusu/quality nie zmienia starego podglądu i map.
- Podmieniony lub uszkodzony CSV nie zwraca częściowej listy; niezgodność ID/liczby/kategorii odrzucona.
- Obca paczka, case z innej własnej paczki i manipulacja hiddenfields nie autoryzują zgłoszenia; zmiana uprawnień między preview a zapisem odrzucona.
- Błąd metadanych po auth i nieudany ponowny odczyt nie usuwa opisu/formularza; CSRF i niepotwierdzona sesja nie ujawniają danych serwera.
- Długie pola, złośliwe href/HTML i anulowanie odczytu nie powodują wycieku/nieograniczonego RAM/slotów.

## Task 1: Niezmienny parser i projekcja
Files: core/package_preview.py, core/package_access.py, tests/test_package_preview.py.
Interfaces: PackagePreview(store,root).read(customer_id,delivery_id,*,page=1,category='all',case_id=None) -> PreviewPage; metadane CSV+ledger w jednym bounded read; wyjście max50rows+selected+counts.
- [ ] RED: actual productionCSV/XLSX parity, frozen data after new import, pages/category, limits, corrupted schema/encoding/rows/IDs/manifest, mapURLescaping i noDBlockfileIO.
- [ ] Minimal implementation, GREEN target i commit.

## Task 2: HTTP i zgłoszenie z kontekstem
Files: app/routes/packages.py, app/main.py, app/routes/dashboard.py, core/customer_dashboard.py, core/issue_tracker.py, wspólny issue_form, tests/test_package_preview.py/test_issue_tracker.py/test_customer_dashboard.py.
Interfaces: GET /panel/paczki/{id}?page&category&case_id, POST /panel/paczki/{id}/uwagi z5polami; report_issue(...,delivery_id=None) weryfikuje konkretną dostawę przy contextualPOST; history(customer,case) po własności/CSV.
- [ ] RED: real owner/session/CSRF/foreign/prepared, membership, 503/429→retry exactlyone, opisescaped/context preserved, own history i existinggeneric dashboard regressions.
- [ ] Implementacja, GREEN, commit.

## Task 3: Native UI i review
Files: app/templates/package_detail.html, dashboard.html, style.css, README/projectplan, tests/browser/test_package_preview.py.
- [ ] RED: link z paczki, native paging/filter/selectcase/report/history zJSon/off, odzyskanie opisu po503, brak szerszego/obcego kontekstu.
- [ ] Reuse branding/cards, layout320/390/768/1280+keyboard/font200%, fullunit/browser/Ruff/ASCII/diffcheck, personyheurystyczne i niezależny wholePRreview; fix i exactHEADCI, PR36 z audit. Bez merge/deploy.
