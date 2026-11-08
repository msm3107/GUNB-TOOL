# Żółta Tablica

[![tests](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml/badge.svg)](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml)

Żółta Tablica rozwija istniejący silnik pobierania i wzbogacania danych z rejestru RWDZ GUNB. Projekt jest przebudowywany według aktualnego [planu](projectplan.md) na niezależny silnik w katalogu core oraz aplikację B2B w katalogu app.

## Aktualny zakres

Silnik obsługuje pobieranie paczek RWDZ, filtrowanie i klasyfikację spraw, geokodowanie ULDK, zapis w SQLite oraz kontrolę stanu bazy. CLI jest dostępne w pliku main.py.

Silnik B2B korzysta ze schematu SQLite v22 dla klientów, zamówień, dostaw i zgłoszeń jakości danych. Klasyfikator rozróżnia nowe inwestycje, zmiany i pełny snapshot, a `core.export_package` zapisuje sklasyfikowane sprawy do arkusza XLSX z trzema zakładkami oraz płaskiego CSV z BOM UTF-8. Pełny snapshot zachowuje wsadowo pobraną historię statusów; eksport zawiera też scoring i jego uzasadnienie, opcjonalne szacunkowe okno branżowe, organ, lokalizację i linki mapowe. Tekst jest zapisywany jako tekst arkusza, a CSV chroni pola przed interpretacją jako formuły.

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

Ustaw `GUNB_WEB_DB_PATH` na ścieżkę istniejącej bazy WAL w aktualnym schemacie (v22), wcześniej zainicjalizowanej przez CLI. Bez tego ustawienia strona główna i liveness działają, natomiast estymator informuje o niedostępności. Serwer WWW nie tworzy i nie migruje bazy oraz nie ładuje konfiguracji silnika ani jego `.env`.

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
| `GUNB_WEB_DB_PATH` | Istniejąca baza WAL v22, zaktualizowana wcześniej przez CLI |
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

Sprzedaż pozostaje wyłączona. Nowa wersja WWW wymaga **schematu v22**:
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

## Testowe abonamenty i Customer Portal (etap PR10 / PR28)

Przed nową wersją WWW wykonaj migrację CLI do **v21** z automatyczną kopią.
Migracja zachowuje wszystkie wpisy `stripe_events`, rozszerza ich wynik o
`subscription` oraz dodaje korelację Checkout i stan abonamentów. Przebudowa
ledgera i nowe tabele są jednym krokiem transakcyjnym; błąd zachowuje v20 i dane.
WWW nadal nie migruje bazy. Rollback wymaga zgodnej kopii i uzgodnienia zdarzeń
otrzymanych po niej; nie przywracaj jej bez uwzględnienia późniejszych zmian.

Do testowego endpointu snapshot API2024-06-20 dodaj
`customer.subscription.updated` i `customer.subscription.deleted`. Obowiązują te
same limity, podpis HMAC i bramka testowa. Opłacony Checkout abonamentu zapisuje
powiązanie zamówienie→Stripe customer/subscription; **nie przypisuje właściciela
konta Stripe na podstawie emaila gościa**. Zdarzenia abonamentu wymagają zarówno
tego powiązania, zgodnych metadanych i opłaconego zamówienia, jak i istniejącego,
zaufanego `b2b_customers.stripe_customer_id`. Nieznani klienci i niepowiązane
subskrypcje są trwale pomijani. Znany klient bez jeszcze otrzymanego opłaconego
Checkout dostaje503, aby Stripe ponowił zdarzenie po uzupełnieniu korelacji.

Aktualizacja rezerwuje odświeżenie z tokenem/lease30s, pobiera **bieżący** obiekt
Subscription oficjalnym SDK poza bazą i blokadą podmiany, a następnie atomowo
zapisuje stan i event. Nie ufa staremu statusowi z dostarczonego snapshotu;
kolejność i jednakowy timestamp eventów nie wybierają wersji stanu. Timeout5s,
bez automatycznych retry; błąd daje503 bez trwałego ACK i zwalnia lease, a awaria
procesu pozwala przejąć go po30s. Podpisane usunięcie zapisuje anulowanie bez
sieci, unieważnia trwające odświeżenie i nie pozwala wskrzesić tego abonamentu.

`has_subscription_access(customer_id, now=...)` sprawdza powiązanie konta,
opłacone zamówienie, `active`, przyszły `current_period_end` i brak wstrzymania.
`past_due`, `unpaid`, `canceled`, `paused`, `trialing`, `incomplete` oraz
`incomplete_expired` odmawiają dostępu. Zaplanowane anulowanie przy `active`
zachowuje go do końca okresu. Anulowanie jednego abonamentu nie odbiera drugiego,
aktywnego. `status_subskrypcji` klienta jest projekcją informacyjną; nie wolno
używać samego tego pola zamiast kontroli czasu i powiązań. To wyłącznie stan
**testowy**, nie potwierdzenie każdego odnowienia, realnego przychodu lub dostawy.

`POST /panel/portal` przyjmuje puste body i wymaga dokładnego skonfigurowanego
Originu. Nie przyjmuje ID klienta, emaila, Stripe customer ani URL powrotu.
Sesja używa wyłącznie zaufanego powiązania zalogowanego klienta, sprawdzanego
także po wywołaniu API. URL powrotu jest stały: `/panel`. Odpowiedź wymaga
testowej sesji i HTTPS na `billing.stripe.com`; token URL nie trafia do DB/logów.
Odpowiedzi mają no-store; proxy również nie powinno logować tokenów przekierowań.
Limit portalu:4 operacje i30 prób/minutę na proces, bez sieci pod blokadą DB.
Brak tożsamości daje401, brak powiązania/niezgodny Origin403, body400, limit429,
awaria503. Domyślnie wyłączony testowy Checkout daje503 bez bazy i dostawcy.

Logowanie i potwierdzenie własności dodaje etap PR11 opisany poniżej. Fabryka
zachowuje interfejs `identity_resolver(Request) -> AuthenticatedCustomer | None`,
domyślnie używając prawdziwej kontroli sesji. Wyłączone logowanie nadal odmawia
wszystkim; nie ma zaufania do klientowskich nagłówków ani podanego ID.

Sprzedaż pozostaje wyłączona; brak nowych cen, bez prawdziwych kont/API/płatności
w testach. Retencja, monitoring, automatyczne recovery i dostawy nadal mają bramki
opisane powyżej. Weryfikacja kontraktów2026-11-02:
[Customer Portal API](https://docs.stripe.com/api/customer_portal/sessions/create?api-version=2024-06-20),
[zdarzenia abonamentu](https://docs.stripe.com/billing/subscriptions/webhooks),
[odczyt Subscription](https://docs.stripe.com/api/subscriptions/retrieve?api-version=2024-06-20).

## Testowe logowanie Magic Link (etap PR11 / PR29)

Przed nowym WWW wykonaj migrację CLI do **v22**, np. `python main.py --stats`
z właściwą konfiguracją. Kopia poprzedza DDL; migracja zachowuje dane i dodaje
hashe linków/sesji, migawkę zweryfikowanego emaila i indeksowane limity żądań.
WWW nie tworzy ani nie migruje bazy. Rollback wymaga zgodnej kopii i uzgodnienia
późniejszych zdarzeń; rotacja sekretu natychmiast unieważnia stare linki/sesje.

Domyślnie logowanie jest wyłączone: `/logowanie` pokazuje ten stan, a endpointy
logowania zwracają503 bez bazy i SMTP. Włączaj tylko na osobnej bazie testowej
i testowej skrzynce SMTP; aplikacja nie ładuje `.env` silnika.

| Zmienna procesu | Wartość |
|---|---|
| `GUNB_WEB_AUTH_MODE` | `test`, domyślnie `disabled`; brak trybu live |
| `GUNB_WEB_AUTH_SECRET` | Osobny losowy sekret 32bajty jako64 małe cyfry hex; nigdy w repo/logach |
| `GUNB_WEB_AUTH_ORIGIN` | Stały origin HTTPS; lokalne HTTP tylko localhost/127.0.0.1/::1; zgodny z Checkout, jeśli włączony |
| `GUNB_WEB_AUTH_SMTP_HOST` | Host serwera testowej poczty |
| `GUNB_WEB_AUTH_SMTP_PORT` | Port, domyślnie587 |
| `GUNB_WEB_AUTH_SMTP_FROM` | Pojedynczy adres nadawcy |
| `GUNB_WEB_AUTH_SMTP_TLS` | `starttls` albo `implicit`; brak trybu bez TLS |
| `GUNB_WEB_AUTH_SMTP_USERNAME/PASSWORD` | Opcjonalna para; zawsze razem, poza repo/logami |
| `GUNB_WEB_DB_PATH` | Istniejąca testowa baza WAL v22 |

Formularz `GET /logowanie` ustawia poufne cookie przeglądarki, a
`POST /auth/magic-link` przyjmuje wyłącznie email (JSON lub formularz). Logowanie
dotyczy istniejących klientów, nie tworzy kont. Dla poprawnego adresu odpowiedź202
jest taka sama przy koncie istniejącym, nieznanym i przekroczonym limicie; nie
gwarantuje doręczenia poczty. Limit trwały wynosi3 próby/adres/h,10/bezpośredni IP/h
i100 globalnie/h. IP pochodzi z połączenia ASGI, bez zaufania do X-Forwarded-For.
Wspólne proxy dzieli ten limit; polityka wielu instancji/proxy wymaga kontroli
przed produkcją. Niepoprawne wejście daje422, format415, body ponad4KiB413,
niewłaściwy Origin403, niedostępna baza503. Nie loguj treści formularzy.

Wysyłka następuje po HTTP ACK, poza transakcją i blokadą podmiany, przez istniejący
`core.smtp_process.SupervisedSMTP` (plan używał nieaktualnej lokalizacji klasy).
Do2 procesów SMTP naraz, budżet nadzoru15s plus ograniczone sprzątanie procesu,
bez automatycznego retry. Wynik accepted oznacza przyjęcie SMTP, nie doręczenie.
Unknown zachowuje ważność linku, bo wiadomość mogła dojść. Przeciążenie/błąd ma
zapisany status; przerwanie procesu po ACK może utracić wysyłkę — zamów nowy link.
Surowy token jest tylko w pamięci na czas wysyłki, bez trwałego outboxa.

Token ma losowy składnik256bitów, HMAC-SHA256 oddzielony celem od sesji, TTL15min
i jeden atomowy użytek. Żądanie linku odnawia ważność tego samego cookie przeglądarki,
aby świeży link miał pełne 15min działania. Jest powiązany z przeglądarką zamawiającą link, aby obca
wiadomość nie logowała ofiary na konto napastnika. **Otwórz go w tej samej
przeglądarce**; przy zmianie urządzenia, czyszczeniu cookies lub innej przeglądarce
zamów nowy. Zmiana emaila klienta unieważnia jego stare linki i sesje.

Wiadomość ma URL `/auth/verify#TOKEN`: fragment nie trafia do HTTP/access logów
ani Referer. `GET /auth/verify` nie zużywa tokenu; lokalny skrypt usuwa fragment
z historii i przygotowuje formularz wymagający przycisku „Potwierdź logowanie”.
Samo otwarcie linku przez skaner nie loguje. Bez JS można wkleić kod z fragmentu
do pola potwierdzenia. Nie umieszczaj tokenu w query, logach lub udostępnionych
zrzutach. Skrypty/style CSP pozostają same-origin; strony auth i panelu mają
Referrer-Policy:same-origin, aby natywne formularze Chromium zachowały poprawny
Origin. Pozostałe odpowiedzi nadal mają no-referrer. Origin:null jest odrzucany.

POST potwierdzenia atomowo zużywa link i tworzy nową sesję (8h absolutnego TTL),
unieważniając starą sesję tej przeglądarki. Cookie ma HttpOnly, SameSite=Lax,
Path=/ i brak Domain; HTTPS wymusza Secure i prefiks __Host-. Jedyny wyjątek
Secure to jawny lokalny test HTTP. Sesje przyjmowane są tylko z cookie i sprawdzane
w DB; surowe tokeny nie są zapisywane. `POST /auth/logout` wymaga Originu, odwołuje
sesję i usuwa oba cookies. GET nie wylogowuje. Po logowaniu dostępny jest panel
klienta; bezpieczne pobrania pozostają kolejnym etapem.

Powiązanie billingu jest osobnym, jawnym `POST /auth/billing-link` z JSON
`{"request_token":"<pierwotny UUIDv4 próby Checkout>"}`. Wymaga sesji, zgodnego
Originu, poufnego oryginalnego identyfikatora zamówienia oraz zgodnego emaila,
klienta, opłaconego zamówienia i zapisanej korelacji ze zweryfikowanego webhooka.
Hash z metadanych Stripe nie zastępuje oryginalnego UUID. Sam login lub email
gościa nigdy nie przypisuje Stripe customer. Inne istniejące powiązanie daje409
i wymaga uzgodnienia przez operatora; endpoint go nie nadpisuje.

Historyczne opłacone abonamenty z v20 nie miały jeszcze `checkout_billing_links`.
Po migracji do v22 ten sam login i oryginalny UUID pozwalają odtworzyć brakującą
korelację: aplikacja odczytuje **istniejącą** Checkout Session wskazaną lokalnym
potwierdzeniem płatności. Nie przyjmuje identyfikatora sesji od klienta. Sprawdza
tryb testowy, sesję abonamentu complete/paid, klienta/metadane/email, kwotę PLN
i niezmienną migawkę zamówienia, a przed zapisem ponownie kontroluje lokalny dowód
i właściciela. Odczyt API odbywa się poza bazą i blokadą podmiany. Odtworzenie nie
tworzy płatności, eventu płatności, zadania ani paczki; późniejszy retry korzysta
z zapisanej korelacji. Dawne wygaśnięcie opłaconej sesji nie odbiera tego prawa.
Błąd odczytu/weryfikacji daje503 bez powiązania; zmiana dowodu lub obce konto409.
Pierwsza taka operacja ma dwa odczyty SDK (sesja Checkout i bieżący abonament),
każdy z timeout5s i bez automatycznego retry; nie wymaga nowego schematu.

Po potwierdzeniu własności bieżący stan abonamentu pobierany jest poza DB przez
SDK. Lokalny audyt ma prefiks `local_reconcile_` i typ `local.subscription.reconciled`,
więc nie udaje eventu Stripe. Błąd503 pozostawia świeży dostęp wyłączony i pozwala
ponowić ten sam dowód. Limit2 uzgodnienia i10 prób/minutę na proces. Dopiero
zweryfikowana sesja oraz potwierdzony właściciel mogą wejść do testowego portalu.
Interfejs zamówień/powiązania jest dostępny w panelu (etap12); nie zgaduj i nie pobieraj
identyfikatora zamówienia innego klienta.

Przy kolejnych próbach logowania wykonywane jest ograniczone sprzątanie wygasłych
rekordów (do100/tabelę/operację; limity starsze2h, linki/sesje dzień po wygaśnięciu).
Bez ruchu mogą pozostać dłużej; harmonogram retencji/monitoring są warunkiem przed
produkcją. Wymagane są również test rzeczywistej poczty, kontrola wdrożonego TLS,
proxy, polityki nadużyć i pełnego cyklu klienta. Sprzedaż nadal jest wyłączona.

Sprawdzono2026-11-02: [OWASP tokeny](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html),
[sesje](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).
Testy są syntetyczne; nie wykonano prawdziwej wysyłki SMTP ani Stripe API.

## Panel klienta (etap 12)

Po potwierdzeniu Magic Link aplikacja przekierowuje do `/panel`. Niezalogowany
użytkownik wraca do `/logowanie`. Panel pokazuje wyłącznie zamówienia i paczki
zalogowanego klienta, po 20 pozycji ze stronicowaniem do starszych identyfikatorów;
liczniki Nowe/Zmiany/Pełna baza pochodzą z zapisanych pozycji danej paczki.
Przygotowana paczka jest wyraźnie oddzielona od doręczonej. Widok nie ujawnia
ścieżek plików ani identyfikatorów Stripe. Bezpieczne pobieranie to następny etap13.

Abonament to ostatni lokalnie potwierdzony stan, nie odczyt na żywo od dostawcy.
Wygasły okres, pauza, zakończenie, brak opłaconego zamówienia lub zaufanego
powiązania nie są prezentowane jako aktywny abonament. GET nie wykonuje Stripe,
nie migruje bazy i czyta spójny snapshot WAL, z limitem0,5s i4 równoległych odczytów
na instancję store; blokada konserwacji lub przeciążenie zwraca503. Bez nowych tabel.

Formularz `POST /panel/uwagi` przyjmuje do24KiB form-urlencoded: numer sprawy,
typ i opis do1000 znaków. Istniejący IssueTracker sprawdza doręczenie klientowi,
deduplikuje uwagi, egzekwuje20 nowych zgłoszeń/h i oznacza dane do weryfikacji.
Pochodzenie musi dokładnie zgadzać się ze skonfigurowanym origin; ID klienta
pochodzi wyłącznie z sesji. Błędy zachowują poprawnie sparsowane pola także wtedy,
gdy po uwierzytelnieniu nie można odczytać snapshotu panelu: formularz pozostaje
dostępny z escapowanym opisem, bez danych konta ze starego odczytu. Ponowienie
nadal sprawdza sesję i uprawnienia; nie tworzy duplikatu zgłoszenia. Jeśli nie
można zweryfikować sesji, formularz nie jest udostępniany. Sukces
przekierowuje303, aby odświeżenie nie wysyłało ponownie formularza.

Testowy billing ma natywny formularz `POST /panel/rozliczenia`, używający
istniejącego dowodu oryginalnego UUID Checkout i uzgodnienia Stripe (w tym v20).
Dowód nigdy nie jest odbijany w HTML/URL. Gdy konto ma zaufane powiązanie, przycisk
otwiera istniejący Customer Portal; jego stały powrót wskazuje teraz `/panel`.
Awaria lub odmowa Portalu dla natywnego żądania HTML wraca303 do panelu
z ogólnym komunikatem i możliwością ponowienia; API nadal otrzymuje JSON
z właściwym kodem błędu. Nie ujawniamy URL sesji Stripe w błędzie.
CSP wyłącznie stron panelu dopuszcza w `form-action` dokładny host
`https://billing.stripe.com`, zweryfikowany również przez PortalService, ponieważ
Chromium sprawdza także przekierowanie natywnego formularza
([MDN](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy/form-action)).
Skrypty/style i pozostałe strony zachowują dotychczasową politykę. Nie dodano JS
ani zależności. Wszystkie odpowiedzi panelu są `no-store`; sprzedaż nadal wyłączona.

## Miejscowość i promień (UX-03)

Konfigurator pozwala wyszukać nazwę i jawnie wybrać miejscowość z gminą/powiatem,
także bez JavaScript. Żaden pierwszy wynik nie jest wybierany automatycznie.
Punkt PRNG reprezentuje miejscowość, nie adres firmy. Promień1–200km nadal
przecina wybrane województwo/powiaty i pomija niepewne lokalizacje inwestycji.
Ręczne WGS84 pozostaje opcją zaawansowaną; podanie miejsca i współrzędnych
jednocześnie daje422. Usunięcie miejsca zachowuje pozostałe filtry i promień.

WWW czyta osobny lokalny katalog SQLite, wskazany przez `GUNB_WEB_PLACES_PATH`.
Brak katalogu daje503 przy wyszukiwaniu/rozwiązywaniu wyboru, a brak trafień to
pusta lista200. Ręczne współrzędne działają bez katalogu. Nie ma sieciowego
geokodera w żądaniu klienta ani katalogu demonstracyjnego udającego dane realne.

Źródło: [PRNG — Geoportal](https://www.geoportal.gov.pl/pl/dane/panstwowy-rejestr-nazw-geograficznych-prng/),
[oficjalny WFS](https://mapy.geoportal.gov.pl/wss/service/PZGiK/PRNG/WFS/GeographicalNames).
Importer obsługuje sprawdzony format **WFS2/GML3.2 M1_UrzedoweNazwyMiejscowosci**
z `srsName="urn:ogc:def:crs:EPSG::4326"` i kolejnością **latitude longitude**.
Domyślny eksport EPSG2180 jest odrzucany; należy zamówić eksport WGS84 z jawnym
`srsName` w WFS. Inne schematy GML, SHP i XLSX nie są obsługiwane przez ten importer.
Nie sklejaj stron XML ręcznie: odsyłacz `next`, niezgodna liczba rekordów,
duplikaty ID, DTD, odwrócone osie i niekompletny eksport powodują błąd.

Po pobraniu kompletnego eksportu dla zamierzonego obszaru uruchom offline:

```powershell
.venv/Scripts/python.exe -m core.places .\PRNG-WGS84.gml --output .\data\places\catalog.sqlite --source-date 2026-11-07
$env:GUNB_WEB_PLACES_PATH = (Resolve-Path .\data\places\catalog.sqlite).Path
```

Datę przykładu zastąp datą swojego eksportu; to metadana wskazana przez operatora,
nie gwarancja aktualności każdej nazwy. CLI nie pobiera danych i nie czyta konfiguracji
silnika ani bazy klientów. Nowy katalog powstaje prywatnie i jest publikowany atomowo;
wadliwy import zachowuje poprzedni plik. Nie wolno edytować opublikowanego SQLite
w miejscu — aktualizuj przez importer. Nie nadpisuje on nieznanej bazy ani symlinka.
Usługa WWW potrzebuje praw odczytu gotowego katalogu i dostępu do jego katalogu nadrzędnego.
Wersja składa się z daty i SHA256 dokładnych bajtów źródła. Wyszukiwanie korzysta
z indeksu znormalizowanej nazwy (polskie znaki), zwraca najwyżej20 rekordów.
`place_id` i `place_version` są rozwiązywane po stronie serwera; nieaktualna wersja
lub zniknięcie miejsca daje422 i wymaga ponownego wyboru, bez punktu zastępczego.

Szkicv2 zawiera dodatkowo nazwę wyszukiwania, ID i wersję katalogu; nadal czytamy
szkiceGPSv1. Nazwa bez wyboru nie uruchamia filtra miejscowości i wymaga korekty,
zamiast cicho przeliczyć większy zakres. Dane katalogu nie zmieniają pamięci dostaw.
Pełny import PRNG i wdrożenie pozostają oddzielną operacją; testy używają syntetycznych
małych eksportów. Nie dodano zależności produkcyjnych.

## Fikcyjna próbka paczki (UX-04)

`GET /przyklad` pokazuje trzy fikcyjne inwestycje; `GET /przyklad.xlsx` i
`GET /przyklad.csv` pobierają te same trzy pozycje przez istniejące generatory.
Pliki mają produkcyjne kolumny, XLSX trzy zakładki, CSV UTF-8 z BOM i przecinki.
Każdy tytuł oraz nazwa pliku wskazuje fikcyjne dane. Nie zawierają kontaktów,
rzeczywistych współrzędnych, działek ani linków mapowych. Nie są pełną bazą regionu:
to po jednym wierszu dla wariantów Nowe/Zmiany/Pełna baza, z fikcyjną historią.

Scenariusz jest stały (2026-11-01, dach); daty decyzji mieszczą się w istniejących
oknach branżowych tej daty. „Nowe” oznacza sprawę nieznaną klientowi w scenariuszu,
nie gwarantuje nowej daty wniosku. Podgląd używa tych samych modeli i helperów
oceny co plik. Baza klientów, SQL, pamięć dostaw i konfiguracja użytkownika nie są
odczytywane ani zmieniane. Dwie małe próbki są buforowane w pamięci procesu;
HTTP używa `no-store`, statycznego attachment filename i obsługuje także HEAD.
Niepowodzenie generowania zwraca bezpieczne500, nie częściowy plik; można ponowić.

Wejście jest na landing page oraz przy wyniku konfiguratora. Drugie jawnie otwiera
nową kartę, zachowując niezapisane ustawienia także bez JS i storage. Próbka nie
uruchamia sprzedaży, logowania ani wysyłki marketingowej. Pobieranie własnych,
autoryzowanych paczek klientów pozostaje osobnym etapem13.

## Testy

UX-02: konfigurator zachowuje wybrane powiaty również przy zmianie województwa,
błędzie metadanych i walidacji; do rozszerzenia na całe województwo służy jawny
przycisk czyszczenia. Wyszukiwanie powiatów nie zmienia zaznaczeń. Błędy API nadal
zawierają `detail`, a walidacja może dodawać `field_errors`. Sukces zachowuje
dotychczasowe cztery pola. Przy zero widoczny jest zakres i ograniczenie kompletności
importu; zmiana obszaru lub branży wymaga działania użytkownika.

Opcjonalny szkic zawiera wyłącznie dozwolone filtry w wersjonowanym `sessionStorage`
tej karty. Nie zawiera emaili, tokenów ani UUID Checkout. Odtworzenie przelicza wynik;
nieznana wersja/niezgodny szkic wymaga ręcznego wyboru i nie przelicza szerszych
domyślnych filtrów. Brak pamięci/skryptu nie blokuje podstawowego formularza.
Wylogowanie zgłasza zamiar wyczyszczenia szkicu; klucz jest usuwany dopiero na GET
logowania potwierdzającym brak sesji. Błąd POST logout nie usuwa pracy.
Odczyt estymatora ma timeout10s, przestarzałe odpowiedzi są odrzucane. Ponowienie po
błędzie/timeoutcie jest ręczne; brak automatycznego retry płatności lub zapisów.

Zwykłe testy uruchamiaj z `python -m pytest --ignore=tests/browser`. Osobne środowisko
przeglądarkowe (zależności wyłącznie developerskie):

    python -m pip install -c requirements-browser.lock -r requirements-browser.txt
    python -m playwright install chromium firefox
    python -m pytest -o addopts= tests/browser --browser chromium --browser firefox

CI ma osobny obowiązkowy job browser na Python3.12; instaluje przeglądarki z
`--with-deps` na Linuxie. Produkcja nadal instaluje tylko wymagania produkcyjne.
Testy uruchamiają własny serwer loopback i tymczasową syntetyczną bazę, z atrapą
poczty i przechwytywaniem żądań. Nie wykonują realnego Stripe/SMTP ani zewnętrznego
importu. Sposób uruchomienia: [Playwright Python](https://playwright.dev/python/docs/intro).

Zainstaluj zależności deweloperskie i uruchom zestaw testów:

    python -m pip install -c requirements.lock -r requirements-dev.txt
    python -m pytest -o addopts= -q

CI uruchamia testy na Pythonie 3.10–3.14.

## Struktura

- core/ — autonomiczny silnik, modele, konfiguracja, SQLite i import RWDZ.
- app/ — fundament WWW i przyszły portal B2B, bez zależności silnika od frameworka WWW.
- tests/ — testy silnika, migracji schematu, klasyfikatora dostaw, jakości danych i eksportu XLSX/CSV.
- projectplan.md — bieżący plan etapów produktu.
## Prywatne pobieranie paczek — etap 13

`POST /panel/paczki/{delivery_id}/link` (pole `format=xlsx|csv`, sesja właściciela i Origin) wydaje link na15min. `GET/HEAD /pobierz/{token}` wymaga tej samej tożsamości klienta; link można ponawiać. Wygasły link prowadzi właściciela do formularza nowego linku bez sekretu w URL formularza. Bez sesji ekran otwiera logowanie w nowej karcie; po zalogowaniu należy wrócić i ponowić. Obce ID/tokeny dają404, brak lub uszkodzenie pliku503 z retry, limit429. GET/HEAD nie potwierdza doręczenia ani odbioru przez człowieka.

Skonfiguruj `GUNB_WEB_PACKAGE_DIR` jako absolutny, prywatny katalog lokalny poza `app/static` (ten sam co katalog eksportera). Na POSIX katalog0700 i pliki0600, na Windows ACL wyłącznie dla konta aplikacji/operatora. WWW nie tworzy katalogu, nie migruje bazy i nie wykonuje backfillu. Istniejący proces migracji CLI/silnika podnosi v22→v23 z kopią sprzed DDL; przy odtwarzaniu użyj zgodnego starszego kodu i tej kopii. Nie cofaj samego `user_version` w działającej bazie.

Manifesty i osobny stan publikacji: gotowy plik nie nadaje prawa pobrania. Panel udostępnia przyciski tylko dla własnych opłaconych dostaw z manifestem oraz historycznym `doreczono_utc` albo publikacją. Nowy preparer zapisuje oba manifesty atomowo z gotową dostawą, lecz nie publikuje; integracja publikacji/ledger rewizji jest etapem15. Zakończenie abonamentu nie odbiera opłaconego archiwum. Pamięć dostaw i historyczne daty pozostają bez zmian.

Kontrolowany backfill pojedynczej istniejącej dostawy (najpierw sprawdź własność plików i katalog):

```powershell
python -m core.package_access --database C:/private/gunb.sqlite --root C:/private/packages --delivery-id 123
```

Operacja hashuje oba istniejące pliki poza DB/OS guardem i atomowo zapisuje manifesty; błąd nie zapisuje połowy zestawu. Nie regeneruje archiwum, nie publikuje, nie zmienia dat/rewizji; powtórzenie tego samego manifestu jest bezpieczne. Brak plików wymaga przywrócenia oryginalnych artefaktów, a nie eksportu z bieżącego `investments`.

Pobieranie sprawdza SHA256/rozmiar i streamuje z tego samego uchwytu, bez ponownego otwarcia ścieżki. POSIX używa `openat/O_NOFOLLOW`, Windows uchwytów [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) z blokadą zapisu/usuwania i [kontroli reparse](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getfileinformationbyhandleex) (dokumentacja sprawdzona2026-11-08). Symlinki/reparse w katalogu lub jego rodzicach, hardlinki i pliki nieregularne są odrzucane. Pliki są niezmienne: eksport tworzy nowe nazwy, operator nie edytuje ich w miejscu, szczególnie podczas pobierania.

Limity na proces:4aktywne weryfikacje/streamy,64MiB/plik,64KiB/porcję; budżet weryfikacji5s sprawdzany pomiędzy odczytami nie jest przerwaniem zawieszonego wywołania systemu plików. Wydawanie linków:10/min/klient; dostępne linki nadal działają. Disconnect/anulowanie/błąd wysyłania zwalniają uchwyt i slot. Krótka kolizja blokady metadanych może dać503; retry zachowuje uprawnienia i plik.

Audyt zapisuje hash tokenu, klienta, czas, metodę i rodzaj próby; adres bezpośredniego peer oraz UA są HMACowane kluczem procesu, bez ufania nagłówkom Forwarded. `requested` oznacza rozpoczętą próbę, `head` samą sondę. Retencja plików/grantów/audytu wymaga osobnej polityki operacyjnej przed uruchomieniem sprzedaży. Brak automatycznego kasowania w tym PR.

Uvicorn access logger ma filtr redagujący także zakodowany prefiks i query tokenu. Przy proxy zastosuj analogiczną redakcję: przykład `docs/nginx-downloads.conf.example` usuwa token, query i Referer z logu oraz wyłącza buforowanie plików. Przykład nie został wdrożony; sprawdź konfigurację aktywnego serwera i każdego dodatkowego loggera przed udostępnieniem. Sprzedaż, Stripe live i wdrożenie pozostają wyłączone.
