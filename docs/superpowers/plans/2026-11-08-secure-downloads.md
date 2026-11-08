# Etap 13 — własne pliki i bezpieczne pobieranie

> Wykonanie: superpowers:executing-plans, test-first; niezależny whole-PR reviewer na końcu.

**Goal:** właściciel pobiera niezmienny, wcześniej udostępniony plik przez odnawialny link15min, bez zużywania pamięci dostaw.
**Architecture:** core/package_access.py przechowuje manifesty, uprawnienia i audyt; core/package_files.py otwiera/weryfikuje prywatne pliki. app/routes/downloads.py obsługuje sesję, same-origin POST i streaming tego samego uchwytu. Brak zależności core od HTTP.
**Tech Stack:** obecny Python/SQLite/FastAPI/Jinja2; stdlib os/hashlib/ctypes dla kontroli plików Windows/POSIX.
**Spec:** etap13 w docs/superpowers/plans/2026-11-06-ux-qol-roadmap.md, projectplan.md; BASE36c1422c29e03a2da9174a5db465e52f0e8ad45c po ludzkim mergePR34.

## Global Constraints
- Brak merge/deploy, nowych zależności, migracji WWW i dostępu do realnych danych/SMTP/Stripe.
- Migracja v22→v23 przez istniejący backup-before-DDL; manifest, granty15min i audyt prób; osobna tabela publikacji jest pusta do integracji etapu15.
- Dostęp: sesja właściciela, opłacone zamówienie i historyczne doreczono_utc albo osobna publikacja; koniec abonamentu nie odbiera archiwum; sama gotowość plików nie publikuje.
- Podpisany losowy token + hash w DB, związany z klientem/formatem/manifestem; ponawialny do TTL, bez surowego tokenu/IP/UA w audycie.
- Prywatny katalog poza static; wyłącznie basename, odrzucanie symlinków/reparse i nieregularnych plików, limit64MiB/plik i weryfikacji5s; SHA256/rozmiar i streaming na tym samym uchwycie. POSIXopenat/O_NOFOLLOW, Windows natywne uchwyty bez współdzielenia zapisu/usuwania.
- Generowanie, hashowanie i streaming poza transakcją i blokadą podmiany SQLite; 4 aktywne weryfikacje/streamy na proces, close także przy disconnect/sendfailure.
- Brak zmian doreczono_utc, rewizji ani klasyfikatora. Audyt oznacza próbę, nie odbiór przez człowieka.
- Stare pliki: jawny CLI backfill jednej dostawy, atomowe dwa manifesty; bez publikowania i zmiany starych dat.
- Brak sekretnego tokenu w return URL logowania; ekran401 otwiera logowanie w nowej karcie i zachowuje link; po zalogowaniu retry, po TTL410 z formularzem wydania nowego.

## Review Focus
- Parent symlink/Windows reparse lub rename między sprawdzeniem a otwarciem nie może wyprowadzić poza katalog.
- Ta sama sesja nie wystarcza do pobrania obcej lub jedynie przygotowanej paczki; stary token nie nadaje nowych praw po zmianie własności/manifestu.
- Zerwany stream, błąd send i HEAD zwalniają uchwyt oraz slot; żaden nie potwierdza doręczenia.
- Blokada bazy po autoryzacji oraz brak/uszkodzenie pliku dają bezpieczny retry bez ścieżek i sekretów.
- Redakcja tokenu w logach aplikacji i przykładzie proxy; także query string nie trafia do access logu.

## Task 1: Manifest i bezpieczny uchwyt
Files: core/package_files.py, core/package_access.py, core/storage.py, tests/test_downloads.py, tests/test_migration.py.
Interfaces: Artifact(format,relative_name,size,sha256); inspect_artifact(root,name,format), open_verified(root,artifact); PackageAccessStore(path).record_artifacts(delivery_id,artifacts).
- [ ] RED: poprawny plik, traversal/symlink/parent/fifo, korupcja, replace po verify, migracja z backupem i zachowaną historią.
- [ ] Implementacja minimum; GREEN test_downloads + migration + storage; commit.

## Task 2: Grant, HTTP i cykl plików
Files: app/routes/downloads.py, app/config.py, app/main.py, app/security.py, core/paid_packages.py, core/payment_store.py, tests/test_downloads.py, tests/test_paid_packages.py.
Interfaces: POST /panel/paczki/{id}/link format=xlsx|csv; GET/HEAD /pobierz/{token}; grants bound to owner/artifact, expires15min, repeatedGET allowed. Store.issue/resolve/audit kończą połączenie przed plikowymIO.
- [ ] RED: owner/foreign/session/CSRF, expired/login/renew, missing/corrupt/retry, HEAD/memory, slot/disconnect/sendfailure/DBlock, manifest generator/backfill, no raw token in logs/audit.
- [ ] Implementacja i GREEN całości odpowiednich testów; commit.

## Task 3: Panel, dokumentacja i przekazanie
Files: core/customer_dashboard.py, app/templates/dashboard.html, app/templates/download_status.html, docs/nginx-downloads.conf.example, README.md, projectplan.md; tests/test_customer_dashboard.py i browser/test_downloads.py.
- [ ] RED: własne przyciski i brak dla przygotowanych/obcych/bezmanifestu, native download+renew, keyboard/layout.
- [ ] UI/docs, fullunit/browser/Ruff/ASCII/diffcheck, persony heurystyczne, niezależny whole-PR reviewer, poprawki i exactHEADCI; PR35 z audit, bez merge/deploy.
