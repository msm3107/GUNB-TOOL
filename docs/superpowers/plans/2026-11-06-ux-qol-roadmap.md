# UX/QOL i niezawodna dostawa B2B — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Nie uruchamiaj implementacji na podstawie samego zapisu tego planu.

**Goal:** Klient samodzielnie ustawia obszar, rozumie wynik, ogląda i pobiera własną paczkę oraz odzyskuje pracę po błędzie.

**Architecture:** Rozbudowa istniejącego FastAPI/Jinja2 i małych modułów JavaScript. `/core` pozostaje niezależną biblioteką; autoryzacja i HTTP pozostają w `/app`. Wykorzystujemy generator XLSX/CSV, pamięć dostaw i trwałe zadania zamiast budować drugi mechanizm dostaw.

**Tech Stack:** Istniejące Python, SQLite/WAL, FastAPI, Jinja2, standardowy JavaScript, openpyxl, pytest; Playwright wyłącznie jako zależność testowa od UX-02, pełna ścieżka E2E w etapie 18.

**Spec:** `projectplan.md`, trzy persony i audyt UX w tej rozmowie z 2026-11-06; proponowane doprecyzowania kontraktów poniżej.

**Baseline:** Lokalny HEAD `86975676b7bec56b152a65fced15166345fa3acd`, gałąź `codex/app-customer-dashboard`. To nie jest potwierdzenie stanu `origin/main` ani wdrożenia. Przed implementacją sprawdź aktualny main i otwarte PR-y; nie duplikuj ukończonych zmian.

## Zakres i podział

Sześć nowych PR-ów `UX-01`–`UX-06` usuwa tarcie z audytu. Istniejące etapy 13–18 kończą funkcjonalność pobierania, dostaw i obsługi operacyjnej. Te oznaczenia są identyfikatorami planu, nie przydzielonymi numerami PR na GitHub. Każdy PR obejmuje własne testy; etap 18 scala pełną ścieżkę E2E.

Podejście: zachować obecny stos i wprowadzać małe zmiany. Osobny frontend SPA zwiększyłby zakres bez rozwiązania brakującej dostawy. Pełna mapa inwestycji, CRM, konta zespołowe i ranking AI pozostają poza tą serią.

## Global Constraints

- Gałęzie `codex/ux-*` lub `codex/app-*`; aktualny zgodny main jako baza, zależne PR-y po ludzkim merge poprzednika. Nigdy automatyczny merge lub wdrożenie.
- Nie rozszerzaj wybranego obszaru przy zmianie województwa, błędzie metadanych, przywróceniu szkicu ani retry. Nie zmieniaj klasyfikacji i okien branżowych jako skutku pracy nad UI.
- Brak danych, niepełne metadane i awaria nie są zerem wyników. `last_seen` nie dowodzi kompletności importu.
- Sieć, SMTP, generowanie i odczyt zawartości dużych plików poza transakcją zapisu oraz blokadą podmiany SQLite. Lease zadania nie jest blokadą zapisu bazy.
- WWW nie tworzy ani nie migruje głównej bazy. Każda migracja: nowa wersja względem aktualnego main, kopia przed DDL, zachowana historia, test aktualizacji i opis odtworzenia.
- Własność paczki wynika z uwierzytelnionej sesji i powiązania zamówienia; własność rozliczeń Stripe z zaufanej korelacji. Sam email nie jest dowodem własności Stripe.
- Dane klientów, tokeny, UUID Checkout i ścieżki prywatne nie trafiają do publicznych przykładów, logów ani analityki. Lokalne instrukcje review pozostają ignorowane.
- Zachowaj natywne formularze bez JS. Nowe funkcje JS muszą zachować podstawową ścieżkę po awarii skryptu lub pamięci przeglądarki.
- Zachowaj ASCII w `requirements.txt` i `requirements.lock`. Nie dodawaj zależności produkcyjnej do funkcji dostępnej w obecnym stosie.
- Sprzedaż pozostaje wyłączona w tej serii. Ceny, VAT, warunki, częstotliwość, polityka retencji i uprawnienie do uruchomienia live wymagają osobnej decyzji biznesowej i wydaniowej.

## Doprecyzowania wymagane w specyfikacjach PR

1. **Historyczna paczka:** podgląd i pobranie korzystają z tego samego niezmiennego artefaktu; nie odtwarzaj archiwum z aktualnej tabeli `investments`.
2. **Doręczenie:** rozróżnij przygotowanie, trwałe udostępnienie w panelu, przyjęcie emaila przez SMTP i żądanie pobrania. Żadne z ostatnich dwóch nie dowodzi odbioru pliku przez człowieka.
3. **Pamięć:** proponowana reguła nowych dostaw to zapisana rewizja trwale udostępniona właścicielowi w panelu. Stare `doreczono_utc` nadal jest dowodem dotychczasowej dostawy. Osobny ledger publikacji rozszerza podstawę deduplikacji; nie przepisuj historycznych dat.
4. **Przygotowane, nieopublikowane pliki:** nie zużywają pamięci. Wszystkie nowe generacje, także pierwsza paczka, przechodzą przez ten sam lease klienta i kontrakt publikacji z etapu 15.
5. **Dostęp:** opłacona i już udostępniona paczka pozostaje własnością klienta po zakończeniu abonamentu; generowanie kolejnych wymaga aktualnych uprawnień. Historia dostaw pozostaje po zakończeniu retencji plików.

Punkty 2–5 doprecyzowują zachowanie przyszłych etapów 13–15 i muszą zostać wpisane do `projectplan.md` w odpowiednich PR-ach, razem z testami zgodności.

## Review Focus

- Zmiana regionu podczas opóźnionego requestu: poprzednia odpowiedź nie nadpisuje nowego zakresu; odtwarzanie szkicu nie rozszerza filtrów — UX-02.
- Dwie miejscowości o tej samej nazwie lub brak katalogu: wymagaj jednoznacznego wyboru; brak cichego punktu zastępczego — UX-03.
- Obca sesja, podmieniony plik lub przerwany stream: brak wycieku, zmiany pamięci i fałszywego potwierdzenia odbioru — etap 13 i UX-05.
- Restart, dwa procesy i SMTP timeout po przyjęciu: jeden opublikowany zestaw na cykl; wynik emaila może pozostać nieznany — etapy 14–15.
- Nowy import po publikacji: podgląd starej paczki nadal pokazuje jej zapisany stan; nowe rewizje trafiają do kolejnej delty — UX-05 i etap 18.

## Cykl wykonania każdego PR

- [ ] Odczytaj aktualną bazę, wymagania i lokalne instrukcje; ustal BASE_SHA/HEAD_SHA oraz pliki w zakresie.
- [ ] Dla zmiany zachowania dodaj wskazane niżej regresje i sprawdź ich niepowodzenie przed implementacją; dla CSS/copy wykonaj kontrolę wizualną i klawiaturą.
- [ ] Zaimplementuj minimalny zakres, uruchom wskazane testy i sprawdź diff pod kątem bezpieczeństwa, dostępności i regresji trzech person.
- [ ] Otwórz PR z blokiem `<audit>`: Utility, Edge, Failure modes, Persona friction, Breaking changes/migracje.
- [ ] Zleć niezależnemu reviewerowi cały diff według lokalnych instrukcji; zapisz wynik w ignorowanym `.local-review/PR-<numer>-<HEAD8>.md`.
- [ ] Napraw P0/P1; P2 napraw albo jawnie uzasadnij odroczenie. Po istotnej poprawce ten sam reviewer weryfikuje nowe SHA.
- [ ] Potwierdź CI dla końcowego HEAD i przekaż do ludzkiego merge. Sprawdź wdrożenie oddzielnie od lokalnych testów i CI.

## UX-01 — Krótsza droga do działania

**Branch:** `codex/ux-compact-navigation` · **Rozmiar:** mały · **Zależności:** brak.

**Files:** modyfikuj `app/templates/base.html`, `app/templates/components/navbar.html`, `app/templates/dashboard.html`, `app/templates/auth.html`, `app/static/style.css`; testy `tests/test_landing_page.py`, `tests/test_customer_dashboard.py`, `tests/test_auth_routes.py`.

**Deliverable:** na publicznej stronie jedno wejście „Konto”; w panelu nawigacja do paczek, zamówień i rozliczeń. Mniejsze nagłówki ekranów pracy, wylogowanie jako akcja drugorzędna, „Pełna baza” zamiast „snapshot” w etykietach klientów. Rozwijane menu to natywne `details`, dostępne bez JS.

**DoD:** przy 390×844 nagłówek w stanie zwiniętym zajmuje najwyżej 120 px; brak poziomego przewijania przy 320/390/768/1280 px; menu i wszystkie formularze osiągalne klawiaturą, focus widoczny. Zweryfikuj powiększenie tekstu 200% i czytelność podziału Nowe/Zmiany/Pełna baza.

- [ ] Wykonaj zmianę i wizualną kontrolę czterech szerokości; uruchom `.venv\Scripts\python.exe -m pytest tests/test_landing_page.py tests/test_customer_dashboard.py tests/test_auth_routes.py`.

## UX-02 — Konfigurator odporny na błędy i utratę pracy

**Branch:** `codex/ux-reliable-configurator` · **Rozmiar:** średni · **Zależności:** UX-01.

**Files:** `app/routes/orders.py`, `app/templates/order_configurator.html`, `app/static/order-configurator.js`, `core/package_selection.py` wyłącznie mapowanie błędów bez zmiany selekcji; nowy `app/static/filter-draft.js`; `tests/test_order_configurator.py`. Dodaj `tests/browser/conftest.py`, `tests/browser/test_order_configurator.py`, oddzielne developerskie `requirements-browser.txt`/`requirements-browser.lock` i job browser w `.github/workflows/tests.yml`.

**Contract:** sukces `POST /api/estimate` zachowuje dotychczasowe cztery pola. Błędy zachowują `detail`, opcjonalnie dodają `field_errors: {nazwa_pola: komunikat}`. Powiaty: wyszukiwalne checkboxy, licznik wyboru, „Całe województwo” i jawne czyszczenie wyboru; nazwa przed kodem TERYT.

**Deliverable:** błędy przy polach i podsumowanie z linkami do nich; wersjonowany szkic dozwolonych filtrów w `sessionStorage`, czyszczony po wylogowaniu; ponowne obliczenie po przywróceniu. Timeout fetch 10 s, anulowanie starych requestów, ręczne retry. Na telefonie zwięzły status wyniku przy formularzu. Zero pokazuje aktywny zakres i działania „zmień branżę/rozszerz obszar” wymagające kliknięcia; nie obiecuje kompletności danych.

**DoD/tests:** `test_estimate_field_errors_keep_detail`, `test_native_estimate_preserves_scope`, `test_late_result_is_ignored`, `test_draft_restore_keeps_exact_scope`, `test_storage_denied_still_estimates`, `test_timeout_keeps_filters_and_hides_stale_result`. Brak automatycznego retry zapisów biznesowych. Uruchom testy API/selekcji i `python -m pytest tests/browser --browser chromium --browser firefox` w środowisku browser. Zwykła macierz Pythona uruchamia `pytest --ignore=tests/browser`; brak Playwright w instalacji produkcyjnej. Job browser blokuje przyjęcie PR ze zmianami JS.

- [ ] Dodaj regresje requestów/szkicu, wdroż poprawę formularza i sprawdź tę samą selekcję z JS oraz bez JS.

## UX-03 — Miejscowość i promień bez znajomości GPS

**Branch:** `codex/ux-place-radius` · **Rozmiar:** średni · **Zależności:** UX-02.

**Files:** nowe `core/places.py`, `app/routes/places.py`, `app/static/place-picker.js`, `tests/test_places.py`; zmiany w `app/config.py`, `app/main.py`, `app/routes/orders.py`, szablonie konfiguratora i `tests/test_order_configurator.py`.

**Contract:** `Place` ma tekstowe `id/name/municipality/county/region/source_version` oraz `lat/lon: float`. `PlaceCatalog.search(query: str, region: str | None, limit: int = 20) -> tuple[Place, ...]`; `PlaceCatalog.get(place_id: str, source_version: str) -> Place | None`. Osobny lokalny katalog SQLite, budowany CLI z oficjalnego eksportu PRNG, czytany przez WWW tylko do odczytu. `GET /api/places?q=...&region=...` przyjmuje 2–80 znaków i zwraca najwyżej 20 wyników. Import waliduje CRS, zakres WGS84 i duplikaty ID; publikuje katalog atomowo, z datą źródła i SHA-256. Zapytania nazw korzystają z indeksu znormalizowanej nazwy. Brak sieci do geokodera w żądaniu klienta.

**Deliverable:** wpisz miejscowość → wybierz jednoznacznie nazwę z gminą/powiatem → podaj 1–200 km. Punkt opisany jako punkt miejscowości, nie adres firmy. `place_id` jest rozwiązywany po stronie serwera; jednoczesny wybór miejsca i ręcznych współrzędnych daje 422. Współrzędne pozostają opcją zaawansowaną. Wersja katalogu jest częścią szkicu; zniknięcie miejsca wymaga ponownego wyboru.

**DoD/tests:** dwie identyczne nazwy, polskie znaki, brak katalogu, katalog starej wersji, import w innym CRS i źle ułożone współrzędne, brak wyników, działanie bez JS przez natywny formularz wyszukania/wyboru. Zachowaj przecięcie promienia z wybranym obszarem i wykluczenia niedokładnych lokalizacji. Uruchom `tests/test_places.py`, `tests/test_order_configurator.py`, `tests/test_package_selection.py`.

**Source:** [PRNG — Geoportal](https://www.geoportal.gov.pl/pl/dane/panstwowy-rejestr-nazw-geograficznych-prng/) — źródło nazw i współrzędnych; przed importerem sprawdź rzeczywisty format eksportu i metadane CRS. Geokodowanie dokładnego adresu firmy i pełna mapa inwestycji są osobnymi rozszerzeniami; punkt miejscowości ma jawną dokładność.

- [ ] Przygotuj importer na małej fixture PRNG; po testach normalizacji i CRS podłącz wybór miejsca do istniejących filtrów. Pełny import danych wykonaj osobno od testów i requestów WWW.

## UX-04 — Namacalna próbka produktu

**Branch:** `codex/ux-demo-package` · **Rozmiar:** mały · **Zależności:** brak; może poprzedzać UX-02.

**Files:** nowe `app/routes/demo.py`, `tests/test_demo_package.py`; zmiany `app/main.py`, `app/templates/index.html`, `app/templates/order_configurator.html`; wykorzystaj `core/demo.py` i `core/export_package.py`.

**Contract:** `GET /przyklad` pokazuje trzy fikcyjne inwestycje; `GET /przyklad.xlsx` i `/przyklad.csv` korzystają z istniejącego generatora. Dane i pliki jawnie oznaczone jako fikcyjne, z ustalonym scenariuszem Nowe/Zmiany/Pełna baza; pobranie nie otwiera bazy i nie zapisuje pamięci dostaw.

**DoD/tests:** zgodność kolumn z produkcyjnym eksportem, polskie znaki/BOM CSV, trzy zakładki XLSX, odporność formuł CSV/XLSX, brak prawdziwych kontaktów i brak dostępu do SQLite. CTA próbki dostępne na landing page oraz przy wyniku. Bez wymyślonych cen, aktywowania sprzedaży i dodawania mechanizmu marketingowej wysyłki.

- [ ] Dodaj testy plików i zerowego dostępu do DB; podłącz istniejący generator oraz linki; uruchom `tests/test_demo_package.py`, `tests/test_export_package.py`, `tests/test_landing_page.py`.

## Etap 13 — Własne pliki i bezpieczne pobieranie

**Branch:** `codex/app-secure-downloads` · **Rozmiar:** średni · **Zależności:** zgodny etap 12 w main.

**Files:** nowe `core/package_access.py`, `app/routes/downloads.py`, `tests/test_downloads.py`; zmiany `core/storage.py`, `core/paid_packages.py`, `core/payment_store.py`, `app/config.py`, `app/main.py`, `app/security.py`, `app/templates/dashboard.html`, `tests/test_migration.py`, `projectplan.md`.

**Contract:** `POST /panel/paczki/{delivery_id}/link` wydaje krótko ważny link `GET /pobierz/{token}` dla jednej paczki i formatu. Wymagana sesja właściciela; token powiązany z właścicielem, plikiem i TTL 15 min. Token jest capability do ponowienia pobrania przez ten sam okres, a nie jednorazową blokadą po zerwanym połączeniu. Audyt zapisuje hash/ID tokenu, nigdy jego treść; proxy redaguje segment tokenu w access logach.

**Deliverable:** manifest `package_artifacts(delivery_id, format, relative_name, size, sha256)` i osobny audyt prób pobrania. Metadane obliczone podczas generowania przed krótkim zapisem. Katalog prywatny, brak dowolnych ścieżek klienta, odrzucanie symlinków i niezgodnego manifestu. Weryfikacja i streaming korzystają z tego samego otwartego uchwytu, aby uniknąć ponownego otwarcia podmienionej ścieżki; streaming bez aktywnej transakcji DB i bez blokady podmiany, z limitami równoległości i zamykaniem uchwytu po disconnect. Stan publikacji jest osobny od fizycznej gotowości plików; integracja publikacji następuje w etapie 15.

**DoD/tests:** właściciel 200; obce ID/token 404 bez danych o pliku; wygasły token po zalogowaniu prowadzi do wydania nowego; brak pliku/uszkodzenie daje bezpieczny komunikat z retry po naprawie; błędna ścieżka, symlink, disconnect i równoległe pobrania. Pobranie i `HEAD` nie zmieniają `doreczono_utc` ani pamięci. Dotychczasowe pliki otrzymują manifest w kontrolowanym backfillu przed udostępnieniem, bez zmiany rewizji dostaw.

- [ ] Dodaj migrację i regresje dostępu/streamu, wdroż resolver plików oraz przyciski XLSX/CSV; uruchom `tests/test_downloads.py`, `tests/test_paid_packages.py`, `tests/test_customer_dashboard.py`, `tests/test_migration.py`.

## UX-05 — Przegląd własnej paczki i uwaga przy inwestycji

**Branch:** `codex/ux-package-preview` · **Rozmiar:** średni · **Zależności:** etap 13 i UX-01.

**Files:** nowe `core/package_preview.py`, `app/routes/packages.py`, `app/templates/package_detail.html`, `tests/test_package_preview.py`; zmiany `app/main.py`, `app/routes/dashboard.py`, `app/templates/components/issue_form.html` i `app/templates/dashboard.html`.

**Contract:** `GET /panel/paczki/{delivery_id}`: stronicowanie po 50 pozycji i filtr Nowe/Zmiany/Pełna baza. Podgląd czyta autoryzowany, niezmienny CSV z manifestu etapu 13 poza transakcją; waliduje nagłówki/rozmiar i nie dołącza bieżącego `investments`. Limit 10 tys. pozycji jest spójny z generatorem. To widok paczki, nie nowy publiczny endpoint surowych leadów.

**Deliverable:** miejscowość, status/data, dlaczego pasuje, szacunek branżowy, źródło i jakość; istniejące mapowe linki tylko dla poprawnych adresów. „Zgłoś uwagę” przekazuje ID wybranej pozycji, a serwer ponownie sprawdza własność/uprawnienie. Po błędzie zachowuje opis i kontekst powrotu. Historia własnych zgłoszeń pokazuje przyjęcie; nie sugeruje rozstrzygnięcia, którego backend nie rejestruje.

**DoD/tests:** obce paczki i zgłoszenia odrzucone; nowy import nie zmienia starego podglądu; snapshot CSV i XLSX zachowują zgodne liczby; bezpieczny HTML; 503/429 nie gubią treści; brak artefaktu nie tworzy nowego „archiwum” z bieżących danych. Zachowaj istniejące regresje naprawionej utraty formularza.

- [ ] Dodaj regresję zmiany `investments` po publikacji, widok i kontekstowe zgłaszanie; uruchom `tests/test_package_preview.py`, `tests/test_customer_dashboard.py`, `tests/test_issue_tracker.py`.

## UX-06 — Czytelne logowanie i rozliczenia bez ręcznych identyfikatorów

**Branch:** `codex/ux-account-recovery` · **Rozmiar:** średni · **Zależności:** UX-01; nowy przebieg Checkout nadal wyłącznie testowy.

**Files:** `app/auth.py`, `app/routes/auth.py`, `app/routes/checkout.py`, `app/routes/dashboard.py`, `core/auth_store.py`, `core/checkout_store.py`, szablony auth/dashboard, `app/static/auth-verify.js`; testy `tests/test_auth_routes.py`, `tests/test_auth_billing.py`, `tests/test_auth_legacy_billing.py`, `tests/test_checkout_routes.py`, `tests/test_auth_service.py`.

**Contract:** dopuszczalne docelowe ścieżki po logowaniu to wyłącznie własny panel/paczka; brak zewnętrznego `next`. Nowe zamówienia rozpoczynane w uwierzytelnionej sesji mają serwerowe powiązanie konta i korelacji Checkout. Zwykły klient nie przepisuje UUID; legacy recovery pozostaje osobną ścieżką opartą na istniejącym dowodzie.

**Deliverable:** jasne instrukcje dla otwarcia w innej przeglądarce, wygaśnięcia i ponowienia; zachowanie celu po wygaśnięciu sesji. Idempotency token tej samej próby płatności jest zachowany po nieznanym wyniku; ponowne kliknięcie nie tworzy drugiego zamówienia. Brak automatycznego uznawania konta Stripe po emailu i brak przełącznika live.

**DoD/tests:** TTL, link w innej przeglądarce, skaner emaila, przejęcie powrotu/open redirect, obce zamówienie, podwójny submit, timeout Checkout i brak zaufanej korelacji. Zachowaj antyenumerację i ochronę same-origin.

- [ ] Dodaj regresje powrotu/idempotencji i nową zwykłą ścieżkę konta; uruchom wskazane pięć plików testowych oraz `tests/test_auth_service.py`.

## Etap 14 — Trwałe powiadomienie o gotowej paczce

**Branch:** `codex/core-delivery-notifications` · **Rozmiar:** średni · **Zależności:** etap 13.

**Files:** nowe `core/delivery_notifications.py`, `core/b2b_outbox.py`, `tests/test_delivery_notifications.py`; zmiany `core/storage.py`, `core/paid_packages.py`, `core/smtp_process.py` tylko jeśli wymaga tego istniejący port nadawcy, `tests/test_migration.py`, `projectplan.md`.

**Contract:** `enqueue_ready(delivery_id)` tworzy jeden rekord zdarzenia na publikację. Nadawca pracuje na trwałej kolejce i sprawdza odbiorcę oraz stan publikacji przed wysyłką. Wiadomość zawiera liczby i link do własnego panelu; po logowaniu klient wraca do paczki i uzyskuje aktualny link pobrania. Generator nie importuje FastAPI i nie tworzy tokenów HTTP.

**DoD/tests:** brak duplikatu enqueue, CRLF, odbiorca zmieniony po enqueue, SMTP reject/timeout, restart i ponowienie zadań. Rozróżnij accepted/rejected/unknown; przy unknown nie obiecuj exactly-once email. Wstrzymanie marketingu jest osobne od ustawień wiadomości usługowych; nie dodawaj kampanii.

- [ ] Dodaj migrację outbox i testy z atrapą SMTP; wdroż enqueue oraz ograniczony worker; uruchom `tests/test_delivery_notifications.py`, `tests/test_smtp_process.py`, `tests/test_migration.py`.

## Etap 15 — Dyspozytor i atomowa publikacja

**Branch:** `codex/core-dispatch-cli` · **Rozmiar:** średni · **Zależności:** etapy 13–14.

**Files:** nowe `core/dispatch.py`, `core/delivery_publication.py`, `tests/test_dispatch.py`; zmiany `main.py`, `core/storage.py`, `core/paid_packages.py`, `core/payment_store.py`, `core/delivery_memory.py`, `core/customer_dashboard.py`, `core/issue_tracker.py`, `core/package_access.py`, `projectplan.md`.

**Contract:** `python main.py --dispatch-deliveries --dry-run` i przebieg wykonawczy. Trwały klucz `(order_id, period_key)` oraz wspólny lease klienta dla pierwszej i kolejnych generacji. `publish_delivery(customer_id: int, delivery_id: int, lease_token: str, *, now: datetime) -> bool` atomowo zapisuje ledger publikacji/rewizje i zdarzenie outbox po sprawdzeniu plików oraz uprawnień; zwraca `False`, jeśli ten sam zestaw już opublikowano albo lease utracił ważność. Historyczne doręczenia pozostają podstawą deduplikacji; przygotowane, nieopublikowane wyniki nie zużywają pamięci. Nie zmieniaj rodzaju kupionej paczki delta/full w ramach refaktoryzacji; wariant i częstotliwość muszą zgadzać się z jawną ofertą przed włączeniem sprzedaży.

**DoD/tests:** dwa procesy, dwa zamówienia jednego klienta, nowa rewizja, pusty okres, awaria przed/po publikacji, wygaśnięty lease i anulowanie abonamentu przed publikacją. Jeden opublikowany zestaw na cykl, brak powtórzenia niezmienionej sprawy, brak blokady DB przy eksporcie/SMTP. Pobranie i zgłoszenie uwagi uwzględniają własne publikacje oraz historyczne doręczenia; sama gotowość pliku nie nadaje tych uprawnień. Stare pliki i dane pozostają bez zmian.

- [ ] Dodaj testy crash/retry na tymczasowej bazie, wspólny ledger/lease i tryb dry-run; uruchom `tests/test_dispatch.py`, `tests/test_paid_packages.py`, `tests/test_delivery_memory.py`, `tests/test_migration.py`.

## Etap 16 — Uruchamianie, odzyskiwanie i kopie

**Branch:** `codex/deploy-b2b-timers` · **Rozmiar:** mały · **Zależności:** etap 15.

**Files:** nowe jednostki `deploy/gunb-web.service`, `gunb-dispatch.service`, `gunb-dispatch.timer`, `gunb-backup.service`, `gunb-backup.timer`, `docs/WDROZENIE_B2B.md`; odpowiednie testy konfiguracji.

**Deliverable:** użytkownik usługi bez root, prywatne pliki, poprawny origin/proxy, kontrolowany restart, timer ponawiający trwałe zadania, kopie SQLite z uwzględnieniem WAL i prywatnych artefaktów. Harmonogram jest jawnie konfigurowany; nie ustanawia nowej obietnicy częstotliwości wobec klientów. Instrukcja restore obejmuje uzgodnienie późniejszych płatności i publikacji.

**DoD:** `systemd-analyze verify` na Linuxie; odtworzenie kopii do osobnego katalogu testowego; po restarcie brak utraty kolejki i podwójnej publikacji. Wdrożenie na VPS oraz rzeczywista poczta są oddzielnymi, autoryzowanymi kontrolami.

- [ ] Sprawdź unity, uprawnienia i odtworzenie na danych syntetycznych; udokumentuj kolejność migracja/start/rollback.

## Etap 17 — Stan usługi i jawna aktualność danych

**Branch:** `codex/app-b2b-readiness` · **Rozmiar:** średni · **Zależności:** etap 15; uruchomienie monitoringu po etapie 16.

**Files:** nowe `core/b2b_health.py`, `app/routes/readiness.py`, `tests/test_b2b_health.py`; zmiany `core/pipeline.py`, `core/storage.py`, `app/main.py`, `app/routes/orders.py`, szablony panelu/konfiguratora, dokumentacja wdrożenia.

**Contract:** publiczne `/zdrowie` pozostaje tanim liveness bez DB; osobne `/gotowosc` zwraca ogólny status bez danych klientów. Szczegóły przez uwierzytelniony dostęp operatora lub CLI. Rejestr importów przechowuje region, żądany/przetworzony zakres, wynik, czas zakończenia i błędy; sam min/max dat rekordów nie potwierdza pokrycia źródła.

**Deliverable:** informacja o ostatnim udanym imporcie i ograniczeniach danych dla wybranego regionu; kolejka/lease, błędy plików, czas zapytań i wolne miejsce dla operatora. Przy nieznanej historii „Brak informacji o pokryciu”; przy częściowym imporcie jawna etykieta. Progi alarmów konfigurowane w odniesieniu do harmonogramu.

**DoD/tests:** stara/niepełna historia, blokada DB, błąd importu, brak miejsca, stuck job; brak maili, tokenów i ścieżek w publicznych odpowiedziach. Indeksy i budżet zapytań sprawdzone na syntetycznej bazie; żadnego deklarowania SLA na podstawie fixture.

- [ ] Dodaj rejestr importów i testy degradacji; podłącz readiness oraz komunikaty; uruchom `tests/test_b2b_health.py`, `tests/test_pipeline.py`, `tests/test_order_configurator.py`, `tests/test_migration.py`.

## Etap 18 — Pełna ścieżka trzech person

**Branch:** `codex/qa-b2b-journeys` · **Rozmiar:** średni · **Zależności:** wyżej wymienione funkcje; testy częściowe powstają już we własnych PR-ach.

**Files:** nowe `tests/e2e/test_full_product_journey.py`, `tests/browser/test_full_product_journey.py`, fixtures; rozszerzenie joba browser z UX-02 w `.github/workflows/tests.yml` i jego developerskich zależności testowych.

**Scenarios:** Marek: miejscowość → promień → wynik → próbka/paczka. Karolina: powiaty → Nowe/Zmiany → zgodny CSV/XLSX → kolejna delta bez duplikatów. Tomasz: logowanie → status → pobranie → uwaga przy inwestycji → powrót po wygaśnięciu sesji. Dodatkowo pełen przepływ syntetycznego importu, mockowanego Checkout/webhooka, publikacji i outbox.

**DoD:** Chromium i Firefox, emulacja 390×844, klawiatura, wyłączony JS dla podstawowego przebiegu; manualny smoke Safari przed wydaniem. Utrata sieci, wolna odpowiedź, double submit, restart, obca sesja i zmiana danych po publikacji. Testy nie wykonują rzeczywistych płatności, importów zewnętrznych ani SMTP. Oddziel raport CI, kontrolę VPS i ręczny odbiór person.

- [ ] Dodaj scenariusze integracyjne i browser; uruchom `.venv\Scripts\python.exe -m pytest --ignore=tests/browser` oraz osobno `python -m pytest tests/browser --browser chromium --browser firefox` w środowisku browser. Raportuj rzeczywisty czas i wynik bez arbitralnego wspólnego limitu 30 s dla przeglądarek. Referencja: [Playwright Python](https://playwright.dev/python/docs/intro).

## Kolejność i odbiór

1. **Szybki efekt:** UX-01 → UX-02; UX-04 jest niezależnym, małym PR-em.
2. **Użyteczny pilotaż:** UX-03, etap 13 → UX-05, UX-06; połączenie z etapami 14–15 zapewnia powtarzalne dostawy.
3. **Gotowość operacyjna:** etapy 16 → 17 → 18. Każdy wcześniejszy PR ma własne testy i niezależne review.

Odbiór produktu: wszystkie trzy persony kończą swoje scenariusze bez wsparcia autora; eksport i podgląd są zgodne; ponowienie nie tworzy nowego zakupu/publikacji; awaria nie ujawnia obcych danych i nie usuwa pracy klienta. Czas, błędy oraz porzucenia mierzymy w pilotażu przed deklarowaniem poprawy konwersji lub SLA.
