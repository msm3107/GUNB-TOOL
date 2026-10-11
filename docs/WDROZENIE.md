# Wdrożenie bota „Żółta Tablica” na serwer (VPS)

Serwer: **Ubuntu 24.04 LTS**, 1 vCPU, 1 GB RAM (wystarczy 512 MB), 5 GB dysku, logowanie przez SSH na
konto z `sudo` (niżej: `ubuntu@ADRES`). Bot tylko **wychodzi** do internetu (Telegram long polling, GUNB,
ULDK), więc nie potrzebuje domeny ani otwartych portów. Z zewnątrz wystarczy SSH.

Polecenia z `PS>` wpisujesz w PowerShellu na komputerze z Windows, a z `$` na serwerze. W miejsce
`WERSJA` wstaw wdrażaną wersję (tag, gałąź albo commit; po scaleniu tej pracy: `main`).

> **Jeden token = jeden działający bot.** Dwa boty na jednym tokenie dublowałyby raporty. Nowy bot
> wykrywa taki konflikt (Telegram 409) i wstrzymuje wysyłki, ale stary bot na komputerze tego nie robi.

## Układ na serwerze

| Ścieżka | Zawartość |
|---|---|
| `/opt/gunb-tool/releases/<data>-<commit>/` | kod jednej wersji z własnym `.venv` (właściciel `root`) |
| `/opt/gunb-tool/current` → wersja | działająca wersja; `previous` → poprzednia |
| `/var/lib/gunb-tool/config.yaml` | konfiguracja (zostaje przy aktualizacjach) |
| `/var/lib/gunb-tool/.env` | token i ID czatów (prawa 600, tylko konto `gunb`) |
| `/var/lib/gunb-tool/data/` | baza `gunb_leads.sqlite`, `backups/` (kopie), `eksport/` (pakiety) |

Bot działa jako usługa systemd `gunb-bot` na koncie `gunb`, które nie ma powłoki ani uprawnień
administratora. Do obsługi służy `sudo gunb-admin` (lista poleceń: `sudo gunb-admin pomoc`).

---

## A. Przeniesienie działającego bota z komputera

Kolejność: przygotowanie serwera, zatrzymanie bota na komputerze, eksport, przesłanie i sprawdzenie
pakietu, import i sekrety, start jednej instancji, sprawdzenie. Bot na komputerze działa aż do kroku A3.

### A1. Przygotuj serwer (niczego nie uruchamia, nie pobiera danych GUNB i nie wysyła wiadomości)

```bash
$ curl -fsSL https://raw.githubusercontent.com/msm3107/GUNB-TOOL/WERSJA/deploy/install.sh | sudo bash -s -- --przygotuj --wersja WERSJA
$ sudo gunb-admin wersja
```

Usługa jest zainstalowana, ale wyłączona. Dopóki nie zaimportujesz danych, `gunb-admin start` i zwykła
instalacja odmawiają startu, bo pusty bot na tym samym tokenie „zgubiłby” wszystkich użytkowników.

### A2. Narzędzie eksportu na komputerze (obok obecnej instalacji, której nie zmienia)

```powershell
PS> git clone --branch WERSJA https://github.com/msm3107/GUNB-TOOL.git C:\GUNB-przeniesienie
PS> cd C:\GUNB-przeniesienie
PS> py -3.12 -m venv .venv
PS> .venv\Scripts\python -m pip install -r requirements.txt
```

**Próba generalna (zalecana, bot na komputerze dalej działa).** Zrób spójną migawkę na żywo, a potem
wykonaj kroki A4–A5 bez `sekrety` i bez `start`:

```powershell
PS> .venv\Scripts\python -m gunb_tool.migration eksport --config C:\Users\SLC\Documents\GUNB\config.yaml --do C:\GUNB-przeniesienie\pakiet --na-zywo
```

### A3. Zatrzymaj bota na komputerze i wszystko, co zapisuje do bazy

Zamknij okno bota albo zatrzymaj zadanie lub usługę, którą go uruchamiasz, i wyłącz jej autostart
(np. `Disable-ScheduledTask -TaskName "<nazwa zadania>"` albo `nssm stop <usługa>` i `nssm set <usługa> Start SERVICE_DISABLED`).
Wyłącz też zaplanowane `--fetch` i `--notify-*`, jeśli je masz. Sprawdź, czy nic nie zostało:

```powershell
PS> Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" | Where-Object CommandLine -Match 'main.py' | Select-Object ProcessId, CommandLine
```

Wynik ma być pusty. Stara wersja bota nie zakłada blokady instancji, więc eksport nie zawsze sam wykryje,
że bot pracuje. Dlatego to sprawdzenie jest obowiązkowe.

### A4. Eksport końcowy i przesłanie

```powershell
PS> .venv\Scripts\python -m gunb_tool.migration eksport --config C:\Users\SLC\Documents\GUNB\config.yaml --do C:\GUNB-przeniesienie\pakiet
PS> Get-FileHash C:\GUNB-przeniesienie\pakiet\gunb-migracja-*.zip -Algorithm SHA256
PS> scp C:\GUNB-przeniesienie\pakiet\gunb-migracja-<czas>.zip ubuntu@ADRES:~/
```

Pakiet zawiera bazę (wszystkie inwestycje z historią, użytkownicy, filtry, testy i abonamenty,
zapisane i ukryte leady, notatki, obserwowane, przypomnienia, stan doręczeń i harmonogramu),
`config.yaml` i `.env` **bez sekretów**, a także `manifest.json` z wersjami i sumami SHA-256.
Zawiera dane osobowe, więc przesyłaj go tylko przez `scp` i usuń po imporcie.

### A5. Sprawdzenie, import i sekrety na serwerze

```bash
$ sha256sum ~/gunb-migracja-<czas>.zip          # ta sama suma co Get-FileHash
$ sudo gunb-admin sprawdz ~/gunb-migracja-<czas>.zip
$ sudo gunb-admin importuj ~/gunb-migracja-<czas>.zip
$ sudo gunb-admin sekrety                        # ten sam token co na komputerze (TELEGRAM_BOT_TOKEN z .env)
```

`sprawdz` odrzuca uszkodzony, niekompletny albo obcy pakiet i niczego nie zmienia. `importuj` wymaga
zatrzymanego bota i robi kopię istniejącej bazy. Po próbie generalnej dodaj `--zastap`, żeby zastąpić
bazę z próby. Starsza baza (np. schemat v6) podniesie się przy pierwszym starcie, a przed zmianą
schematu powstanie kopia. ID czatów i kontakt admina przychodzą z pakietu.

Konfiguracja zostaje serwerowa, czyli z nowej wersji. Jeśli `config.yaml` na komputerze był zmieniany
względem repozytorium (region, godziny), dodaj `--konfiguracja-z-pakietu`, a potem sprawdź w
`/var/lib/gunb-tool/config.yaml` wartości `backup_every_days: 1` i `backup_keep: 14`. Starsze pliki
mają kopię raz w tygodniu.

### A6. Start jednej instancji

```bash
$ sudo gunb-admin start
$ sudo gunb-admin zdrowie        # po minucie; ✘ przy „odbieraniu” = sprawdź token i sieć
```

Bot po przeniesieniu nie wysyła historii od nowa. Doręczenia, oznaczenia i dzisiejsze zadania są w
bazie, więc wyjdzie tylko to, co nowe albo zaległe z przerwy (w ciszy nocnej 22–6 nic nie wychodzi).

### A7. Sprawdzenie

1. Napisz do bota `/status` z konta administratora: wątek zadań, ostatni import, wysyłki.
2. Kliknij „📊 Inwestycje” i „⭐ Zapisane”. Zapisane leady i notatki powinny być na miejscu.
3. `sudo gunb-admin logi 200` – bez błędów i bez serii wysyłek po starcie.
4. Restart serwera: `sudo reboot`, po 2 minutach `sudo gunb-admin status` (bot działa sam).
5. Po pierwszym raporcie o zwykłej godzinie usuń pakiety: `rm ~/gunb-migracja-*.zip` na serwerze
   i `C:\GUNB-przeniesienie\pakiet\*` na komputerze.

**Powrót na komputer w pierwszych dniach:** `sudo gunb-admin stop && sudo systemctl disable gunb-bot`,
potem włącz bota na komputerze. Jego baza jest z chwili eksportu, więc zmiany z serwera przepadną,
chyba że najpierw przeniesiesz je z powrotem (`gunb-admin eksport` na serwerze, `importuj --zastap`
na komputerze).

---

## B. Nowy bot na czystym serwerze

```bash
$ curl -fsSL https://raw.githubusercontent.com/msm3107/GUNB-TOOL/WERSJA/deploy/install.sh | sudo bash -s -- --wersja WERSJA
```

Instalator pyta o token i Twój ID (podaje go @userinfobot), sprawdza połączenia, pobiera dane GUNB,
uruchamia bota, wysyła wiadomość testową na czat admina i w tle dociąga historię (do godziny).

## C. Codzienna obsługa

| Co | Polecenie |
|---|---|
| stan i kontrola zdrowia (0 OK, 1 ostrzeżenie, 2 awaria) | `sudo gunb-admin status` · `sudo gunb-admin zdrowie` |
| dziennik (na żywo / ostatnie N linii) | `sudo gunb-admin logi -f` · `sudo gunb-admin logi 300` |
| start / stop / restart | `sudo gunb-admin start` · `stop` · `restart` |
| zmiana tokenu lub ID admina | `sudo gunb-admin sekrety` |
| konfiguracja | `sudoedit /var/lib/gunb-tool/config.yaml`, potem `sudo gunb-admin restart` |

Dziennik systemd ma ograniczony rozmiar, a plik `data/gunb_tool.log` ma najwyżej 3 × 5 MB. Tokeny są
w logach zamaskowane. Awarie (np. GUNB, ULDK, baza) trafiają na czat admina, najwyżej 20 alertów na
godzinę.

## D. Aktualizacja

```bash
$ sudo gunb-admin aktualizuj WERSJA
$ sudo gunb-admin zdrowie
```

Nowa wersja powstaje obok działającej z dokładnymi wersjami zależności (`requirements.lock`). Bot
kończy bieżący krok, powstaje kopia bazy `…-przed-<wersja>-…`, przełącza się `current` i bot rusza.
Jeśli zależności się nie zainstalują albo nowa wersja nie przyjmie konfiguracji, nic się nie zmienia
i dalej działa stara wersja. Bot celowo zatrzymany zostaje zatrzymany.

### Wydanie przygotowawcze powiadomień (PR 1: v12 → v13)

Sekcje PR 1–3 poniżej opisują historyczne etapy. Bieżące uruchomienie e-mail opisuje
[PR 4](#pr-4-uruchomienie-e-mail-bez-zmiany-v14).

Wdrażaj przypięty tag lub commit wydania po scaleniu PR. Zostaw `email.enabled: false`
i `whatsapp.enabled: false`; starszy config bez tych sekcji również działa. Nie wpisuj
jeszcze danych SMTP ani tokenu Meta. W tej wersji `enabled: true` jest błędem
konfiguracji, ponieważ nadawcy będą dostępni w następnych wydaniach.

Przed aktualizacją wykonaj `sudo gunb-admin kopia` i zachowaj kopię poza serwerem
(sekcja F). Standardowe `aktualizuj WERSJA` robi własną kopię, a pierwsze otwarcie
bazy przez nowy kod dodatkowo tworzy `…-przed-v13-…`. Brak możliwości wykonania tej
kopii przerywa migrację. Krok v13 jest transakcyjny i dodaje cztery puste tabele,
zachowując konta, abonamenty, filtry, zamówienia i doręczenia Telegrama. Nie tworzy
odbiorców ani zadań nowych kanałów. Ponowny start na v13 nie powtarza migracji.

Po aktualizacji sprawdź `sudo gunb-admin zdrowie`, `sudo gunb-admin logi 100`
oraz `/status` w bocie. Potwierdź także zwykły raport Telegrama i ustawienia istniejącego
użytkownika. Na kopii bazy można sprawdzić `PRAGMA user_version` (13),
`PRAGMA integrity_check` (`ok`) i `PRAGMA foreign_key_check` (brak wierszy).
Nowe tabele `notification_endpoints`, `notification_outbox`, `notification_deliveries`
i `notification_webhook_events` powinny być puste po tej aktualizacji. Nie uruchamiaj
drugiego bota na kopii z tym samym tokenem.

**Powrót do kodu v12 wymaga kopii sprzed migracji**, zgodnie z sekcją E; samo
przełączenie kodu nie wystarczy. Odtworzenie cofa dane zapisane od czasu kopii.
PR 2 zachowuje v13, więc wycofanie go do PR 1 nie wymaga odtwarzania bazy.
PR 3 dodaje v14; dla tego wydania obowiązuje procedura odtworzenia opisana niżej.
Przed powrotem do wydania
przygotowawczego ustaw oba `enabled: false`, ponieważ odrzuca ono włączone kanały.
Stan outbox pozostaje w bazie, ale wysyłka nowych kanałów jest w nim zatrzymana.

### PR 2: raporty i trwały outbox bez uruchomienia nadawców

PR 2 zachowuje schemat v13 i procedurę aktualizacji powyżej. Dodaje wyłącznie wewnętrzne
API raportów, odbiorców, kolejki i workera; bot, CLI i harmonogram nie uruchamiają nowych
kanałów. Zostaw oba `enabled: false`. Nie dodawaj procesu workera ani danych SMTP/Meta.
Nie ma nowej migracji ani zależności. Cofnięcie kodu do PR 1 z wyłączonymi kanałami
zachowuje tabele i zapisany stan outbox bez odtwarzania bazy.

Limity wewnętrznego API: do 20 inwestycji na raport, 32 KiB treści na część i 64 KiB
JSON; domyślnie do 40 nierozliczonych części na odbiorcę (konfigurowalne w kodzie 1–100).
Kandydaci przechodzą istniejące filtry i kontrolę dostępu, z historią niezależną od Telegrama;
domyślne okno obejmuje ostatnie 30 dni i respektuje datę początku raportów użytkownika.
Raport ma termin ważności do 24 h od enqueue. Worker obsługuje domyślnie do 25 zadań
na cykl (maks. 100), z jednym aktywnym przejęciem na endpoint. Każdy wątek otwiera własne
repozytorium SQLite, a wywołanie nadawcy odbywa się poza transakcją bazy.
Publiczne operacje zapisu `NotificationStore` (odbiorca, enqueue, claim, preflight,
complete) wymagają połączenia bez zewnętrznej transakcji. Odrzucają ją przez
`RuntimeError` przed zmianami, aby błąd kolejnej części lub wpisu historii nie
pozostawił częściowego wyniku po przechwyceniu wyjątku przez wywołującego.

Nadawca musi kończyć żądanie przed upływem lease (domyślnie 120 s). `retry` jest dopuszczalne
wyłącznie przy pewności, że dostawca nie przyjął wiadomości: backoff od 60 s, podwajany
do 3600 s plus jitter 0–15 s, z uwzględnieniem dłuższego `Retry-After` do 24 h. Domyślnie
5 podejść (maks. 10). `accepted` nie oznacza doręczenia ani odczytania. Wygasłe `sending`,
timeout lub nieoczekiwany wyjątek oznacza `unknown`, bez automatycznego ponowienia.
`unknown` i trwałe `failed` wyłączają tylko dany endpoint i anulują jego pozostałe zadania;
inne endpointy i Telegram działają dalej. Późne potwierdzenie tego samego przejęcia może
zapisać faktyczny wynik, ale nie włącza odbiorcy. Zmiana adresu po rozpoczęciu wysyłki
nie może cofnąć już wysłanej wiadomości. Stabilny klucz zadania nie zapewnia idempotencji
u dostawcy, który jej nie obsługuje.

Przed uruchomieniem kanału kolejne PR muszą dodać rzeczywistą weryfikację adresu i limity
opt-in (sama poprawność formatu nie potwierdza adresu ani dostępności WhatsApp), adapter
z bezpiecznym przechowywaniem i maskowaniem sekretów, integrację harmonogramu z TTL
i ciszą nocną, obsługę statusów/webhooków oraz procedurę rozliczania `unknown`/`failed`.
Potrzebna jest też retencja treści kolejki i webhooków wraz z pomiarem na dużej historii.
W tym PR nie ma polecenia do ręcznego rozliczania awarii; samo ponowne włączenie endpointu
nie zwalnia rezerwacji inwestycji z `unknown`/`failed` i nie jest procedurą naprawczą.

Przed podłączeniem harmonogramu jego implementer musi też zmierzyć selekcję kandydatów
na dysku dla planowanej liczby inwestycji i odbiorców, z selektywnymi filtrami. Limit
20 wyników nie ogranicza obecnego skanu i sortowania SQLite. Pomiar ma rozstrzygnąć
potrzebę indeksu lub stronicowania; PR 2 nie zmienia już wydanego schematu v13.

### PR 3: SMTP i weryfikacja adresu (v13 → v14)

PR 3 dodaje adapter SMTP i wewnętrzne API `EmailVerification.request/consume`.
Bot, CLI i harmonogram nadal nie uruchamiają kanałów; zostaw oba `enabled: false`.
Nie ma jeszcze pól SMTP w config.yaml ani komend użytkownika. Nie dodawaj procesu
wysyłającego i nie wpisuj credentials w kod. Kolejny PR integracji musi dodać
konfigurację przez zmienne środowiskowe, maskowanie i eksport bez sekretów.

Aktualizuj przypięty commit/tag po scaleniu, standardowym `gunb-admin aktualizuj WERSJA`.
Zrób też `gunb-admin kopia` i zachowaj ją poza serwerem. Pierwszy start nowego kodu
tworzy `…-przed-v14-…`, a następnie transakcyjnie dodaje jedną tabelę
`email_verifications` i indeksy. Brak kopii lub błąd DDL zatrzymuje migrację;
istniejące dane Telegrama i outbox pozostają. W czystej aktualizacji nowa tabela jest pusta.
Po aktualizacji sprawdź zdrowie, logi, `/status` i raport Telegrama. Na kopii bazy:
`PRAGMA user_version` = 14, integrity_check = ok, foreign_key_check bez wyników.

**Wycofanie PR 3 do kodu PR 1–2/v13 wymaga odtworzenia kopii sprzed v14** zgodnie
z sekcją E. Samo przełączenie kodu zostanie bezpiecznie odrzucone jako nowszy schemat.
Odtworzenie cofa wszystkie dane zapisane po kopii, także zmiany bota i outbox.
Nie usuwaj ręcznie tabel ani nie obniżaj user_version. Zachowaj kopię stanu v14
przed odtwarzaniem, jak robi istniejące narzędzie odtworzenia.

Weryfikacja jest wewnętrznym API z zaufanym ownerem: request dla własnego wyłączonego
endpointu e-mail, wiadomość z jednorazowym kodem, następnie consume w tym samym
zaufanym interfejsie. Nie ma serwera HTTP ani gotowej komendy bota do tych czynności.
Kod ważny 15 minut; po 5 błędnych próbach jest unieważniany. Zmiana adresu lub wersji
endpointu, powtórne żądanie i usunięcie endpointu unieważniają możliwość użycia starego
kodu. Potwierdzenie nie zapisuje zgody ani nie włącza wysyłki. Dalsza aktywacja wymaga
osobnej, potwierdzonej zgody i istniejących kontroli dostępu.

Limity request: min. 60 s między żądaniami ownera/adresu, maks. 3/h dla ownera
i adresu oraz 100/h globalnie. Liczą się także nieprzyjęte próby wysyłki. Limity adresu
i globalne pozostają po usunięciu endpointu/konta dzięki nullable FK; digest adresu
jest metadaną wrażliwą. Rekordy starsze niż 7 dni są usuwane przy request, do 200 na
operację. To sprzątanie przy ruchu, bez gwarancji usunięcia dokładnie po 7 dniach;
integracja produkcyjna musi zapewnić planową retencję również bez ruchu.
Tokeny nie są utrwalane jawnie i nie są automatycznie ponawiane. Po crashu można
żądać nowego tokenu zgodnie z limitem. Unknown zachowuje możliwość potwierdzenia
maila, który mógł już dotrzeć; retry/failed unieważnia token. Przyszłe UI musi pokazywać
jednakowy komunikat bez ujawniania diagnostyki SMTP lub istnienia skrzynki.

SMTPSettings obsługuje wyłącznie STARTTLS lub implicit TLS z kontrolą certyfikatu
i hosta. AUTH następuje po TLS. Jeden adres koperty i To, bez Cc/Bcc. Message-ID
jest stabilnym identyfikatorem nagłówka aplikacji, nie potwierdzeniem odczytu ani
mechanizmem idempotencji SMTP. Dla nagłówka dłuższego niż 256 bajtów wynik przechowuje
jego skrót `sha256:…`; pełny Message-ID pozostaje w mailu. Wynik jest walidowany przed
połączeniem, aby metadane nie mogły nadpisać DATA 250. Tekst UTF-8 jest kodowany jako
quoted-printable, więc nie wymaga 8BITMIME. Jawne 4xx pozwala retry, 5xx oznacza failed, końcowe
DATA 250 oznacza accepted. Utrata połączenia podczas DATA oznacza unknown.
Socket timeout wynosi domyślnie 5 s (0.1–10 s); budżet 45 s jest sprawdzany między
poleceniami, bez twardego przerwania DNS/polecenia w toku. Przed podłączeniem
workera trzeba dobrać lease i nadzór czasu. Nadal wymagane są operacyjne unknown/failed,
zgody/UI/rezygnacja, harmonogram/TTL/cisza nocna, retencja i pomiar PERF-01 z PR 2.

### PR 4: uruchomienie e-mail bez zmiany v14

E-mail pozostaje domyślnie wyłączony; WhatsApp nie jest jeszcze dostępny. Ten etap
nie dodaje migracji, bibliotek, usługi systemowej ani zewnętrznego brokera kolejki.

1. Zrób kopię i zaktualizuj do przypiętego commitu po review/scaleniu. Zachowaj początkowo
   `email.enabled: false`. Dotychczasowe komendy, filtry, zamówienia i Telegram działają dalej.
2. Przygotuj konto SMTP oraz domenę nadawcy; sprawdź u dostawcy konfigurację SPF/DKIM/DMARC,
   limity, uprawnienia nadawcy i dostarczalność do własnej skrzynki. Kod i CI nie sprawdzają
   rzeczywistej konfiguracji domeny ani nie gwarantują folderu odbiorczego.
3. W środowisku usługi/jej prywatnym `.env` ustaw `SMTP_HOST`, `SMTP_FROM_ADDRESS`,
   `SMTP_USERNAME`, `SMTP_PASSWORD`. Chroń plik tak samo jak token Telegrama (prawa 600).
   Hasła i loginu nie wpisuj do YAML ani do argumentów poleceń. Sekcja `email`:

   ```yaml
   email:
     enabled: true
     host: ${SMTP_HOST:-}
     port: 587
     tls: starttls
     from_address: ${SMTP_FROM_ADDRESS:-}
     timeout_seconds: 5
   ```

   Dla implicit TLS ustaw `tls: implicit` i port dostawcy (zwykle 465). Nie ma trybu bez TLS
   ani wyłączenia weryfikacji certyfikatu. Aktywna konfiguracja wymaga obu danych logowania;
   błędy zatrzymują start przed otwarciem bazy. `notifications.max_age_days` dla e-mail: 1–365.
   Port i timeout mogą korzystać z placeholderów, np. `port: ${SMTP_PORT:-587}` oraz
   `timeout_seconds: ${SMTP_TIMEOUT:-5}`; port musi być całkowity 1–65535, timeout 0.1–10 s.
4. Zrestartuj istniejącego bota `--bot`. Nie uruchamiaj osobnego nadawcy ani drugiego bota.
   `--bot-once` także wykonuje wysyłkę; te tryby odrzucają `--dry-run`.
5. Na własnym koncie z aktywnym dostępem, w prywatnym czacie: `/email ustaw ADRES`,
   `/email potwierdz KOD`, osobno `/email zgoda`. Sprawdź rzeczywiste odebranie kodu,
   nowy pasujący rekord po aktywacji, tryby, wstrzymanie konta i `/email wylacz`.
   Kod ma 15 minut, do 5 prób; ponowienie `/email ponow` wymaga odczekania i podlega
   trwałym limitom 3/h na konto i adres, 100/h globalnie oraz odstępowi 60 s.
   Zgoda jest wersjonowana; jej wycofanie działa także po wygaśnięciu dostępu i w pauzie.
6. Sprawdź `--zdrowie`: przy włączonym e-mail bada heartbeat `email_worker` (5 min)
   i podaje liczby wysyłek zalegających/unknown/failed, bez adresów i treści.
   Pilotaż zaczynaj od małej liczby odbiorców; przed zwiększeniem skali zmierz selekcję
   na kopii danych i kontroluj CPU, czas cyklu oraz wzrost WAL.

Osobny wątek tworzy własne połączenie SQLite. Kolejka kodów mieści 25 żądań, jedno na konto,
ważne 60 s; trzyma wyłącznie ID i znika przy restarcie. Ogólny komunikat o przyjęciu zlecenia
nie potwierdza istnienia skrzynki ani wykonania SMTP. Zmiana adresu/trybu może wymagać nowego kodu.
Raporty rano/wieczorem stosują `bot.morning_time`/`evening_time` w Europe/Warsaw, a
`natychmiast` — `bot.instant_every_minutes`. Cisza nocna 22–06 dotyczy raportów, nie jawnego
żądania kodu. Nowe e-maile zaczynają od zmian po aktywacji, bez historycznego zalewu.
W jednym cyklu selekcja obejmuje do 10 odbiorców i 200 pasujących leadów
(dodatkowo `notifications.max_leads_per_run`), partie do 20 leadów, kolejkę do 40 części/odbiorcę
i TTL 24 h. Cykl wysyła najwyżej jeden raport i jeden kod; oczekiwanie między cyklami wynosi 1 s.
Brak miejsca w kolejce nie zatwierdza ukończenia harmonogramu; następny cykl kontynuuje pracę.

Pomiar PERF-01, Windows/Python 3.12, 2026-10-09: 100 tys. syntetycznych inwestycji,
5 zapytań na wariant, limit 20 wyników; p95 metodą nearest-rank jest tu najwolniejszą próbką.

| Selekcja | p50 | p95 |
|---|---:|---:|
| Pasujące rekordy | 0,1816 s | 0,1982 s |
| Powiat bez dopasowań (istniejący indeks) | 0,0001 s | 0,0003 s |
| Nazwa miejsca bez dopasowań | 8,2639 s | 8,6114 s |

Przed poprawką filtr powiatu bez dopasowań miał p50 3,7253 s / p95 5,0007 s.
SQL ogranicza teraz powiat tylko wtedy, gdy nie ma alternatywy nazwy miejsca/promienia;
zachowuje istniejące reguły OR. Nie dodano indeksu ani migracji. Filtry tekstowe i promień
nadal mogą przeglądać cały zbiór: limit 200 dotyczy **pasujących** leadów, nie liczby
czytanych wierszy. Dziesięciu odbiorców z takimi filtrami może zająć ponad minutę selekcji.
To ograniczenie pilotażu, nie SLA ani pomiar rzeczywistego serwera. Szersze wdrożenie wymaga
ponownego pomiaru i w razie potrzeby osobnego PR z kontynuowalnym skanem/indeksem.

Każda operacja SMTP działa w procesie `spawn` bez bazy. Nadzór ma budżet 45 s
obejmujący oczekiwanie na DNS/SMTP, następnie kończy proces (dwa ograniczone join po 1 s).
Lease outbox wynosi 120 s. Potwierdzone accepted nie znika wskutek błędu zamykania procesu.
Brak potwierdzenia po rozpoczęciu, crash lub przekroczenie czasu daje unknown, bez automatycznego
retry. Nie jest to gwarancja czasu przy awarii samego systemu operacyjnego. SMTP Message-ID
służy do korelacji, nie gwarantuje deduplikacji; accepted nie oznacza doręczenia ani odczytu.

**Unknown/failed:** endpoint jest automatycznie wyłączany, rezerwacje pozostają w outbox,
a `/email zgoda` nie omija tej blokady. Operator sprawdza ID zadania i potwierdzenie u dostawcy,
kontaktując się z odbiorcą zwykłym procesem obsługi. Nie kasuj zadania, nie zmieniaj stanu na
retry i nie wznawiaj w ciemno: wiadomość mogła zostać przyjęta. Ten etap nie dodaje automatycznego
uzgadniania statusów ani komendy operatora do naprawy. `/email usun` jest świadomym usunięciem
adresu i lokalnych raportów, a nie mechanizmem naprawy niepewnej wysyłki.

Nocna konserwacja istniejącego bota usuwa do 2000 rekordów na tabelę: weryfikacje starsze
niż 7 dni, zakończone outbox (accepted/delivered/read/cancelled/expired) starsze niż 30 dni,
historię doręczeń starszą niż `max(90, max_age_days+7)` dni i osierocone znaczniki harmonogramu.
Nie usuwa unknown/failed/sending. Retencja wymaga działającego bota; duży zaległy zbiór schodzi
partiami, więc nie obiecujemy usunięcia dokładnie siódmego dnia. Usunięcie adresu usuwa jego
outbox i historię, pozostawiając czasowo skróty weryfikacji/limity. Kopie zapasowe i wiadomości
już dostarczone mają własny cykl przechowywania. Pakiet migracji usuwa wartości SMTP z `.env`;
po imporcie operator ponownie uzupełnia je przed startem aktywnego kanału.

Wyłączenie/rollback: ustaw `email.enabled: false` i zrestartuj bota; wysyłka rozpoczęta może
jeszcze dotrzeć. Przy powrocie do PR 3 usuń dodatkowe klucze z sekcji `email`, zostawiając
`enabled: false` (stary walidator je ignoruje, ale nie ma integracji). Baza pozostaje v14;
nie odtwarzaj starej bazy dla samego cofnięcia PR 4. Powrót do PR 1–2/v13 nadal wymaga kopii sprzed v14.

### PR 5: adapter Meta i granica webhooka (v14 bez zmian)

To etap wewnętrzny. `whatsapp.enabled: true` wciąż jest błędem konfiguracji przed
otwarciem bazy. Nie dodawaj tokenów Meta do YAML, nie podłączaj adaptera ręcznie
do produkcyjnego workera i nie wystawiaj parsera jako samodzielnego webhooka.
Telegram i e-mail korzystają z dotychczasowych ścieżek. Brak migracji bazy,
nowej usługi, automatycznych wysyłek i zmian w eksporcie konfiguracji.

`WhatsAppSender` realizuje istniejący protokół nadawcy: jeden POST HTTPS do
`graph.facebook.com`, zweryfikowany TLS, bez proxy/netrc z otoczenia, redirectów
i retry transportu. Wersja API, ID numeru, token, szablon i język to jawne argumenty
wewnętrznego API, bez produkcyjnych kluczy config/env w tym etapie. Odpowiedź do 16 KiB.
Timeout connect/read 0.1–10 s i współpracujący budżet 30 s nie zatrzymują twardo DNS;
przyszła integracja musi dodać nadzór procesu krótszy od lease.

Szablon musi mieć dwa pozycyjne parametry BODY: tytuł oraz pełną treść snapshotu.
Adapter składa białe znaki do spacji i rozdziela linie ` | `; nie obcina danych.
Łącznie do 900 znaków w parametrach to limit aplikacji, nie deklaracja maksymalnej
pojemności Meta. Dłuższy raport daje failed bez HTTP; przyszły planner musi dopasować
raport przed zakolejkowaniem. Operator zatwierdza rzeczywisty szablon z maksymalnymi
przykładami i statycznym tekstem przed aktywacją. Przekaz powinien jasno podawać dane
GUNB, datę, inwestycje „do sprawdzenia”, niepewność etapu oraz działającą rezygnację.
Nie ma deklaracji kategorii UTILITY, bezpłatności, zgodności prawnej czy zatwierdzenia
szablonu przez Meta. Brak fallbacku do wiadomości sesyjnej i automatycznego tworzenia
szablonów. Aktualną kategorię, limity, koszty i wersję API sprawdza operator na pilotażu.

ACK API to wyłącznie accepted, po sprawdzeniu numeru kontaktu i ID wiadomości.
Timeout, 5xx, redirect, 408/409, błędna lub niespójna odpowiedź to unknown,
bez automatycznej powtórki. Potwierdzone odrzucenie 429 może dać retry z ograniczonym
Retry-After; pozostałe jednoznaczne 4xx dają failed. Istniejący worker zapisuje wynik
i izoluje endpoint; nie zmienia historii Telegrama/e-mail. Meta nie otrzymuje
nieudokumentowanego klucza idempotencji. Zgubiony ACK wymaga operacyjnego rozliczenia.

`WhatsAppWebhook` weryfikuje HMAC-SHA256 niezmienionych bajtów z oddzielnym app secret
przed JSON; verify token służy tylko challenge GET. Odrzuca dane >64 KiB, duplikaty
kluczy, niepoprawny UTF-8, niefinitywne liczby i nadmierne zagnieżdżenie. Odczytuje
do 100 statusów z własnego WABA i ID numeru. Wynik nie zawiera surowych błędów,
treści wiadomości, kontaktów ani danych rozliczeniowych. sent odpowiada accepted;
delivered/read/failed są oddzielnymi zdarzeniami. Nie zapisuje ich do bazy i nie
wykonuje poleceń z wiadomości przychodzących. Powtórzone zdarzenia w paczce są
łączone, ale podpis i hash event_key nie zastępują trwałej ochrony przed replay.

Kolejny PR musi zapewnić trwały inbox przed ACK, korelację z pierwotną wiadomością
i odbiorcą, monotoniczne statusy, retencję oraz HTTPS z limitem body przed odczytem.
Potem potrzebne są weryfikacja numeru, osobny opt-in i rezygnacja, krótszy raport,
harmonogram/tempo oraz nadzór DNS/HTTP. Testy tego etapu używają wyłącznie atrap
transportu i syntetycznej bazy; rzeczywista wysyłka i deliverability pozostają
bramką pilotażu. Powrót do kodu PR 4 zachowuje bazę i konfigurację bez zmian.

## E. Wycofanie wersji

```bash
$ sudo gunb-admin wycofaj
```

Wraca do poprzedniej wersji kodu, jeśli zna ona schemat bazy. Gdy nowa wersja zmieniła schemat,
polecenie odmawia i wypisuje dokładne kroki. Starszy kod celowo nie startuje na nowszej bazie (kod 2,
bez pętli restartów):

```bash
$ sudo gunb-admin kopie                               # kopia …-przed-<wersja>-… albo …-przed-v<N>-…
$ sudo gunb-admin stop
$ sudo gunb-admin odtworz /var/lib/gunb-tool/data/backups/<kopia>.sqlite
$ sudo gunb-admin wycofaj
$ sudo gunb-admin start
```

**Uwaga:** odtworzenie kopii cofa bazę do chwili jej wykonania. Nowe osoby, abonamenty, notatki,
zapisane leady i stan wysyłek z czasu po kopii przepadają. Zostają tylko w kopii
`…-przed-odtworzeniem-…`, którą `odtworz` robi automatycznie.

## F. Kopie zapasowe

- **Automatycznie:** codziennie o 03:30 (także przed importem, jeśli tego dnia jeszcze nie było), 14 ostatnich.
  Dodatkowo kopia przed każdą zmianą wersji i przed każdą zmianą schematu.
- **Ręcznie:** `sudo gunb-admin kopia` (spójna, także przy pracującym bocie).
- **Poza serwerem (co tydzień):** kopie na tym samym dysku nie chronią przed utratą serwera.

```bash
$ sudo gunb-admin kopia-do-pobrania                    # świeża kopia w ~/gunb-kopie, prawa 600
```
```powershell
PS> scp ubuntu@ADRES:gunb-kopie/<plik>.sqlite C:\GUNB-kopie\
```
```bash
$ rm ~/gunb-kopie/<plik>.sqlite
```

Kopie trzymaj w folderze dostępnym tylko dla Ciebie (dane osobowe).

**Próba odtworzenia (raz w miesiącu, na osobnej bazie, bez wpływu na bota):**

```powershell
PS> mkdir C:\GUNB-test-kopii; Copy-Item C:\GUNB-przeniesienie\config.yaml C:\GUNB-test-kopii\
PS> cd C:\GUNB-przeniesienie
PS> .venv\Scripts\python -m gunb_tool.migration odtworz C:\GUNB-kopie\<plik>.sqlite --config C:\GUNB-test-kopii\config.yaml
PS> .venv\Scripts\python main.py --config C:\GUNB-test-kopii\config.yaml --stats
```

## G. Monitor zewnętrzny (alarm, gdy stanie cały serwer albo proces)

Alert z samego bota nie przyjdzie, gdy bot albo serwer nie działa. Najprostszy „dead man's switch”:

1. Na healthchecks.io (plan darmowy) utwórz kontrolę: okres 5 min, tolerancja 10 min, powiadomienie
   e-mailem lub przez Telegram.
2. `sudo gunb-admin monitor https://hc-ping.com/<uuid>`: timer co 5 minut uruchamia kontrolę zdrowia
   i zgłasza wynik. Awaria idzie na `/fail`, a brak zgłoszeń (serwer lub proces stoi) wywołuje alarm
   po stronie monitora. Kontrola niczego nie restartuje.

## H. Gdy coś nie działa

| Objaw | Co zrobić |
|---|---|
| `zdrowie`: ✘ odbieranie wiadomości | `sudo gunb-admin logi 50`: `401` = zły token (`sekrety`), `409` = działa drugi bot na tym tokenie (wyłącz go), brak połączenia = sieć lub Telegram (bot ponawia sam) |
| usługa `failed`, kod 2 | błąd konfiguracji albo baza nowsza niż kod (`logi`), potem popraw config lub `aktualizuj` / `odtworz` |
| usługa `failed`, kod 3 | na tych danych działa już inny proces bota |
| ⚠ stary import GUNB | GUNB niedostępny albo zmienił format; bot ponawia co godzinę, szczegóły w `/status` i `logi` |
| mało miejsca na dysku | `sudo gunb-admin kopie` i usuń stare kopie `…-przed-…` (dzienne rotują się same) |
