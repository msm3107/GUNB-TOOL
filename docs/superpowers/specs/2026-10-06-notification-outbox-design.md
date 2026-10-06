# Raporty i trwały outbox powiadomień — PR 2

## Cel i ograniczenia

Kontynuacja zaakceptowanego planu po scaleniu PR #7. Baza: `main`,
`92a70ea1f64d7c496d59766cef6823cfae885bc2`. Zachować Python >=3.10,
SQLite/WAL, schemat v13, istniejące zależności i cały przepływ Telegrama.
Nowe moduły mają budować raporty, przechowywać kolejkę i obsługiwać jej wyniki.
Flagi e-mail/WhatsApp nadal odrzucają `true`. Nie podłączamy modułów do CLI,
harmonogramu ani UI; realne SMTP, Meta API, tokeny weryfikacyjne i webhook należą
do kolejnych PR. Testy używają fikcyjnych danych i wstrzykniętych nadawców.

## Moduły i przepływ

`notification_models.py`: niemutowalne typy odbiorcy, rewizji inwestycji, części
raportu, przejęcia zadania i wyniku nadawcy; walidowany format JSON v1.
`notification_reports.py`: ograniczony raport tekstowy z faktami GUNB.
`notification_store.py`: właściciel odbiorcy, cykl zgody/weryfikacji, selekcja
niezależna od Telegrama, atomowe dodanie raportu, przejęcie i rozliczenie zadania.
`notification_worker.py`: ograniczone `run_once`, z nadawcami przez protokół Python.

Przepływ: aktualny odbiorca → pasujące inwestycje → raport → atomowe enqueue →
claim → ponowna walidacja → wywołanie nadawcy poza transakcją → trwały wynik.
Każdy właściciel workera otwiera własny `LeadRepository` w swoim wątku.
Worker nie uruchamia własnej nieskończonej pętli ani współdzielonego połączenia.

## Odbiorca i dostęp

Operacje odbiorcy wymagają `chat_id` i `endpoint_id`; cudzy/nieistniejący odbiorca
nie może zostać odczytany ani zmieniony. Utworzenie zawsze daje wyłączony kanał.
E-mail: ASCII, pojedynczy adres, maks. 254 znaki, zachowany local-part i małe litery
domeny; bez białych znaków/sterowania. Numer: zapis `+` i 8–15 cyfr, pierwsza niezerowa.
Walidacja formatu nie dowodzi istnienia skrzynki ani dostępności WhatsApp.

`record_verification` jest wewnętrznym zapisem dowodu przez zaufany adapter,
związanym z bieżącą wersją odbiorcy. Nie jest publiczną weryfikacją adresu.
Token jednorazowy, jego wygasanie i limity musi zapewnić PR SMTP/Meta przed integracją.
`record_consent` zapisuje czas i źródło potwierdzonej zgody. Aktywacja wymaga obu
dowodów z przeszłości oraz braku cofnięcia zgody. Zmiana adresu usuwa oba dowody.
Zmiany zgody/adresu/aktywności podnoszą `version` i anulują queued/retry.
Nie przepisują wiadomości już wysyłanej. Usunięcie odbiorcy korzysta z istniejącego CASCADE.

Przy enqueue i bezpośrednio przed dispatch: kanał aktywny, zweryfikowany i ze zgodą;
konto aktywne, bez pauzy i z dostępem. Dostęp odpowiada regule bota: admin, tryb
`open`, dostęp bez limitu albo aktywny test/abonament z przyszłym poprawnym terminem.
Filtry, tylko HOT, szum, ukrycie i bieżąca rewizja są ponownie sprawdzane.
Zmiana po zakończeniu kontroli nie odwoła rozpoczętego wywołania zewnętrznego;
potwierdzenie zapisuje prawdziwy wynik oryginalnego odbiorcy i treści.

## Raport, limity i deduplikacja

Do 20 inwestycji w raporcie; domyślnie jedna część, jawne dzielenie 1–20 pozycji.
Tekst przedstawia inwestycje do sprawdzenia, status rejestrowy i datę danych.
Nie obiecuje gotowych klientów, etapu robót ani aktywnej funkcji kanału.
Źródłowe pola mają ograniczoną długość i usunięte znaki sterujące; tylko sprawdzony
HTTPS Google Maps może zostać dołączony jako mapa. Tytuł jest pojedynczą linią.
JSON ma maks. 64 KiB, tekst części 32 KiB. Brak prywatnych notatek i danych płatniczych.

Kandydaci: osobna historia `notification_deliveries`; nigdy `deliveries` Telegrama.
Strumieniowy odczyt inwestycji, filtr przed limitem, najstarsze nieobsłużone rewizje
najpierw. Zakres ogranicza `nowe_od` konta i maks. 30 dni (konfigurowalne 1–365).
To ograniczona partia kolejki; ranking całej historii Telegrama pozostaje bez zmian.
Rezerwacje wyprowadzone z JSON kolejki dla odbiorcy: queued/retry/sending/unknown/failed.
Limit domyślnie 40 części na odbiorcę, konfigurowalne 1–100; przekroczenie blokuje
enqueue tylko tego odbiorcy. Nie wymaga JSON1 ani nowej tabeli/migracji.

Enqueue całego raportu w krótkim BEGIN IMMEDIATE. Zachowany snapshot zawiera
`chat_id`, wersję odbiorcy oraz identyfikatory i rewizje pokazanych inwestycji.
Powtórka tego samego event_key zwraca pierwszy utrwalony raport bez modyfikacji.
Inny event nie może ponownie zarezerwować tej samej rewizji; konflikt nie oznacza
inwestycji jako wysłanej. Po akceptacji wszystkie pokazane rewizje i wynik części
zapisywane są w jednej transakcji. Brak akceptacji nie tworzy historii doręczeń.
Nieaktualna/uszkodzona część jest anulowana lub zatrzymana, bez zmiany jej treści.

## Przejęcie, awarie i wynik

Atomowy claim: tylko zarejestrowane kanały, queued/retry z terminem <= teraz,
losowy identyfikator przejęcia, czas przejęcia i licznik podejść. Jeden dispatch
na odbiorcę naraz. Do 100 rekordów porządkowanych w jednej operacji.
Lease domyślnie 120 s (1–3600). Nadawca musi mieć timeout krótszy od lease.
Wygasłe `sending` → `unknown`: po crashu nie wiadomo, czy dostawca przyjął wiadomość.
Brak automatycznego ponowienia. Odbiorca zostaje wyłączony, pozostałe queued/retry
anulowane; Telegram i inne endpointy działają dalej. Późne potwierdzenie tego samego
przejęcia może rozliczyć `unknown`; obcy/stary identyfikator nie zmienia zadania.

Wyniki nadawcy: accepted/delivered/read, retry, failed, unknown. `accepted` nie
znaczy odczytania. `retry` dopuszcza tylko pewność, że dostawca nie przyjął wiadomości.
Nieoczekiwany wyjątek → unknown, bez zapisywania treści wyjątku/sekretów.
Retry: 60 s × 2^(podejście−1), maks. 3600 s, jitter 0–15 s; respektować dłuższy
Retry-After (maks. 86400 s). Domyślnie 5 podejść, do 10; wyczerpanie → failed.
Permanent failure/unknown wyłącza tylko dany endpoint. Unikalny stabilny klucz
części jest przekazany nadawcy przy każdym podejściu; nie obiecujemy exactly-once.
Raport ma obowiązkowy termin ważności, nie dalej niż 24 h od enqueue.

## Weryfikacja i dalszy etap

Testy rzeczywistego SQLite, w tym konkurencyjne połączenia i ponowne otwarcie;
normalizacja i własność; nowy adres unieważnia dowody i snapshot; równoległe enqueue
i claim bez duplikacji; niezależność TG/e-mail/WA; retry i akceptacja; crash i późny ACK;
brak transakcji podczas dispatch; cofnięcie dostępu/zgody, filtry, zmiana rewizji,
upływ lease/TTL, uszkodzony JSON, bounded batch/queue i nieaktywny kanał.
Pełne pytest, CI Python 3.10–3.14 i deploy-scripts, niezależny reviewer trzech osi.

Przed uruchomieniem produkcyjnym: prawdziwa weryfikacja i limity opt-in, integracja
harmonogramu z TTL/ciszą nocną, maskowanie sekretów adapterów, obsługa webhooków
i monotonicznych statusów, operacyjne rozliczanie unknown/failed, retencja payloadów
i niepowiązanych webhooków oraz pomiar retencji na dużej historii. Ten PR nie ma
aktywnej ścieżki nadawania i nie udostępnia tych przyszłych funkcji użytkownikowi.
