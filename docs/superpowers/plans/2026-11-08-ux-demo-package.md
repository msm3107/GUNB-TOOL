# UX-04 — namacalna próbka produktu

Spec: UX-04 w 2026-11-06-ux-qol-roadmap.md. BASE3adfb53458149cda3af3b17550aecd6266812b43 (PR33 scalony przez człowieka); branch codex/ux-demo-package.

## Kontrakty i decyzje

- GET /przyklad: trzy fikcyjne inwestycje, jedna demonstracja każdego wariantu Nowe/Zmiany/Pełna baza. To trzy przykładowe wiersze, nie rzeczywista dostawa ani kompletna baza regionu.
- Fikcyjny scenariusz ma stałą datę2026-11-01 i branżę dach, bazuje na demo_leads. Dom/decyzja dotąd nieznana klientowi, hala/zmiana na decyzję, szkoła/pełna baza z zapisaną fikcyjną historią; wszystkie decyzje mieszczą się w istniejącym oknie dachowym dla tej daty. Bez prawdziwych kontaktów, współrzędnych, numerów działek i linków mapowych. Nazwy zamierzeń i pliki jawnie oznaczone DANE FIKCYJNE.
- GET/HEAD /przyklad.xlsx i /przyklad.csv: istniejące write_xlsx/write_csv, dokładnie produkcyjne kolumny, trzy zakładki i CSV UTF8/BOM, te same trzy pozycje. BytesIO i mały cache2 stałych artefaktów na proces; brak użytkownikowych parametrów pliku, ścieżek, DB lub klasyfikatora historii dostaw. No-store i stałe attachment filename z fikcyjną etykietą.
- HTML korzysta z tych samych domain items i istniejących helperów scoring/timing; skrót pól bez obietnic faktycznego etapu robót. Szablon dziedziczy branding i natywne details; bez JS.
- CTA na landing i przy wyniku. Link z konfiguratora otwiera jawną nową kartę, by zachować niezapisane filtry także przy braku JS/storage.
- Bez cen, aktywowania sprzedaży, migracji, zależności, marketingowej wysyłki ani pobierania paczek klientów (etap13 osobno). P3 z PR33 nie należy do tego zakresu.

## Task 1: Próbka i pliki
RED→GREEN tests/test_demo_package.py: trzy pozycje/scenariusze, schema/BOM/diacritics/sheets/history, formula/XSS resilience, no contacts/maps/SQLite/memory, nagłówki GET/HEAD oraz błąd generowania/retry. Współdzielony generator, zero DB, bounded bytes.

## Task 2: Podgląd i wejścia
Widok HTML z jawnymi etykietami i ograniczeniami; landing CTA i link przy wyniku bez utraty formularza. Testy linków/dostępności/bezpiecznego HTML, kontrola wizualna320/390/768/1280 i klawiatury, browser download bezJS.

## Task 3: Audyt i PR
Fullunit/browser/Ruff/ASCII/diffcheck, samodzielny audyt i persony heurystyczne; niezależny whole-PR reviewer, poprawki i exactHEADCI. PR34 z audit, wyłącznie ludzki merge, bez wdrożenia.
