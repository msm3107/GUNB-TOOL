# Żółta Tablica

[![tests](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml/badge.svg)](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml)

Żółta Tablica rozwija istniejący silnik pobierania i wzbogacania danych z rejestru RWDZ GUNB. Projekt jest przebudowywany według aktualnego [planu](projectplan.md) na niezależny silnik w katalogu core oraz aplikację B2B w katalogu app.

## Aktualny zakres

Silnik obsługuje pobieranie paczek RWDZ, filtrowanie i klasyfikację spraw, geokodowanie ULDK, zapis w SQLite oraz kontrolę stanu bazy. CLI jest dostępne w pliku main.py.

Silnik B2B korzysta ze schematu SQLite v18 dla klientów, zamówień, dostaw i zgłoszeń jakości danych. Klasyfikator rozróżnia nowe inwestycje, zmiany i pełny snapshot, a `core.export_package` zapisuje sklasyfikowane sprawy do arkusza XLSX z trzema zakładkami oraz płaskiego CSV z BOM UTF-8. Pełny snapshot zachowuje wsadowo pobraną historię statusów; eksport zawiera też scoring i jego uzasadnienie, opcjonalne szacunkowe okno branżowe, organ, lokalizację i linki mapowe. Tekst jest zapisywany jako tekst arkusza, a CSV chroni pola przed interpretacją jako formuły.

`core.issue_tracker.IssueTracker.report_issue(...)` zapisuje uwagi do spraw wcześniej doręczonych danemu klientowi. Obsługuje typy `zly_telefon`, `to_nie_inwestor_tylko_projektant`, `inwestycja_juz_stoi` i `inna_uwaga`. Opis jest zwykłym tekstem do 1000 znaków wejściowych, a limit wynosi 20 nowych zgłoszeń na godzinę na klienta. Ponowienie tego samego typu uwagi do tej samej sprawy zwraca oryginalne zgłoszenie, bez zmiany opisu i zużywania limitu. Wywołująca warstwa aplikacji musi ustalać `customer_id` z uwierzytelnionej sesji.

Zgłoszenie nadaje leadowi etykietę „Zgłoszona uwaga — wymaga weryfikacji”, widoczną również w kolumnie „Jakość danych” w XLSX/CSV. Opis i autor zgłoszenia nie trafiają do paczki. Etykieta nie potwierdza błędu ani ukończenia budowy i nie zmienia statusu GUNB; brak etykiety nie oznacza zweryfikowanej poprawności. Import zachowuje flagę. Formularz WWW i rozstrzyganie zgłoszeń nie są jeszcze dostępne.

Katalog app udostępnia publiczną stronę B2B w FastAPI/Jinja2: trzy filary produktu, trzy rozwijane fikcyjne przykłady inwestycji, porównanie planowanej Paczki i Abonamentu oraz FAQ. Przykłady są statyczne i nie pobierają danych z bazy. Konfigurator `/zamowienie` oraz `POST /api/estimate` pozwalają policzyć pasujące sprawy w istniejącej bazie. Ceny zostaną podane przed uruchomieniem sprzedaży; zamówienia internetowe, płatności i automatyczne doręczanie paczek pozostają kolejnymi etapami planu projektu.

Runtime botów Telegram/WhatsApp, inne kanały powiadomień i stary instalator VPS nie należą do bieżącego produktu.

## Uruchomienie silnika

Utwórz środowisko i zainstaluj zależności:

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -c requirements.lock -r requirements.txt

Na Linuxie i macOS zamiast .venv\Scripts\python.exe użyj .venv/bin/python.

Najważniejsze akcje CLI:

    python main.py --help
    python main.py --stats
    python main.py --zdrowie
    python main.py --fetch --no-geocode --limit 10

Pobieranie danych kontaktuje się z oficjalnymi źródłami GUNB i — jeśli geokodowanie jest włączone — z usługą ULDK. Domyślną konfigurację obszaru i filtrów zawiera config.yaml; przed pełnym importem sprawdź zakres oraz ścieżkę bazy.

## Uruchomienie WWW

Po instalacji zależności uruchom serwer lokalny:

    python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

`GET /` renderuje stronę B2B z informacją o przygotowywanym portalu. Przykłady i FAQ rozwijają się natywnie przez HTML `details/summary`, również bez JavaScriptu. Strona korzysta z lokalnego CSS, logo, ilustracji budowlanych i fontów Manrope przeniesionych z poprzedniej witryny Żółtej Tablicy. Pochodzenie i licencję opisuje [WEB_ASSETS.md](docs/WEB_ASSETS.md). Nie ładuje analityki, zewnętrznych fontów ani skryptów. Nie zawiera formularza zamówienia ani zakupu.

`GET /zdrowie` zwraca `{"status":"ok","service":"gunb-web"}`. Sonda HTTP potwierdza działanie serwera WWW; nie czyta bazy ani blokad silnika. Pełną diagnostykę silnika nadal wykonuje `python main.py --zdrowie`.

### Konfigurator i estymator

Ustaw `GUNB_WEB_DB_PATH` na ścieżkę istniejącej bazy WAL w aktualnym schemacie (v18), wcześniej zainicjalizowanej przez CLI. Bez tego ustawienia strona główna i liveness działają, natomiast estymator informuje o niedostępności. Serwer WWW nie tworzy i nie migruje bazy oraz nie ładuje konfiguracji silnika ani jego `.env`.

`GET /zamowienie` pokazuje formularz, `POST /zamowienie` zwraca wynik HTML (także bez JavaScriptu), a `POST /api/estimate` przyjmuje JSON. Przykład:

```json
{"wojewodztwo":"28","branza":"materialy","powiaty":["2814"],"kategorie":["komercyjna"]}
```

Województwo to dwucyfrowy kod TERYT; powiaty (opcjonalnie, do 20) muszą należeć do tego województwa. Branże pochodzą z `core.stages.TRADES`, typy inwestycji z `core.models.LEAD_CATEGORIES`. Materiały i stan surowy nie ograniczają dat; pozostałe branże stosują obecne szacunkowe okno etapu i mnożnik dla większych budynków. Opcjonalne `lat`, `lon`, `promien_km` podaje się łącznie: baza WGS84 w Polsce (49–55,1 N; 14–24,5 E), promień 1–200 km. Licznik promienia pomija lokalizacje gminne i znane rozbieżności działek.

Odpowiedź zawiera wyłącznie `count`, `with_parcel_coordinates`, `without_parcel_coordinates`, `last_seen`. To liczby zapisanych, niesklasyfikowanych jako szum spraw z pozytywnym lub trwającym statusem, a nie gwarancja przyszłej dostawy ani liczba NEW dla klienta. `last_seen` jest najnowszą obserwacją pasujących rekordów; nie dowodzi aktualności całego rejestru. Puste wyniki to zero, natomiast brak/niezgodność bazy, podmiana lub przekroczenie budżetu odczytu daje 503, błędne filtry 422, za duże żądanie 413, niewłaściwy format 415.

Branże z opóźnionym oknem wymagają wcześniejszego importu odpowiedniej historii (np. przez `--historical` CLI). Przy domyślnym krótkim zakresie importu mogą nie mieć wyników. Estymator sam nie rozszerza zakresu pobierania danych.

Żądania mają limit 8 KiB; zapytania SQLite budżet 500 ms i limit współbieżnych operacji. Każdy odczyt otwiera własne połączenie `mode=ro`/`query_only` pod kanoniczną blokadą podmiany bazy. Import WAL może trwać równolegle. Obecna blokada odczytów jest wyłączna, dlatego równoległy odczyt może szybko otrzymać 503 i ponowić próbę. Nie ma operacji sieciowych ani zapisów biznesowych. Selekcja używa indeksu pokrywającego `ix_investments_package` dodanego w v18. Migrację wykonuje silnik CLI (np. `python main.py --stats`, z właściwym `--config`), przed zmianą tworząc kopię przez `VACUUM INTO`. WWW odrzuca bazę v17 do czasu migracji. Indeks zwiększa rozmiar bazy i koszt aktualizacji pól selekcji; nie zmienia zapisanych spraw ani pamięci dostaw. Wycofanie kodu wymaga odtworzenia kopii zgodnej ze starszą wersją, z uwzględnieniem późniejszych importów. CORS nadal dopuszcza wyłącznie GET/HEAD dla jawnych originów; formularz i estymator działają w tym samym originie.

Pomiar syntetyczny można odtworzyć poleceniem `python -m tests.benchmark_package_estimator --runs 7`; warianty `--unique --municipality` i `--ring --municipality` sprawdzają różne geometrie, w tym punkty przy granicy promienia. Skrypt tworzy wyłącznie tymczasową bazę 100 tys. fikcyjnych spraw i mierzy pełny odczyt serwisu (blokada, nowe połączenie, zapytanie). Cel <100 ms dotyczy mierzonego scenariusza i sprzętu, nie dowolnego rozmiaru bazy. Budżet 500 ms pozostaje niezmieniony.

CORS jest domyślnie zamknięty. Jeśli osobny frontend potrzebuje dostępu, ustaw w środowisku procesu `GUNB_WEB_CORS_ORIGINS` jako listę jawnych originów oddzielonych przecinkiem, np. `https://portal.example.test,http://localhost:3000`. Wartości nie mogą zawierać wildcardów, ścieżek, danych logowania ani parametrów URL; dozwolone są obecnie wyłącznie metody GET/HEAD, bez credentials. Konfiguracja WWW nie ładuje pliku `.env` silnika.

Odpowiedzi, także błędy 500, otrzymują CSP i nagłówki ochronne. HSTS jest ustawiany tylko dla schematu HTTPS w ASGI. Przy terminowaniu TLS przez reverse proxy serwer ASGI musi ufać wyłącznie właściwym proxy; aplikacja nie interpretuje samodzielnie `X-Forwarded-Proto`. Publiczna dokumentacja API i tryb debugowania są wyłączone. Uruchomienie serwera nie uruchamia importu GUNB ani nie otwiera SQLite; silnik pozostaje niezależny od pakietów WWW.

## Testy

Zainstaluj zależności deweloperskie i uruchom zestaw testów:

    python -m pip install -c requirements.lock -r requirements-dev.txt
    python -m pytest -o addopts= -q

CI uruchamia testy na Pythonie 3.10–3.14.

## Struktura

- core/ — autonomiczny silnik, modele, konfiguracja, SQLite i import RWDZ.
- app/ — fundament WWW i przyszły portal B2B, bez zależności silnika od frameworka WWW.
- tests/ — testy silnika, migracji schematu, klasyfikatora dostaw, jakości danych i eksportu XLSX/CSV.
- projectplan.md — bieżący plan etapów produktu.
