# Żółta Tablica

[![tests](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml/badge.svg)](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml)

Żółta Tablica rozwija istniejący silnik pobierania i wzbogacania danych z rejestru RWDZ GUNB. Projekt jest przebudowywany według aktualnego [planu](projectplan.md) na niezależny silnik w katalogu core oraz aplikację B2B w katalogu app.

## Aktualny zakres

Silnik obsługuje pobieranie paczek RWDZ, filtrowanie i klasyfikację spraw, geokodowanie ULDK, zapis w SQLite oraz kontrolę stanu bazy. CLI jest dostępne w pliku main.py.

Silnik B2B korzysta ze schematu SQLite v20 dla klientów, zamówień, dostaw i zgłoszeń jakości danych. Klasyfikator rozróżnia nowe inwestycje, zmiany i pełny snapshot, a `core.export_package` zapisuje sklasyfikowane sprawy do arkusza XLSX z trzema zakładkami oraz płaskiego CSV z BOM UTF-8. Pełny snapshot zachowuje wsadowo pobraną historię statusów; eksport zawiera też scoring i jego uzasadnienie, opcjonalne szacunkowe okno branżowe, organ, lokalizację i linki mapowe. Tekst jest zapisywany jako tekst arkusza, a CSV chroni pola przed interpretacją jako formuły.

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

Ustaw `GUNB_WEB_DB_PATH` na ścieżkę istniejącej bazy WAL w aktualnym schemacie (v20), wcześniej zainicjalizowanej przez CLI. Bez tego ustawienia strona główna i liveness działają, natomiast estymator informuje o niedostępności. Serwer WWW nie tworzy i nie migruje bazy oraz nie ładuje konfiguracji silnika ani jego `.env`.

`GET /zamowienie` pokazuje formularz, `POST /zamowienie` zwraca wynik HTML (także bez JavaScriptu), a `POST /api/estimate` przyjmuje JSON. Przykład:

```json
{"wojewodztwo":"28","branza":"materialy","powiaty":["2814"],"kategorie":["komercyjna"]}
```

Województwo to dwucyfrowy kod TERYT; powiaty (opcjonalnie, do 20) muszą należeć do tego województwa. Branże pochodzą z `core.stages.TRADES`, typy inwestycji z `core.models.LEAD_CATEGORIES`. Materiały i stan surowy nie ograniczają dat; pozostałe branże stosują obecne szacunkowe okno etapu i mnożnik dla większych budynków. Opcjonalne `lat`, `lon`, `promien_km` podaje się łącznie: baza WGS84 w Polsce (49–55,1 N; 14–24,5 E), promień 1–200 km. Licznik promienia pomija lokalizacje gminne i znane rozbieżności działek.

Odpowiedź zawiera wyłącznie `count`, `with_parcel_coordinates`, `without_parcel_coordinates`, `last_seen`. To liczby zapisanych, niesklasyfikowanych jako szum spraw z pozytywnym lub trwającym statusem, a nie gwarancja przyszłej dostawy ani liczba NEW dla klienta. `last_seen` jest najnowszą obserwacją pasujących rekordów; nie dowodzi aktualności całego rejestru. Puste wyniki to zero, natomiast brak/niezgodność bazy, podmiana lub przekroczenie budżetu odczytu daje 503, błędne filtry 422, za duże żądanie 413, niewłaściwy format 415.

Branże z opóźnionym oknem wymagają wcześniejszego importu odpowiedniej historii (np. przez `--historical` CLI). Przy domyślnym krótkim zakresie importu mogą nie mieć wyników. Estymator sam nie rozszerza zakresu pobierania danych.

Żądania mają limit 8 KiB; zapytania SQLite budżet 500 ms i limit współbieżnych operacji. Każdy odczyt otwiera własne połączenie `mode=ro`/`query_only` pod kanoniczną blokadą podmiany bazy. Import WAL może trwać równolegle. Obecna blokada odczytów jest wyłączna, dlatego równoległy odczyt może szybko otrzymać 503 i ponowić próbę. Sam estymator nie wykonuje operacji sieciowych ani zapisów biznesowych. Selekcja używa indeksu pokrywającego `ix_investments_package` dodanego w v18. Migrację wykonuje silnik CLI (np. `python main.py --stats`, z właściwym `--config`), przed zmianą tworząc kopię przez `VACUUM INTO`. WWW odrzuca starszy schemat do czasu migracji. Indeks zwiększa rozmiar bazy i koszt aktualizacji pól selekcji; nie zmienia zapisanych spraw ani pamięci dostaw. Wycofanie kodu wymaga odtworzenia kopii zgodnej ze starszą wersją, z uwzględnieniem późniejszych importów. CORS nadal dopuszcza wyłącznie GET/HEAD dla jawnych originów; formularz i estymator działają w tym samym originie.

Pomiar syntetyczny można odtworzyć poleceniem `python -m tests.benchmark_package_estimator --runs 7`; warianty `--unique --municipality` i `--ring --municipality` sprawdzają różne geometrie, w tym punkty przy granicy promienia. Skrypt tworzy wyłącznie tymczasową bazę 100 tys. fikcyjnych spraw i mierzy pełny odczyt serwisu (blokada, nowe połączenie, zapytanie). Cel <100 ms dotyczy mierzonego scenariusza i sprzętu, nie dowolnego rozmiaru bazy. Budżet 500 ms pozostaje niezmieniony.

CORS jest domyślnie zamknięty. Jeśli osobny frontend potrzebuje dostępu, ustaw w środowisku procesu `GUNB_WEB_CORS_ORIGINS` jako listę jawnych originów oddzielonych przecinkiem, np. `https://portal.example.test,http://localhost:3000`. Wartości nie mogą zawierać wildcardów, ścieżek, danych logowania ani parametrów URL; dozwolone są obecnie wyłącznie metody GET/HEAD, bez credentials. Konfiguracja WWW nie ładuje pliku `.env` silnika.

Odpowiedzi, także błędy 500, otrzymują CSP i nagłówki ochronne. HSTS jest ustawiany tylko dla schematu HTTPS w ASGI. Przy terminowaniu TLS przez reverse proxy serwer ASGI musi ufać wyłącznie właściwym proxy; aplikacja nie interpretuje samodzielnie `X-Forwarded-Proto`. Publiczna dokumentacja API i tryb debugowania są wyłączone. Uruchomienie serwera nie uruchamia importu GUNB ani nie otwiera SQLite; silnik pozostaje niezależny od pakietów WWW.

## Testowy Stripe Checkout (etap PR8)

Sprzedaż nadal jest wyłączona, bez ustalonych cen i VAT. `POST /zamowienie/checkout` domyślnie zwraca 503 bez otwarcia bazy ani klienta Stripe. Ten etap nie dodaje przycisku zakupu. Konfiguracja dopuszcza wyłącznie `test`; klucze live i inne tryby są odrzucane. Nie uruchamiaj testów z danymi prawdziwych klientów.

Do lokalnej integracji testowej wymagane są jawne zmienne procesu (WWW nie czyta `.env` silnika):

| Zmienna | Wartość |
|---|---|
| `GUNB_WEB_CHECKOUT_MODE` | `test` (domyślnie `disabled`) |
| `GUNB_WEB_DB_PATH` | Istniejąca baza WAL v20, zaktualizowana wcześniej przez CLI |
| `GUNB_WEB_STRIPE_SECRET_KEY` | Klucz testowy `sk_test_…` albo `rk_test_…`, nigdy w repo/logach |
| `GUNB_WEB_PUBLIC_ORIGIN` | Origin HTTPS bez ścieżki, albo lokalne HTTP np. `http://127.0.0.1:8000` |
| `GUNB_WEB_STRIPE_TEST_PACKAGE_GROSZE` | Dodatnia całkowita kwota fikcyjnej paczki w PLN groszach |
| `GUNB_WEB_STRIPE_TEST_SUBSCRIPTION_GROSZE` | Dodatnia całkowita kwota fikcyjnego abonamentu w PLN groszach |
| `GUNB_WEB_STRIPE_TEST_INTERVAL` | `month` albo `year`, wyłącznie okres testowy |

Kwoty testowe nie są ofertą produkcyjną ani decyzją podatkową. Quantity wynosi 1; klient nie podaje ceny, waluty, identyfikatorów kont/zamówień ani adresów powrotu. Po jawnym włączeniu trybu testowego endpoint może tworzyć obiekty w Stripe test/sandbox oraz lokalne zamówienia `pending`; domyślnie pozostaje nieaktywny.

JSON zawiera `email` (ASCII), `kind` (`jednorazowy` lub `abonament`), losowy `request_token` UUIDv4 oraz `filters` jak w estymatorze. Żądanie wymaga nagłówka `Origin` równego skonfigurowanemu originowi; JSON ma limit 8 KiB. Po sukcesie zwracane jest 303 wyłącznie do `https://checkout.stripe.com`. Nie wysyłaj poczty ani danych prawdziwych klientów do testów. `request_token` jest poufną zdolnością ponowienia tej jednej próby: generuj go losowo, zachowaj na czas retry, nie umieszczaj w URL/logach i nigdy nie używaj go dla innego zestawu danych.

Schemat v19 dodaje `checkout_attempts` z niezmienną migawką emaila, filtrów, rodzaju paczki, ceny/okresu testowego, originu, korelacji zamówienia i czasu wygaśnięcia; zapisuje tylko skrót tokenu, nie klucz API. Migracja CLI zachowuje dane i tworzy kopię przed DDL. Każda operacja WWW otwiera własne istniejące połączenie `mode=rw` pod kanoniczną blokadą podmiany, z krótkim `BEGIN IMMEDIATE`. Sieć Stripe odbywa się po zamknięciu połączenia i zwolnieniu blokady. Kolizja blokady lub błąd SQLite/Stripe daje ogólne 503. Błędne dane dają 422, zmiana danych pod tym samym tokenem lub wygaśnięcie 409, lokalny limit prób 429. Limit wynosi 30 wywołań/minutę i 4 jednoczesne operacje na proces; nie jest to limit rozproszony ani uwierzytelnienie.

Ponowienie używa tego samego zamówienia, migawki i Stripe `Idempotency-Key`, również po nieznanym wyniku sieciowym lub nieudanym utrwaleniu odpowiedzi. Nowe wywołanie Stripe jest blokowane po 30 minutach; sesja wygasa po godzinie. Zmiana klucza API blokuje ponowne tworzenie niepewnej próby w innym koncie; zmiana ceny zachowuje pierwotną migawkę. Nie zakładaj, że nowy token anuluje poprzednią sesję: po niepewnym wyniku ponawiaj ten sam token, a po wygaśnięciu najpierw uzgodnij stan w testowym Stripe. Przed produkcją potrzebna jest też polityka retencji prób i wieloprocesowego ograniczania nadużyć.

SDK `stripe==10.12.0` jest przypięty zgodnie z zakresem zatwierdzonym w planie, API `2024-06-20`, timeout HTTP 5 s, bez automatycznych retry. Testy przechwytują HTTP rzeczywistego SDK oraz używają atrap sesji; agent nie wykonuje prawdziwego wywołania Stripe. Podstawy kontraktu: [tworzenie sesji](https://docs.stripe.com/api/checkout/sessions/create?api-version=2024-06-20), [idempotencja](https://docs.stripe.com/api/idempotent_requests), [potwierdzanie i realizacja](https://docs.stripe.com/checkout/fulfillment).

`GET /zamowienie/sukces` ignoruje parametry klienta i pokazuje wyłącznie informację o powrocie z testu. Nie oznacza zamówienia jako opłaconego, nie aktywuje abonamentu i nie generuje/doręcza paczki. Cena/VAT/warunki, cykl abonamentu, uwierzytelnienie i dostawy wymagają ukończenia przed dopuszczeniem realnej sprzedaży. Ten PR nie zawiera przełącznika live.

## Testowe webhooki i pierwsza paczka (etap PR9)

Sprzedaż pozostaje wyłączona. Nowa wersja WWW wymaga **schematu v20**:
przed jej uruchomieniem wykonaj migrację silnikiem CLI, np. `python main.py --stats`
z właściwą konfiguracją. Migracja dodaje `stripe_events` oraz `paid_package_jobs`,
robi kopię przed DDL i zachowuje wcześniejsze zamówienia/próby Checkout. WWW i
runner przygotowania otwierają wyłącznie istniejącą bazę WAL, bez migracji.
Wycofanie kodu wymaga zgodnej kopii oraz uzgodnienia zdarzeń otrzymanych po niej;
przywrócona stara kopia nie zawiera późniejszych płatności ani zadań.

`POST /api/webhook/stripe` domyślnie zwraca 503 bez SQLite/Stripe API. Oprócz
konfiguracji testowego Checkout ustaw jawnie w środowisku procesu:

| Zmienna | Wartość |
|---|---|
| `GUNB_WEB_STRIPE_WEBHOOK_SECRET` | Sekret `whsec_…` własnego testowego endpointu; nie klucz API, nie w repo/logach |
| `GUNB_WEB_TEST_PACKAGE_DIR` | Dedykowany prywatny katalog eksportów poza `app/static`, bez udostępnienia przez reverse proxy |

W Stripe skonfiguruj **snapshot**, API **2024-06-20**, zdarzenie
`checkout.session.completed` z własnego konta testowego, bez Connect.
Sekret webhooka CLI jest inny niż sekret endpointu Dashboard. Nie używaj danych
prawdziwych klientów; zachowaj osobną bazę i katalog wyłącznie dla testów.
Konfiguracja nadal nie oferuje trybu live ani publicznego zakupu.

Oficjalny SDK sprawdza `Stripe-Signature` na niezmienionym body (JSON, maks.128KiB).
Podpis wymaga czasu w granicach ±300s; utrzymuj synchronizację zegara serwera.
Odrzucane są niepoprawne/niejednoznaczne JSON-y, eventy live/Connect i inne wersje
API. Ukończona sesja musi mieć `payment_status=paid`, testowy identyfikator, poprawną
korelację metadanych/`client_reference_id`, rodzaj paczki oraz dokładną zapisaną
kwotę całkowitą PLN. Zgodne `unpaid`/`no_payment_required` i nieobsługiwane typy
testowych zdarzeń są zapisywane jako pominięte; opóźnione metody płatności nie są
włączone w Checkout. `completed` samo nie dowodzi zapłaty.

Zapis ID zdarzenia, `pending→paid` i jedno zadanie na zamówienie są jedną krótką
transakcją. Dopiero po commicie endpoint zwraca200; powtórzenie ID zwraca200 bez
ponownego zadania. Inne ID dla tej samej sesji również nie tworzy kolejnej paczki.
Brak podpisu/błędny podpis lub kształt daje400, niezgodność lokalnego zamówienia409,
nadmiar body413, format415, blokada/błąd bazy albo limit równoległości503 (ponów).
Nie utrwalamy surowego eventu, danych karty, emaila ani sekretu w `stripe_events`.
Webhook nie wywołuje Stripe API i nie wiąże Stripe customer z kontem przez email.

Po odpowiedzi HTTP uruchamiana jest próba przygotowania prywatnych XLSX/CSV.
Generator używa tego samego zakresu co estymator oraz pamięci doręczeń wszystkich
zamówień klienta. Jednorazowa paczka i pierwsza paczka abonamentu używają delty
NEW/CHANGE. Pliki są zamykane i synchronizowane przed publikacją ich nazw w DB;
generowanie odbywa się po zamknięciu połączenia SQLite i blokady podmiany.
Katalog/pliki mają na POSIX prawa700/600; na Windows skonfiguruj prywatne ACL dla
użytkownika usługi. `b2b_deliveries.doreczono_utc` pozostaje NULL: przygotowanie
nie oznacza doręczenia i nie zużywa pamięci klienta. Nie ma linku pobierania,
wysyłki email, weryfikacji właściciela emaila ani aktywacji abonamentu w tym etapie.

Zadanie pozostaje trwałe po utracie procesu po ACK. Zajęcie ma losowy token i
lease10min; wygasły wykonawca nie może opublikować wyniku po przejęciu zadania.
Uruchom odzyskiwanie ręcznie po awarii, używając **tej samej** bazy i katalogu:

    python -m core.paid_packages --database <test.sqlite> --output <private-test-dir> --limit 10

Runner nie kontaktuje się z siecią, nie zmienia płatności i nie doręcza paczek.
Przetwarza do100 zadań na wywołanie (domyślnie10); kod1 sygnalizuje niedostępność
lub nadal niedokończoną pracę, także `building` z ważnym lease. Po przerwaniu
wykonawcy odczekaj do wygaśnięcia lease i ponów recovery; aktywny lease może też
należeć do nadal działającego procesu. Monitoruj `paid_package_jobs.state/error_code`:
`prepare_failed` wymaga kontroli dysku/uprawnień/bazy, `too_large` oznacza więcej
niż10000 pasujących rekordów — zakres trzeba obsłużyć operacyjnie, bez cichego
ucięcia wyniku. Odczyt ma budżet2s, do2 generatorów i4 weryfikacji na proces;
kolizje istniejącej blokady są szybkim błędem z możliwością retry.

Nie ma jeszcze automatycznego harmonogramu odzyskiwania ani retencji. Nagłe zabicie
procesu może zostawić prywatne osierocone pliki, które operator usuwa po uzgodnieniu
z DB i wygaśnięciu lease. Nie przenoś katalogu przed rozliczeniem przygotowanych
zadań; nazwy plików są względne wobec jego skonfigurowanego położenia. Przed live
koordynator musi zapewnić: ceny/VAT/warunki, cykl abonamentu, uwierzytelnienie,
bezpieczne pobrania, dyspozytor z odzyskiwaniem/monitorowaniem i retencję. Testowe
`paid` nie może zostać uznane za realny przychód lub uprawnienie produkcyjne.

Testy używają prawdziwego SDK HMAC, syntetycznych podpisanych body i lokalnych
plików/baz; nie wykonują Stripe API, prawdziwej płatności ani sesji Excel/browser.
Kontrakt sprawdzono2026-11-02 w dokumentacji Stripe:
[webhooki](https://docs.stripe.com/webhooks),
[realizacja płatności](https://docs.stripe.com/checkout/fulfillment.md?ui=full-page),
[sesja snapshot](https://docs.stripe.com/api/checkout/sessions/object?api-version=2024-06-20).

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
