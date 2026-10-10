# Integracja e-mail — PR 4

## Cel i zakres

Kontynuacja autoryzowanego wdrożenia po merge PR #9. Baza
079559949fc9d9296dc4aca228404ca77243c4ee, gałąź codex/email-integration.
Użytkownik działającego bota dodaje własny adres, potwierdza kod i osobno zgadza się
na raporty. Telegram pozostaje osobnym kanałem z własną historią. Domyślnie e-mail
jest wyłączony; WhatsApp true nadal odrzucane. Bez migracji: schemat v14.

## Architektura i decyzje

Pozostają dwa istniejące wątki bota i ich połączenia. Addytywny wątek e-mail ma
własne repozytorium; interaktywny bot przekazuje tylko owner/endpoint do ograniczonej
kolejki weryfikacji. SMTP działa w krótkotrwałym procesie spawn bez bazy. Rodzic
nadzoruje 45 s i stop, kończy/reapuje dziecko; lease outbox 120 s. Przy niepewnym
wyniku unknown, bez automatycznej ponownej wysyłki. Potwierdzony wynik nie jest
nadpisywany przez cleanup. Budżet obejmuje oczekiwanie na proces po start; nie jest
gwarancją czasu działania systemu operacyjnego podczas tworzenia/zabijania procesu.

Alternatywa synchroniczna blokuje rozmowy lub import/Telegram na DNS/SMTP;
dodatkowy serwer/broker wymagałby nowej infrastruktury. Wybór: standardowa biblioteka,
jeden własny wątek i jeden proces SMTP naraz, istniejący worker/store i SQLite/WAL.
Nie kopiujemy połączeń SQLite do procesu ani między wątkami. Brak fork.

## Konfiguracja i prywatność

EmailConfig(enabled=False, smtp=None). Pola email: enabled, host, port (587),
tls (starttls), from_address, timeout_seconds (5). Host/adres mogą być placeholderami
SMTP_HOST/SMTP_FROM_ADDRESS; port/TLS mogą być jawne lub z env. Login i hasło wyłącznie
SMTP_USERNAME/SMTP_PASSWORD z env; wymagane dla aktywnego runtime. YAML username/password
i nieznane klucze są odrzucane również przy wyłączeniu, aby nie eksportować sekretów.
SMTPSettings zachowuje dotychczasową walidację TLS/port/timeout/adresów; ConfigError
nie zawiera wartości. Przy enabled=False nie potrzeba SMTP. Repr nie zawiera settings.

Maskowanie logów obejmuje login, hasło i adres nadawcy, także krótkie wartości.
Istniejący eksport env pomija wartości SMTP (poza whitelist); test rzeczywistego pakietu.
Eksport z enabled=True wymaga ponownego ustawienia danych SMTP przed startem na celu.
Żadne adresy GUNB nie są odbiorcami: wyłącznie własny endpoint utworzony przez użytkownika.
Kod/adres z komendy nie trafia do logu ani kolejki między wątkami. Komendy tylko private.
--bot/--bot-once z --dry-run odrzucane przed bazą/siecią, aby nie uruchomić SMTP w podglądzie.

## Rozmowa i zgody

/email: status i instrukcja. /email ustaw ADRES tworzy/zmienia jeden własny endpoint
(rano domyślnie) i kolejkuje weryfikację. /email ponow ponawia żądanie kodu.
/email potwierdz KOD zużywa token. /email zgoda zapisuje jawne potwierdzenie odbierania
raportów w tym kanale i włącza endpoint dopiero po weryfikacji. /email tryb
rano|wieczor|natychmiast zmienia harmonogram. /email wylacz cofa zgodę i anuluje kolejkę;
/email usun usuwa endpoint. Status/rezygnacja/usunięcie dostępne również po końcu
abonamentu lub podczas pauzy; ustawienie/weryfikacja/zgoda wymagają aktywnego dostępu.
Brak automatycznej zgody lub aktywacji po samym podaniu adresu/kodu. Wiadomość mówi,
że rozpoczęta wysyłka może dotrzeć po rezygnacji. Raport zawiera instrukcję wyłączenia.

Jeden endpoint zarządzany komendą; wiele zastanych endpointów wymaga operatora.
Status maskuje adres, pokazuje stan weryfikacji i zatrzymanie po unknown/failed, bez
diagnostyki SMTP. Zatrzymanego endpointu nie można reaktywować komendą zgody bez
rozstrzygnięcia wyników przez operatora. Nie usuwamy unknown/failed automatycznie.
Komunikat request jest jednakowy, niezależnie od kolejki/limitu/wyniku dostawcy.
Kolejka 25 par owner/endpoint, jedna oczekująca na ownera, ważność 60 s; bez tokenów.
Jest ulotna: po restarcie użytkownik może poprosić ponownie. Trwałe limity PR3 pozostają.

## Harmonogram i niezawodność

Osobny EmailJobs sprawdza do10 aktywnych endpointów na cykl, rotując po ID; globalnie
do200 leadów (lub mniejszy notifications.max_leads_per_run), części po20. Limit kolejki
40 części na endpoint. Na cykl jedna prośba o weryfikację i jedna próba raportu.
Polling wątku co1s, stop przed kolejną pracą. Awaria całego wątku kończy proces bota
do kontrolowanego restartu, jak dotychczasowy JobsWorker.

Rano/wieczorem: godziny BotConfig w Europe/Warsaw; najwyżej jeden ukończony przebieg
dziennie. Natychmiast: instant_every_minutes. Markery osobno w istniejącym zadania,
po zakolejkowaniu; crash przed markerem nie dubluje leadów dzięki rezerwacjom/historii
i deterministycznym kluczom części. Błąd/backpressure nie zapisuje ukończenia.
Raporty i retry poza22:00–06:00; weryfikacja na jawne żądanie również w nocy.
Od aktywacji endpointu, bez historycznego backfill; z max_age_days i user.nowe_od.
Wygaśnięcie części po24h. Dostęp, pauza, filtry, wersja, rewizje i ukrycie ponownie
sprawdzane przez store/worker przed SMTP. Telegram history nie ogranicza e-mail.

## Retencja, obsługa i wydajność

Nocna konserwacja istniejącego bota również przy email.disabled: do2000 rekordów
weryfikacji starszych7dni oraz terminalny outbox starszy30dni. Historia accepted/
delivered/read/skipped starsza max(90,max_age_days+7)dni. Unknown/failed/sending pozostają.
Retencja wymaga działającego bota; nie obiecuje usunięcia dokładnie w chwili TTL.
Osierocone markery email endpointów sprzątane, bez dotykania markerów innych zadań.
Health dla włączonego kanału: heartbeat wątku i liczba unknown/failed oraz zaległości,
bez adresów/body/tokenów. Operator sprawdza ID i wynik; brak automatycznego uznania
unknown za failed. Domena SPF/DKIM/DMARC i rzeczywista dostarczalność wymagają własnej
weryfikacji operatora przed aktywacją; nie deklarujemy zgodności prawnej/marketingowej.

PERF-01: benchmark candidates na fikcyjnych100000 wierszach (dopasowania i brak dopasowań),
bez testów z kruchym progiem czasu w CI. Zapisać wynik i granice; nie zmieniać migracji
na podstawie samego EXPLAIN. Ograniczenie cyklu i osobny wątek chronią rozmowy Telegrama;
operator mierzy także własny wolumen/filtry przed większym rollout.

## Testy i dalsze kroki

Wyłącznie fake SMTP/fake proces, tymczasowe bazy i fikcyjne adresy. Prawdziwy spawn
tylko z danymi odrzuconymi przed siecią lub bezsieciowym workerem testowym.
Konfiguracja/default/sekrety/eksport, proces deadline/stop/cleanup/ACK, ownership,
oddzielne zgody/kod, unsubscribe po access loss, status/quarantine, konkurencja,
restart/deduplikacja, report scheduling/DST/quiet/TTL/recheck/retencja/heartbeat,
izolacja rozmów podczas zatrzymanego transportu. Pełny pytest i CI3.10–3.14.
Po PR: kontrolowane uruchomienie operatora z własnym SMTP; następnie Meta/WhatsApp.
Brak merge/deploy podczas implementacji; niezależne całe review PR według lokalnego promptu.

Źródło sprawdzone2026-10-09: [Python multiprocessing](https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods).
