# GUNB Lead Tool – dokument projektowy

Data: 2026-09-28 · Status: zatwierdzony do implementacji (specyfikacja dostarczona przez właściciela repozytorium)

## Cel

Codzienne pozyskiwanie leadów inwestycyjnych (pozwolenia na budowę, zgłoszenia) z rejestru RWDZ
GUNB dla lokalnych wykonawców: filtrowanie po województwie/powiecie/dacie, odrzucanie szumu,
geokodowanie działek (ULDK), zapis w SQLite z wykrywaniem zmian, powiadomienia Telegram/Discord
i eksport do Google Sheets.

## Ustalenia z rozpoznania źródła (stan na 2026-09-28)

| Fakt | Konsekwencja projektowa |
|---|---|
| Wyszukiwarka `wyszukiwarka.gunb.gov.pl` oraz moduł mapy (`/mapa/captcha/wyniki/`) wymagają CAPTCHA (securimage). | **Nie automatyzujemy wyszukiwarki i nie obchodzimy CAPTCHA.** |
| GUNB publikuje oficjalne „Dane do pobrania” (`/pliki_pobranie/wynik_<województwo>.zip` oraz `wynik_zgloszenia_2022_up.zip`), dane od 2016 r., aktualizacja co noc. | Scraper pobiera paczki ZIP; serwer zwraca `ETag`, `Last-Modified`, `Accept-Ranges` → pobieranie warunkowe i wznawianie. |
| Strona deklaruje separator `#`, faktycznie jest `;`; plik ma BOM UTF-8; kolumna `cecha` występuje dwukrotnie; pola z cudzysłowami mogą zawierać znaki nowej linii. | Wykrywanie separatora z nagłówka, deduplikacja nazw kolumn, parser `csv`. |
| Jeden wiersz = jedna działka; sprawa może mieć setki wierszy; wiersze sprawy **nie zawsze** sąsiadują. | Grupowanie po numerze sprawy w słowniku (po filtrach), dopiero potem paginacja. |
| Dwa schematy: pozwolenia (31 kolumn, m.in. `numer_gunb`, `data_wydania_decyzji`, `nazwa_inwestor`) i zgłoszenia (26 kolumn, m.in. `numer_ewidencyjny_system`, `stan`, `obiekt_kod_pocztowy`). | Mapowanie kolumn per źródło z nazwami alternatywnymi; brak kolumny ID → czytelny błąd „zmiana formatu”. |
| Paczki zawierają wyłącznie sprawy zakończone pozytywnie: pozwolenia z datą decyzji, zgłoszenia ze stanem „Brak sprzeciwu”. Stany „w trakcie”, odmowy, sprzeciwy są widoczne tylko w wyszukiwarce (CAPTCHA). | Pełny model 9 statusów RWDZ i historia zmian są zaimplementowane, ale przy obecnych paczkach lead pojawia się od razu jako `decyzja` / `brak_sprzeciwu`. |
| ULDK `GetParcelByIdOrNr` sam dopasowuje arkusz mapy (`…0058.52/11` → `…0058.AR_1.52/11`); część starszych działek już nie istnieje (podziały). WKT w EPSG:4326 ma kolejność „lon lat”. | Zapytanie bez arkusza, preferencja wyniku z arkuszem z RWDZ, fallback: środek obrębu (`GetRegionById`). |

## Architektura

```
main.py (CLI) ─► pipeline.LeadPipeline
                   ├─ gunb_scraper.GunbScraper ──► http_client.ResilientHttpClient
                   ├─ data_filter.LeadFilter
                   ├─ geocoding_uldk.UldkGeocoder ─► UldkClient ─► ResilientHttpClient
                   ├─ storage.LeadRepository (SQLite; cache geokodowania)
                   └─ exporter (TelegramNotifier, DiscordNotifier, GoogleSheetsExporter, formatery)
```

- `http_client` – retry z wykładniczym backoffem i jitterem, obsługa `Retry-After` (nagłówek i JSON),
  rotacja User-Agent, losowe odstępy między zapytaniami, pobieranie warunkowe i wznawiane (`Range`).
- `gunb_scraper` – pobieranie paczek, strumieniowe parsowanie CSV, filtrowanie (województwo, powiat,
  data), scalanie wierszy w sprawy, paginacja wyników (`Page`).
- `data_filter` – normalizacja tekstu, flagi `is_residential` / `is_commercial` / `is_noise`,
  kategoria biznesowa, ekstrakcja inwestora i projektanta (pracownia, uprawnienia).
- `geocoding_uldk` – identyfikator działki → WGS84 (centroid WKT), linki Google Maps i Geoportal.
- `storage` – tabela `investments`, historia statusów, stan wysyłki per kanał, synchronizacja arkusza.
- `exporter` – wiadomości Markdown (Telegram MarkdownV2 / Discord), wysyłka, upsert do Google Sheets.

## Wykrywanie zmian

`upsert` porównuje nowy rekord z zapisanym: `NEW` → wstawienie; inna wartość `status` →
`STATUS_CHANGED` (wpis w `status_history`, wyzerowanie `czy_wyslano`, ponowne powiadomienie);
inne pola treści → `UPDATED` (do synchronizacji arkusza, bez ponownego powiadomienia);
brak zmian → `UNCHANGED`.

## Obsługa błędów

Błędy sieci i 5xx/429 → retry; uszkodzona paczka (rozmiar, ZIP) → ponowienie pobrania, stary plik
pozostaje; zmiana formatu CSV → wyjątek z nazwą brakującej kolumny; błąd ULDK dla sprawy → lead
zapisany bez współrzędnych; błąd wysyłki → wiadomość zostaje w kolejce (`czy_wyslano = 0`).

## Testy

pytest na danych syntetycznych (bez danych osobowych z rejestru – repozytorium jest publiczne);
atrapy klienta HTTP, SQLite w pamięci, atrapa arkusza gspread.
