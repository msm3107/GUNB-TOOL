# GUNB Lead Tool

[![tests](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml/badge.svg)](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml)

GUNB Lead Tool rozwija istniejący silnik pobierania i wzbogacania danych z rejestru RWDZ GUNB. Projekt jest przebudowywany według aktualnego [planu](projectplan.md) na niezależny silnik w katalogu core oraz aplikację B2B w katalogu app.

## Aktualny zakres

Silnik obsługuje pobieranie paczek RWDZ, filtrowanie i klasyfikację spraw, geokodowanie ULDK, zapis w SQLite oraz kontrolę stanu bazy. CLI jest dostępne w pliku main.py.

Silnik B2B korzysta ze schematu SQLite v17 dla klientów, zamówień, dostaw i zgłoszeń jakości danych. Klasyfikator rozróżnia nowe inwestycje, zmiany i pełny snapshot, a `core.export_package` zapisuje sklasyfikowane sprawy do arkusza XLSX z trzema zakładkami oraz płaskiego CSV z BOM UTF-8. Pełny snapshot zachowuje wsadowo pobraną historię statusów; eksport zawiera też scoring i jego uzasadnienie, opcjonalne szacunkowe okno branżowe, organ, lokalizację i linki mapowe. Tekst jest zapisywany jako tekst arkusza, a CSV chroni pola przed interpretacją jako formuły.

`core.issue_tracker.IssueTracker.report_issue(...)` zapisuje uwagi do spraw wcześniej doręczonych danemu klientowi. Obsługuje typy `zly_telefon`, `to_nie_inwestor_tylko_projektant`, `inwestycja_juz_stoi` i `inna_uwaga`. Opis jest zwykłym tekstem do 1000 znaków wejściowych, a limit wynosi 20 nowych zgłoszeń na godzinę na klienta. Ponowienie tego samego typu uwagi do tej samej sprawy zwraca oryginalne zgłoszenie, bez zmiany opisu i zużywania limitu. Wywołująca warstwa aplikacji musi ustalać `customer_id` z uwierzytelnionej sesji.

Zgłoszenie nadaje leadowi etykietę „Zgłoszona uwaga — wymaga weryfikacji”, widoczną również w kolumnie „Jakość danych” w XLSX/CSV. Opis i autor zgłoszenia nie trafiają do paczki. Etykieta nie potwierdza błędu ani ukończenia budowy i nie zmienia statusu GUNB; brak etykiety nie oznacza zweryfikowanej poprawności. Import zachowuje flagę. Formularz WWW i rozstrzyganie zgłoszeń nie są jeszcze dostępne.

Katalog app udostępnia fundament FastAPI: fabrykę ASGI, bazowy szablon Jinja2 z lokalnym CSS, informację o przygotowywanym portalu oraz sondę żywotności HTTP. Strona ofertowa, zamówienia internetowe, płatności i automatyczne doręczanie paczek pozostają kolejnymi etapami planu projektu.

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

## Uruchomienie fundamentu WWW

Po instalacji zależności uruchom serwer lokalny:

    python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000

`GET /` renderuje informację o przygotowywanym portalu, a `GET /zdrowie` zwraca `{"status":"ok","service":"gunb-web"}`. Sonda HTTP potwierdza działanie serwera WWW; nie czyta bazy ani blokad silnika. Pełną diagnostykę silnika nadal wykonuje `python main.py --zdrowie`.

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
