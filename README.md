# GUNB Lead Tool

[![tests](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml/badge.svg)](https://github.com/msm3107/GUNB-TOOL/actions/workflows/tests.yml)

Modularny backend w Pythonie, który codziennie pozyskuje **leady inwestycyjne** z rejestru
GUNB (RWDZ – pozwolenia na budowę i zgłoszenia) dla lokalnych wykonawców budowlanych:

- pobiera dane z wybranych **województw, powiatów i okresu**,
- odrzuca **szum** (ogrodzenia, zjazdy, przyłącza, sieci, rozbiórki…) i kategoryzuje inwestycje
  (`is_residential`, `is_commercial`, `is_noise` + kategoria biznesowa),
- wyciąga **inwestora** (gdy jawny) i **projektanta / pracownię** z numerem uprawnień,
- lokalizuje działki przez **ULDK (GUGiK)** → współrzędne WGS84, link do **Google Maps** i **Geoportalu**,
- dzieli leady na **segmenty klientów** (np. „domki” dla małych ekip, „duże inwestycje” dla większych
  podwykonawców) – każdy segment może trafiać na osobny czat,
- zapisuje wszystko w **SQLite** (tryb WAL) z wykrywaniem nowych spraw i **zmian statusu**,
- wysyła powiadomienia **Telegram** (HTML, przyciski inline, raporty zbiorcze zamiast spamu, limit
  1 wiadomość/s) i **Discord**, synchronizuje **Google Sheets**,
- ma **interaktywnego bota** dla wielu osób: własne filtry, 🔥 HOT/NORMAL/LOW, watchlista inwestorów
  i gmin, ⭐ zapisane leady, raport rano lub wieczorem – wszystko przyciskami.

```
🏗️ NOWY LEAD · pozwolenie na budowę
Budowa budynku mieszkalnego wielorodzinnego z lokalami usługowymi

📌 Status: decyzja – pozwolenie na budowę
🎯 Segment: Duże inwestycje / wielorodzinne
🏷️ Kategoria: mieszana · kat. XIII – pozostałe budynki mieszkalne
📍 Adres: ul. Przykładowa 15, Opole
🗺️ Lokalizacja: gm. Opole (miasto), powiat Opole
💼 Inwestor: Przykładowa Sp. z o.o.
📐 Projektant: Jan Kowalski (upr. 123/94/Op)
📦 Kubatura: 5 200 m³
📅 Daty: decyzja 2026-09-21 · wpływ 2026-08-14
🔖 Sprawa: ST-OP-OP/WNIOSEK/1234/2026

[ 📍 Otwórz w Google Maps ]      ← przyciski inline Telegrama
[ 🏛️ Geoportal             ]
```

---

## Skąd pochodzą dane

Wyszukiwarka [wyszukiwarka.gunb.gov.pl](https://wyszukiwarka.gunb.gov.pl) i jej moduł mapy są
chronione **CAPTCHA** – narzędzie **nie** automatyzuje wyszukiwarki i nie obchodzi zabezpieczeń.
Korzysta z oficjalnych plików [„Dane do pobrania”](https://wyszukiwarka.gunb.gov.pl/pobranie.html),
które GUNB aktualizuje **co noc** (ok. 23:30):

| Plik | Zawartość | Rozmiar |
|---|---|---|
| `wynik_<województwo>.zip` | Rejestr Wniosków i Decyzji (pozwolenia na budowę) od 2016 r. | 7–46 MB |
| `wynik_zgloszenia_2022_up.zip` | Rejestr Zgłoszeń dla całego kraju od 2022 r. | ~26 MB |

Ustalenia z analizy plików (uwzględnione w parserze):

- separator kolumn to `;` (strona podaje `#` – parser wykrywa separator sam), plik ma BOM UTF-8,
  kolumna `cecha` występuje dwukrotnie, pola w cudzysłowach mogą zawierać znaki nowej linii;
- **jeden wiersz = jedna działka** – sprawa może mieć setki wierszy, nie zawsze sąsiadujących;
- pozwolenia i zgłoszenia mają **różne schematy** (31 vs 26 kolumn);
- paczki zawierają **wyłącznie sprawy zakończone pozytywnie** – pozwolenia z wydaną decyzją
  i zgłoszenia ze stanem „Brak sprzeciwu” (patrz [Ograniczenia](#ograniczenia));
- urzędy wprowadzają sprawy do rejestru **z opóźnieniem** (zgłoszenia – nawet kilka tygodni),
  dlatego domyślne okno pobierania to 60 dni.

Dzięki nagłówkom `ETag`/`Last-Modified` kolejne uruchomienia pobierają paczkę tylko wtedy,
gdy GUNB ją zaktualizował (odpowiedź 304), a przerwany transfer jest wznawiany (`Range`).

---

## Architektura

```
main.py (CLI)
   └─► pipeline.LeadPipeline ──────────────────────────────────────────────┐
         │ 1. fetch                                                        │
         ├─► gunb_scraper.GunbScraper ─► http_client.ResilientHttpClient   │  GUNB: paczki ZIP
         │       (pobieranie, parsowanie CSV, filtr woj./powiat/data,      │
         │        scalanie wierszy w sprawy, paginacja)                    │
         ├─► data_filter.LeadFilter      (kategoryzacja, szum, inwestor, projektant)
         ├─► geocoding_uldk.UldkGeocoder ─► UldkClient ─► ResilientHttpClient  ULDK (GUGiK)
         ├─► storage.LeadRepository      (SQLite: investments, status_history, geocode_cache)
         │ 2. notify / 3. sync-sheets                                      │
         └─► exporter  (MessageFormatter, TelegramNotifier, DiscordNotifier,
                        GoogleSheetsExporter)                              │
```

| Moduł | Odpowiedzialność |
|---|---|
| `gunb_tool/gunb_scraper.py` | Pobieranie paczek GUNB, strumieniowe parsowanie CSV, filtrowanie po województwie, powiecie i dacie, scalanie działek w sprawy, **paginacja** wyników (`Page`, `page_size`). |
| `gunb_tool/http_client.py` | **Retry policy** (wykładniczy backoff z jitterem, `Retry-After`), **rotacja User-Agent**, **losowe opóźnienia** między zapytaniami, pobieranie warunkowe i wznawiane, maskowanie tokenów w logach. |
| `gunb_tool/data_filter.py` | Flagi `is_residential` / `is_commercial` / `is_noise`, kategoria biznesowa, **segment klientów**, ekstrakcja inwestora i projektanta (pracownia, uprawnienia, porządkowanie nazwisk). |
| `gunb_tool/geocoding_uldk.py` | ULDK: identyfikator działki → centroid WGS84, nazwy gminy/powiatu, linki Google Maps i Geoportal; ponowna próba w jednostce z kodu TERC adresu; fallback do środka obrębu; cache; bezpiecznik przy awarii usługi. |
| `gunb_tool/storage.py` | SQLite (WAL, migracje schematu): tabela `investments`, historia statusów, wykrywanie zmian, kolejka powiadomień per kanał, kolejka synchronizacji arkusza, cache geokodowania. |
| `gunb_tool/exporter.py` | Wiadomości Telegram (HTML + przyciski inline) i Discord (Markdown), raporty zbiorcze, kolejka z limitem tempa, routing segmentów, upsert do Google Sheets (`gspread`). |
| `gunb_tool/pipeline.py` | Orkiestracja etapów i raporty. |
| `gunb_tool/scoring.py` | Scoring 🔥 HOT / 🟡 NORMAL / ⚪ LOW z uzasadnieniem. |
| `gunb_tool/bot.py`, `bot_ui.py`, `bot_store.py`, `telegram_api.py` | Interaktywny bot: logika i harmonogram, teksty i przyciski, dane użytkowników (filtry, watchlista, zapisane, doręczenia), klient Bot API. |
| `gunb_tool/config.py` | `config.yaml` + zmienne środowiskowe (`${VAR}`), walidacja. |
| `gunb_tool/models.py`, `teryt.py`, `text.py` | Modele domenowe, słownik województw TERYT, normalizacja polskiego tekstu. |

---

## Instalacja

Wymagany Python **3.10+**.

```bash
git clone https://github.com/msm3107/GUNB-TOOL.git
cd GUNB-TOOL
python -m venv .venv
# Windows:  .venv\Scripts\activate      Linux/macOS:  source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # uzupełnij tokeny (plik jest w .gitignore)
```

---

## Konfiguracja

Całość ustawień jest w [`config.yaml`](config.yaml) (plik jest opisany komentarzami). Najważniejsze:

```yaml
gunb:
  sources: [pozwolenia, zgloszenia]
  voivodeships: ["12"]                 # kody TERYT lub nazwy województw
  powiats: ["1206", "1219", "1261"]    # 4-cyfrowe kody TERYT; pusta lista = całe województwo
  date_field: decyzja                  # decyzja | wplyw
  lookback_days: 60
  page_size: 200

filter:
  drop_noise: true
  drop_demolitions: true
  exclude_keywords: [ogrodzen, zjazd, przyłącz, sieć, gazow, ...]
  noise_categories: [IV, XXII, XXV, XXVI, XXIX, ...]
  include_categories: []               # np. [mieszkaniowa-jednorodzinna, komercyjna]

segments:                              # pierwszy pasujący segment wygrywa
  domki:
    label: Domki jednorodzinne
    categories: [mieszkaniowa-jednorodzinna]
    max_kubatura: 2500
    telegram_chat_id: ${TELEGRAM_CHAT_ID_DOMKI}   # puste = domyślny czat
  duze:
    label: Duże inwestycje / wielorodzinne
    categories: [mieszkaniowa-jednorodzinna, mieszkaniowa-wielorodzinna, mieszana, komercyjna, publiczna]
    telegram_chat_id: ${TELEGRAM_CHAT_ID_DUZE}

notifications:
  max_leads_per_run: 200               # leady obsłużone na kanał w jednym uruchomieniu
  digest_threshold: 10                 # > 10 leadów na czat → raport zbiorczy
  max_age_days: 14
```

- **Kody TERYT** podawaj w cudzysłowie (`"0201"`) – bez niego YAML może przeczytać `0201` jako liczbę
  ósemkową (konfiguracja to wykryje i zgłosi błąd). Kody powiatów znajdziesz w
  [rejestrze TERYT GUS](https://eteryt.stat.gov.pl) – to pierwsze 4 cyfry kodu gminy.
- **Sekrety** nie trafiają do `config.yaml`. Plik zawiera placeholdery `${TELEGRAM_BOT_TOKEN}`,
  `${DISCORD_WEBHOOK_URL}` itd., rozwijane ze zmiennych środowiskowych lub z pliku `.env`
  (wzór: [`.env.example`](.env.example)). Obsługiwana jest też postać `${ZMIENNA:-domyślna}`.
- Ścieżki względne (`data/…`) liczone są od katalogu pliku konfiguracyjnego.

---

## Użycie

```bash
python main.py --fetch                              # pobierz i przetwórz (okno z config.yaml)
python main.py --fetch --days 7 --limit 50          # szybki test konfiguracji
python main.py --fetch --powiat 1206 --since 2026-09-01 --no-geocode
python main.py --notify-telegram --dry-run          # podgląd wiadomości (nie wymaga tokenów)
python main.py --fetch --notify-telegram --notify-discord --sync-sheets
python main.py --stats
```

| Parametr | Opis |
|---|---|
| `--fetch` | Pobiera paczki GUNB, filtruje, geokoduje i zapisuje w bazie. |
| `--notify-telegram`, `--notify-discord` | Wysyła leady nowe i ze zmienionym statusem; powyżej `notifications.digest_threshold` na czat – raport zbiorczy. |
| `--sync-sheets` | Eksportuje do Google Sheets leady nowe/zmienione od ostatniej synchronizacji. |
| `--mark-sent` | Oznacza wszystkie oczekujące leady jako wysłane – bez wysyłania. |
| `--stats` | Podsumowanie bazy. |
| `--bot`, `--bot-once` | Interaktywny bot Telegram (stała praca) / jeden cykl bota. |
| `--since`, `--until`, `--days` | Zakres dat (nadpisuje `lookback_days`). |
| `--voivodeship`, `--powiat`, `--source` | Zakres danych zamiast ustawień z pliku (można powtarzać). |
| `--no-geocode`, `--limit N` | Pominięcie ULDK / przetworzenie najwyżej N spraw. |
| `--dry-run`, `--max-leads N` | Podgląd wiadomości (z czatem docelowym i przyciskami) zamiast wysyłki / limit leadów na kanał (dawniej `--max-messages`). |
| `-c/--config`, `-v/--verbose` | Plik konfiguracyjny / logi DEBUG. |

Akcje można łączyć – wykonują się w kolejności `fetch → mark-sent → notify → sync-sheets → stats`.
Kod wyjścia: `0` – sukces, `1` – część operacji się nie powiodła (szczegóły w logu), `2` – błąd konfiguracji.

**Pierwsze uruchomienie:** baza jest pusta, więc wszystkie leady z okna czasowego byłyby „nowe”.
Aby nie zalać kanału powiadomieniami, zbuduj stan początkowy bez wysyłania:

```bash
python main.py --fetch --mark-sent
```

### Harmonogram

Paczki GUNB są aktualizowane ok. 23:30, więc wystarczy jedno uruchomienie rano.

Windows (Harmonogram zadań) – podaj pełne ścieżki, bo zadanie startuje w innym katalogu:

```bat
schtasks /Create /TN "GUNB leady" /SC DAILY /ST 06:30 ^
  /TR "\"C:\GUNB-TOOL\.venv\Scripts\python.exe\" \"C:\GUNB-TOOL\main.py\" --config \"C:\GUNB-TOOL\config.yaml\" --fetch --notify-telegram --sync-sheets"
```

Linux (cron):

```cron
30 6 * * * cd /opt/GUNB-TOOL && .venv/bin/python main.py --fetch --notify-telegram --sync-sheets >> data/cron.log 2>&1
```

---

## Powiadomienia

**Telegram**

1. Utwórz bota u [@BotFather](https://t.me/BotFather) (`/newbot`) i zapisz token w `TELEGRAM_BOT_TOKEN`.
2. Dodaj bota do grupy/kanału (na kanale jako administratora) albo napisz do niego prywatnie.
3. Identyfikator czatu odczytasz z `https://api.telegram.org/bot<TOKEN>/getUpdates` (pole `chat.id`,
   dla grup/kanałów liczba ujemna, np. `-1001234567890`) → `TELEGRAM_CHAT_ID`.

Wiadomości używają `parse_mode=HTML` – escapowane są tylko `& < >`, więc nazwy firm i adresy ze
znakami specjalnymi (`Kowalski & Syn`, `ul. 3 Maja 5/7`) nie psują formatowania. Linki do Google Maps
i Geoportalu są **przyciskami inline** (każdy w osobnym, pełnym wierszu) – wygodne do kliknięcia kciukiem.

- **Kolejka z limitem tempa:** najwyżej 1 wiadomość na sekundę (`telegram.delay_seconds`, minimum 1 s);
  odstęp liczony jest od *zakończenia* poprzedniej wysyłki – zweryfikowane na API Telegrama (1,000 s).
- **Raport zbiorczy:** gdy dla czatu czeka więcej niż `notifications.digest_threshold` leadów (domyślnie
  10), zamiast serii wiadomości idzie jeden raport: podsumowanie kategorii/segmentów i zwięzła lista
  leadów z linkami, dzielona na części ≤ 4096 znaków (każdy lead dokładnie raz).
- **Segmenty na osobnych czatach:** `TELEGRAM_CHAT_ID_DOMKI` / `TELEGRAM_CHAT_ID_DUZE` (puste = domyślny
  czat) pozwalają obsługiwać dwie grupy klientów jednym botem.

**Discord** – *Ustawienia kanału → Integracje → Webhooki → Nowy webhook → Kopiuj URL* →
`DISCORD_WEBHOOK_URL`. Wiadomości nie wywołują wzmianek (`@everyone`) ani podglądów linków.

Stan wysyłki jest śledzony **osobno dla każdego kanału** – Telegram i Discord dostają ten sam lead
niezależnie. Lead trafia ponownie do kolejki, gdy zmieni się jego status.

## Bot Telegram dla ekipy (`--bot`)

Interaktywny bot dla wielu osób – każda ustawia **własne** filtry, tryb raportów i listy.
Obsługa wyłącznie przyciskami („proste jak drut”):

```
┌──────────────────┬──────────────────┐
│ 📊 Co nowego?     │ 🔎 Filtry         │   ← stałe menu na dole ekranu
│ ⭐ Zapisane       │ 👀 Obserwowane    │
│ ⏰ Kiedy wysyłać  │ 🔥 Tylko HOT      │
└──────────────────┴──────────────────┘
```

| Funkcja | Jak działa |
|---|---|
| **🔎 Filtry** | Miejsce (powiaty z listy albo wpisana miejscowość/gmina), rodzaj budynku (🏠 domy, 🏢 bloki, 🏭 hale…), minimalna kubatura (przyciski „od 10 000 m³”), inwestor (dowolny / tylko firmy / nazwa). Przykład: „Warszawa + bloki + od 10 000 m³”. |
| **🔥 HOT / 🟡 NORMAL / ⚪ LOW** | Punkty bez AI: kubatura, rodzaj budynku, kilka budynków, nowa budowa, inwestor-firma; minus za garaże, wiaty, drobne roboty i zmiany starych pozwoleń. Każdy lead pokazuje punkty i powody. **🔥 Tylko HOT** włącza tylko najlepsze. |
| **Przyciski pod leadem** | 📍 Mapa · 🏛️ Geoportal · ⭐ Zapisz · ✅ Przejrzane · 🗑️ Ukryj (z „↩️ Przywróć”) · 👀 Obserwuj inwestora · 📌 Obserwuj gminę |
| **⭐ Zapisane** | Własna lista ciekawych inwestycji (namiastka CRM), z numerami do otwarcia szczegółów. |
| **👀 Watchlista** | Nowa inwestycja obserwowanego inwestora lub w obserwowanej gminie → alert **„👀 WATCHLISTA”** od razu, niezależnie od trybu. Obserwacja „Napollo 3 Sp. z o.o.” obejmuje też „Napollo 4” (spółki celowe dewelopera). |
| **⏰ Kiedy wysyłać** | ⚡ Od razu · 🌅 Raport rano · 🌙 Raport wieczorem. W trybie „od razu” przy wielu leadach naraz przychodzi raport zamiast spamu. |
| **📊 Raport** | „📊 Raport 29.09 – Znaleziono 36 nowych inwestycji. 7 spełnia Twoje filtry. 2 to 🔥 HOT LEADY. 1 dotyczy obserwowanego inwestora lub gminy.” + lista z numerami – klik w numer otwiera lead z przyciskami. |

**Dostęp:** nowa osoba pisze do bota `/start`, a administrator (`bot.admins`, domyślnie Twój czat)
dostaje wiadomość z przyciskami **✅ Wpuść / ⛔ Odrzuć**. `bot.access: open` wpuszcza każdego.
Admin widzi listę osób komendą `/uzytkownicy`.

**Uruchomienie:** bot musi działać **stale** (odbiera kliknięcia na bieżąco) – sam pobiera dane GUNB
o `bot.fetch_times`, rozsyła alerty co `instant_every_minutes` i raporty o `morning_time` / `evening_time`:

```bash
python main.py --bot          # działa do Ctrl+C
python main.py --bot-once     # jeden cykl (odbierz wiadomości + zaległe zadania) – do testów
```

Na Windows najprościej: Harmonogram zadań → „Przy logowaniu” → `python main.py --bot` (albo usługa
przez NSSM). Komputer musi być włączony – do pracy 24/7 lepszy jest mały serwer VPS lub Raspberry Pi.
Tryb bota zastępuje `--notify-telegram` (nie uruchamiaj obu dla tego samego czatu).

## Google Sheets

1. W [Google Cloud Console](https://console.cloud.google.com) utwórz projekt i włącz **Google Sheets API**.
2. Utwórz **konto serwisowe** → *Klucze → Dodaj klucz → JSON*; zapisz plik jako `service_account.json`
   w katalogu projektu (plik jest w `.gitignore`) lub wskaż go w `GOOGLE_SERVICE_ACCOUNT_FILE`.
3. Utwórz arkusz i **udostępnij go** adresowi konta serwisowego (`…@….iam.gserviceaccount.com`) jako edytor.
4. Identyfikator arkusza (fragment URL między `/d/` a `/edit`) wpisz do `GOOGLE_SHEET_ID`.

`--sync-sheets` robi upsert po kolumnie „ID sprawy”: aktualizuje istniejące wiersze i dopisuje nowe
(stała liczba wywołań API – odczyt, aktualizacja, dopisanie – niezależnie od liczby leadów). Zakładka `sheets.worksheet` jest tworzona automatycznie.

---

## Baza danych

Plik SQLite (`storage.db_path`), tabela **`investments`** – pola ze specyfikacji:

| Kolumna | Opis |
|---|---|
| `id_sprawy` | Numer sprawy w systemie GUNB (np. `ST-OP-OP/WNIOSEK/1234/2026`) – klucz główny |
| `status` | `wniosek`, `decyzja`, `brak_sprzeciwu`, … (patrz niżej) |
| `data_aktualizacji` | Data ostatniego zdarzenia w sprawie (decyzja, a gdy jej brak – wpływ) |
| `kategoria` | Kategoria biznesowa: `mieszkaniowa-jednorodzinna`, `mieszkaniowa-wielorodzinna`, `mieszana`, `komercyjna`, `publiczna`, `rolnicza`, `inna` (`szum` – gdy `drop_noise: false`) |
| `nazwa_zamierzenia` | Opis zamierzenia budowlanego |
| `adres_opisowy` | Adres (ulica wg konwencji TERYT, numer, kod, miejscowość) |
| `teryt_dzialki` | Identyfikator pierwszej działki (`WWPPGG_R.OOOO[.AR_n].NR`) |
| `lat`, `lon` | Współrzędne WGS84 (centroid działki lub środek obrębu) |
| `google_maps_url` | Link `https://www.google.com/maps?q={lat},{lon}` (budowany zawsze ze współrzędnych) |
| `projektant` | Imię i nazwisko projektanta lub nazwa pracowni |
| `segment` | Segment klientów (`domki`, `duze`… wg `segments`) |
| `czy_wyslano` | Czy powiadomienie o bieżącym stanie leada zostało wysłane |

Dodatkowo m.in.: `zrodlo`, `status_opis`, `data_wplywu`, `data_decyzji`, `numer_decyzji`, `organ`,
`kategoria_obiektu` (I–XXX), `rodzaj_robot`, `gmina`, `powiat`, `powiat_teryt`, `dzialki` (JSON),
`precyzja_geo` (`dzialka`/`obreb`), `geoportal_url`, `inwestor`, `projektant_uprawnienia`, `pracownia`,
`kubatura`, `is_residential`, `is_commercial`, `is_noise`, `wyslano_kanaly` oraz znaczniki czasu.

Tabele pomocnicze: `status_history` (pełna historia statusów), `geocode_cache` (wyniki ULDK,
również negatywne – ponawiane po `geocoding.negative_cache_days`).

Baza działa w trybie **WAL** (`PRAGMA journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout` 5 s) –
odczyty (np. `--sync-sheets` z innego zadania) nie blokują trwającego `--fetch`. Plik bazy musi leżeć
na dysku lokalnym (WAL nie działa na udziałach sieciowych). Schemat jest wersjonowany
(`PRAGMA user_version`) i migrowany automatycznie przy starcie.

### Statusy i wykrywanie zmian

| Status | Stan w RWDZ | W paczkach GUNB |
|---|---|---|
| `wniosek` | W trakcie rozpatrywania | – |
| `decyzja` | Decyzja pozytywna | ✔ |
| `odmowa`, `umorzenie` | Decyzja odmowna / umarzająca | – |
| `wycofany`, `bez_rozpatrzenia` | Wycofany przez inwestora / bez rozpatrzenia | – |
| `zgloszenie` | Sprawa w toku (zgłoszenie) | – |
| `brak_sprzeciwu` | Brak sprzeciwu | ✔ |
| `sprzeciw` | Decyzja o sprzeciwie | – |

Przy każdym zapisie `storage` klasyfikuje zmianę:

- **NEW** – nowa sprawa → powiadomienie „🏗️ NOWY LEAD”;
- **STATUS_CHANGED** – inny status (np. `wniosek → decyzja`) → wpis w `status_history`,
  `czy_wyslano = 0` i powiadomienie „🔄 ZMIANA STATUSU: wniosek → decyzja”;
- **UPDATED** – zmiana innych pól → tylko synchronizacja arkusza;
- **UNCHANGED** – bez zmian.

---

## Filtrowanie i kategorie

Szum rozpoznawany jest po **pozycji słów w opisie** – przedmiotem inwestycji jest to, co opis wymienia
najpierw:

| Opis | Wynik |
|---|---|
| „Przyłącze gazowe do budynku mieszkalnego” | szum („przyłącz” przed „budynek”) |
| „Budowa budynku mieszkalnego wraz z przyłączami, zjazdem i ogrodzeniem” | lead mieszkaniowy |
| „Rozbiórka budynku gospodarczego” | szum (`drop_demolitions`) |
| „Rozbiórka starego budynku i budowa nowego budynku mieszkalnego” | lead |
| „Budynek mieszkalny jednorodzinny oraz rozbiórka istniejącego budynku” (rodzaj robót: budowa) | lead |
| „Przebudowa ul. Polnej” (kat. XXV) | szum (kategoria z `noise_categories`, brak słów o budynku) |

Flagi `is_residential` / `is_commercial` wynikają z kategorii obiektu (I, XIII / XIV, XVI–XVIII, XX)
i wzorców w opisie (np. „mieszkal” bez „niemieszkalny”, „hala”, „magazyn”, „usługowy”); opis ma
pierwszeństwo przed polem „rodzaj inwestycji”. Wszystkie listy słów można zmienić w `config.yaml`
(dopasowanie bez polskich znaków, wpisy mogą być wyrażeniami regularnymi).

### Segmenty klientów

Każdy lead trafia do **pierwszego pasującego** segmentu z `segments` (kategoria z listy + kubatura
w zakresie; nieznana kubatura – typowa dla zgłoszeń – nie wyklucza). Domyślnie:

| Segment | Warunek | Dla kogo |
|---|---|---|
| `domki` | dom jednorodzinny do 2500 m³ | małe ekipy, instalatorzy (np. pompy ciepła) |
| `duze` | pozostałe domy (osiedla), wielorodzinne, mieszane, komercyjne, publiczne | duzi podwykonawcy |
| — | rolnicze, inne | domyślny czat |

Próg 2500 m³ wynika z danych: w powiecie poznańskim mediana kubatury domu to 878 m³, 90% domów ma
poniżej 1540 m³, a powyżej 2500 m³ są pojedyncze „domy” będące w praktyce osiedlami.

Projektant: usuwane są tytuły i prefiksy („mgr inż. arch.”, „Projektant:”), nazwiska pisane
WIELKIMI LITERAMI są porządkowane, wpisy typu „Brak projektu” pomijane, a nazwy pracowni
(„Pracownia…”, „Biuro…”, „sp. z o.o.”, „s.c.”) rozpoznawane. Inwestor jest jawny tylko dla
podmiotów innych niż osoby fizyczne.

---

## Geokodowanie (ULDK)

- Zapytania wysyłane są z `srid=4326` – ULDK zwraca wtedy geometrię od razu w WGS84 (bez tego
  parametru odpowiada w EPSG:2180, w metrach), więc biblioteka do przeliczeń (pyproj) nie jest potrzebna.
  Prefiks `SRID=` odpowiedzi i zakres współrzędnych (Polska) są weryfikowane – geometria w innym
  układzie jest odrzucana zamiast zostać zapisana jako błędny punkt.
- `GetParcelByIdOrNr` – ULDK sam dopasowuje arkusz mapy (`…0058.52/11` → `…0058.AR_1.52/11`);
  gdy wyników jest kilka, wybierany jest ten z arkuszem zapisanym w RWDZ.
- Współrzędne to centroid geometrii WKT (ważony powierzchnią, z uwzględnieniem otworów i multipoligonów).
- Pole działki bywa nietypowe: kilka numerów (`12/1, 12/2`), pełne identyfikatory
  (`146510_8.0309.24/35, 146510_8.0309.24/36`) lub dopiski (`3/1 część`) – parser rozpoznaje wszystkie,
  zachowując kolejność, a geokoder używa pierwszej istniejącej działki.
- **Niespójna jednostka w RWDZ:** zdarza się, że wpisana jednostka ewidencyjna nie istnieje
  (`302105_5`), a działka jest zarejestrowana w jednostce z kodu TERC adresu (`302108_5`). Geokoder
  ponawia wtedy próbę w jednostce z adresu (zweryfikowane: obręby zgadzają się z miejscowościami).
  Celowo **nie** zgaduje innych typów gminy – ten sam numer działki i obrębu istnieje w sąsiednich
  gminach, co dawało błędną lokalizację.
- Starsze działki bywają podzielone – sprawdzanych jest do `max_parcels_per_case` działek sprawy,
  a gdy żadnej nie ma, używany jest środek obrębu (`precyzja_geo = obreb`).
- Identyfikator działki potwierdzony przez ULDK trafia do `teryt_dzialki`.
- Wyniki (także „nie znaleziono”) trafiają do cache; znany lead nie jest geokodowany ponownie.
- Po kilku kolejnych błędach sieci geokodowanie jest wyłączane do końca uruchomienia – awaria ULDK
  nie blokuje zapisu leadów.

## Odporność na błędy

- ponawianie błędów sieci i statusów 408/425/429/5xx z wykładniczym backoffem i jitterem,
  respektowanie `Retry-After` (nagłówek oraz pola JSON Telegrama i Discorda);
- rotacja User-Agent z puli (`http.user_agents`) i losowe odstępy między zapytaniami
  (`http.min_delay`–`max_delay`, osobno dla ULDK);
- pobieranie paczek do pliku tymczasowego, weryfikacja rozmiaru i struktury ZIP – uszkodzony transfer
  nie nadpisze poprzedniej paczki;
- zmiana formatu CSV po stronie GUNB → czytelny błąd z nazwą brakującej kolumny;
- odrzucona wiadomość nie blokuje kolejki; po kilku kolejnych błędach wysyłka jest przerywana;
- nieoczekiwany błąd geokodowania jednej sprawy nie przerywa zapisu strony (lead zapisuje się bez
  współrzędnych i dostanie je przy kolejnym uruchomieniu);
- tokeny bota i webhooka są maskowane w logach.

---

## Testy

```bash
pip install -r requirements-dev.txt
pytest
```

Testy działają bez sieci (atrapy klienta HTTP, SQLite w pamięci, atrapa arkusza) na syntetycznych
danych w formacie GUNB. CI uruchamia je na Pythonie 3.10–3.12.

Test dymny na **żywych** usługach – odpytuje ULDK o działkę testową (domyślnie Pałac Kultury i Nauki),
sprawdza układ współrzędnych i format linku Google Maps, zapisuje rekord w SQLite `:memory:`:

```bash
python sanity_check.py                        # działka 146510_8.0309.24/35
python sanity_check.py 161106_5.0058.52/11    # dowolna inna działka
```

### Wydajność (test obciążeniowy)

Powiat poznański, pełne 30 dni, oba rejestry, pierwsze uruchomienie (pusta baza i cache):

| Etap | Czas | Wynik |
|---|---|---|
| Pobranie paczki wielkopolskiej (35,4 MB) | 1,3 s | – |
| Parsowanie 519 419 wierszy CSV | 2,8 s | 212 spraw w oknie |
| Geokodowanie ULDK + zapis | ~196 s | 179/179 leadów z lokalizacją, 0 błędów sieci |
| Paczka zgłoszeń (927 tys. wierszy) | 4,8 s | 4 sprawy |

Czas pierwszego uruchomienia wyznacza celowo spowolnione geokodowanie (~0,9 s/sprawę); kolejne
uruchomienia trwają kilka sekund (paczki 304, lokalizacje z bazy).

---

## Ograniczenia

- **Brak spraw w toku.** Publiczne paczki GUNB zawierają tylko pozwolenia z decyzją i zgłoszenia bez
  sprzeciwu. Wnioski „w trakcie rozpatrywania”, odmowy i sprzeciwy są widoczne wyłącznie w wyszukiwarce
  chronionej CAPTCHA. Mechanizm zmiany statusu (pełny model 9 statusów + historia) jest gotowy, ale przy
  obecnych danych lead pojawia się od razu jako `decyzja` / `brak_sprzeciwu`.
- **Opóźnienia wpisów** – urzędy wprowadzają sprawy do rejestru z opóźnieniem, stąd szerokie okno czasowe.
- **Dane osób fizycznych** – inwestor będący osobą fizyczną nie jest publikowany; nazwa pracowni
  projektowej pojawia się w rejestrze rzadko (zwykle jest tylko imię i nazwisko projektanta).
- Paczka zgłoszeń obejmuje cały kraj (~350 MB CSV) – jej przetworzenie trwa kilka sekund dłużej.
- Sprawy pasujące do filtrów są scalane w pamięci (wiersze jednej sprawy bywają rozrzucone po pliku).
  Przy pełnym imporcie historycznym dużego województwa (np. `--since 2016-01-01` bez powiatów)
  zawęź zakres powiatami lub latami.

## Uwagi prawne

Rejestr RWDZ jest jawny, ale zawiera **dane osobowe** (m.in. imiona i nazwiska projektantów, nazwy
jednoosobowych firm). Wykorzystując je do kontaktu handlowego, zadbaj o podstawę prawną przetwarzania
(np. art. 6 ust. 1 lit. f RODO) i spełnij obowiązek informacyjny z art. 14 RODO. Plik bazy, cache
i klucze API są wyłączone z repozytorium (`.gitignore`).
