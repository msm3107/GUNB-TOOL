# Etap 14 — trwałe powiadomienia o paczce Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans, test-first, one independent whole-PR review at the end.

**Goal:** Klient otrzymuje usługowy email o własnej opublikowanej paczce, a restart i timeout mają jawny wynik w trwałej kolejce.
**Architecture:** SQLite outbox z unikalnym delivery_id i atomowym triggerem publikacji. Krótkie rezerwacje oraz osobna faza sending poprzedzają SMTP poza blokadami. Obecny SupervisedSMTP obsługuje tekst i opcjonalną alternatywę HTML. Link prowadzi do paczki przez istniejące logowanie.
**Tech Stack:** Python >=3.10, SQLite WAL, stdlib i istniejący port SMTP.
**Spec:** Etap14 docs/superpowers/plans/2026-11-06-ux-qol-roadmap.md i projectplan.md.
**BASE:** 920166f7641e2ff7ed0efbd3933166611fc6a383, PR37 zmergowany przez człowieka.

## Global Constraints

- Core działa samodzielnie. WWW korzysta wyłącznie z istniejącej bazy zgodnego schematu.
- Migracja v24 z kopią przed DDL i zachowaniem wszystkich wcześniejszych rekordów.
- SMTP poza transakcją i blokadą podmiany bazy. Wysyłka domyślnie disabled, jawny tryb test.
- Wiadomość zawiera liczniki oraz link /panel/paczki/{id}. Sesja właściciela chroni zawartość paczki.
- Odbiorca, właściciel, płatność, publikacja i manifest sprawdzane ponownie przed SMTP.
- Bez migracji historycznych dat doręczeń, zmian pamięci dostaw i automatycznego emaila do dawnych publikacji.
- Sprzedaż wyłączona, testy wyłącznie syntetyczne/SMTP fake, sekrety poza logami i repr.

## Review Focus

- Zmiana adresu/właściciela lub cofnięcie publikacji/płatności po enqueue — anulowanie wysyłki.
- Crash po claim przed sending — bezpieczny retry po lease. Crash po sending — unknown bez automatycznego resend.
- Akceptacja SMTP i utrata zapisu wyniku — unknown po lease. Późny worker jest odgrodzony tokenem.
- Istniejące publikacje podczas migracji — brak automatycznej wysyłki wstecz.
- Generator tworzący tylko pliki — brak powiadomienia przed publikacją etapu15.

### Task 1: Trwała kolejka i migracja

**Files:** core/storage.py, core/b2b_outbox.py, tests/test_delivery_notifications.py, tests/test_migration.py.
**Interfaces:** B2BOutbox.enqueue_ready(delivery_id)->bool, claim(now)->ticket|None, begin_send(ticket,now)->message snapshot|None, finish(ticket,result,now)->bool, recover_stale(now,limit)->None.
- [ ] RED — publikacja tworzy jeden outbox, jawne enqueue jest idempotentne, przygotowana paczka nie tworzy eventu. Migracja23→24 zachowuje dane i tworzy kopię przed DDL.
- [ ] Implementacja schematu/triggera i krótkich transakcji z lease90s. Statusy pending/leased/sending/accepted/rejected/unknown/cancelled.
- [ ] GREEN — testy kolejki, migracji i przygotowywania paczek. Commit.
Expected: deduplikacja, aktualny odbiorca oraz fencing i odzyskiwanie lease przechodzą na tymczasowej bazie.

### Task 2: Email, worker i zwykłe wejście CLI

**Files:** core/delivery_notifications.py, core/email_sender.py, core/smtp_process.py, tests/test_delivery_notifications.py, tests/test_smtp_process.py.
**Interfaces:** DeliveryNotificationSettings.from_env, DeliveryNotificationWorker.run_pending(limit=10)->dict[str,int], python -m core.delivery_notifications --database PATH --limit N.
- [ ] RED — wiadomość text/HTML zawiera tylko liczniki i link do paczki, ochrona CRLF, disabled bez DB/SMTP, aktualizacja odbiorcy po claim, accepted/rejected/retry/unknown, restart dwóch workerów i brak blokad w SMTP.
- [ ] Implementacja nadzorowanego SMTP. Retry tylko dla wyniku retry, backoff60s rosnący do3600s, maksimum5 prób. Unknown/rejected terminalne. Limit1–100 na przebieg.
- [ ] GREEN — testy wiadomości/worker/SMTP, istniejące auth SMTP zachowuje kontrakt. Commit.
Expected: można uruchomić osobny ograniczony worker z jawną konfiguracją testową, prywatne dane nie trafiają do logów.

### Task 3: Kontrakt produktu, QA i PR

**Files:** README.md, projectplan.md, .env.example tylko wyłączone ustawienia.
- [ ] Opisać uruchomienie, migrację/rollback, accepted/rejected/unknown, ponawianie i granicę publikacji z etapem15.
- [ ] Pełne unit/browser, Ruff krytyczne, ASCII i BASE..HEAD whitespace. Heurystyczne trzy persony.
- [ ] Niezależny whole-PR review, poprawki z regresjami, CI dokładnego HEAD, PR z audit.
Expected: PR gotowy do ludzkiej oceny z jawnie opisanymi ograniczeniami SMTP i statusami.
