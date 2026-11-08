# UX-03 — lokalny wybór miejscowości

Spec: UX-03 w 2026-11-06-ux-qol-roadmap.md; projectplan.md. BASE8fc6b93ccb6a1a791a3a883d657a33fcffcbd8a3, branch codex/ux-place-radius.

## Decyzje i kontrakty

- Zweryfikowano 2026-11-07 oficjalny PRNG WFS2 GetCapabilities i dwie pozycje M1_UrzedoweNazwyMiejscowosci w GML3.2: namespace MapServer, IDENTYFIKATORPRNG/NAZWAGLOWNA/GMINA/POWIAT/IDENTYFIKATORGMINY, Point srsName urn:ogc:def:crs:EPSG::4326, kolejność latitude longitude. Źródło: https://www.geoportal.gov.pl/pl/dane/panstwowy-rejestr-nazw-geograficznych-prng/ i https://mapy.geoportal.gov.pl/wss/service/PZGiK/PRNG/WFS/GeographicalNames . Badanie objęło tylko dwie publiczne pozycje, nie pełny import.
- Importer offline obsługuje ten jawny format WFS2 GML M1, odrzuca inne CRS, odwrócone osie, duplikaty ID, puste dane i niedokończone/paginowane eksporty. Nie konwertuje domyślnego EPSG2180 ani nie zgaduje osi. Fixture jest syntetyczna, oparta na sprawdzonym schemacie.
- Osobny SQLite, application_id/schema1, data źródła i SHA256 dokładnych bajtów wejścia. Import do prywatnego pliku, atomowe os.replace; brak nadpisywania nieznanej bazy lub symlinka. WWW tylko mode=ro/immutable, połączenie na operację, indeksy prefix(name) i region/name. Brak migracji bazy klientów.
- Place(id/name/municipality/county/region/source_version,lat/lon), region jako TERYT. Search2–80 znaków, max20, normalizacja polskich znaków; get wymaga ID i dokładnej wersji. Brak katalogu to503, brak trafień to200pusta lista, nieaktualny wybór422.
- /api/estimate zachowuje cztery pola sukcesu. Opcjonalne place_query/place_id/place_version; serwer rozwiązuje punkt, nie przyjmuje punktu równocześnie z ręcznymi lat/lon. Wspólny promień1–200km, dotychczasowe przecięcie i jakość lokalizacji bez zmian. Testowy Checkout używa tej samej walidacji poza zapisem/siecią.
- Natywny przycisk szukania i select z nazwą/gminą/powiatem; JS wyłącznie usprawnia tę ścieżkę. Bez automatycznego wyboru pierwszego wyniku. Timeout/stale-result fencing, jawne wybranie/usunięcie miejsca. Współrzędne zaawansowane nadal dostępne.
- Draft zachowuje siedem pól v1; rozszerzony v2 zapisuje też query/ID/wersję, bez ukrytego fallbacku na współrzędne. Stary draft działa. Konflikt geograficzny, usunięte miejsce lub zmieniona wersja wymagają jawnej korekty.

## Task 1: Katalog i importer
RED→GREEN testy schematu, nazw/duplikatów/CRS/osi, atomiczności, metadanych i indeksów; CLI python -m core.places.

## Task 2: Odczyt HTTP i natywne formularze
RED→GREEN API search/get-resolution,503/422, native search/wybór, zachowanie scope, konflikt GPS/ID i source-version; konfiguracja GUNB_WEB_PLACES_PATH.

## Task 3: Picker i draft
RED→GREEN Chromium/Firefox: wybór z dwóch nazw, reload/draft, stale lookup, niedostępny katalog, zmiana wersji, JS-off. Zachować wszystkie regresje UX02.

## Task 4: Weryfikacja i PR
Samodzielny audyt bezpieczeństwa i trzy persony; unit/browser, układ/klawiatura; niezależny whole-PR reviewer, poprawki, exactHEAD CI i PR z audit. Wyłącznie ręczny merge, bez wdrożenia i pełnego importu.
