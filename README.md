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
- ma **interaktywnego bota „Żółta Tablica”** dla wielu osób: opis i przykład przed testem, prowadzenie krok
  po kroku, 7-dniowy test, własne filtry, kolejność według branży, odległości i daty, skala jako szacunek,
  obserwowani inwestorzy i gminy, ⭐ zapisane, ⏰ przypomnienia, notatki i wynik pracy, ręczne zamówienie
  z potwierdzeniem płatności, raport lejka dla admina – wszystko przyciskami.

```
🏗️ NOWA INWESTYCJA · pozwolenie na budowę
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
| `gunb_tool/notification_schema.py` | Definicja v13: odbiorcy e-mail/WhatsApp, outbox, niezależna historia doręczeń i deduplikacja webhooków. |
| `gunb_tool/notification_models.py`, `notification_reports.py` | Niemutowalne części raportu, ograniczenia rozmiaru i wersjonowany JSON kolejki. Fakty GUNB z identyfikatorami i rewizjami inwestycji. |
| `gunb_tool/notification_store.py`, `notification_worker.py` | Własność i zgody odbiorców, trwałe enqueue/claim, ponowna kontrola dostępu przed wysyłką, retry i rozliczanie wyników. Wewnętrzne API z wstrzykniętym nadawcą; bez podłączenia do harmonogramu. |
| `gunb_tool/email_sender.py` | Wewnętrzny adapter SMTP z TLS, jednym odbiorcą i rozróżnieniem odrzucenia od niepewnego wyniku DATA. |
| `gunb_tool/email_verification.py`, `email_verification_schema.py` | Wewnętrzna jednorazowa weryfikacja adresu oraz schemat v14: skróty tokenów i trwałe limity żądań. |
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
  voivodeships: ["28"]                 # kody TERYT lub nazwy województw (28 – warmińsko-mazurskie)
  powiats: []                          # pusta lista = całe województwo; np. ["2862", "2814"] = Olsztyn + powiat
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
python main.py --fetch --historical   # historia ok. 27 mies. pod przypomnienia o etapie (bez zalewu nowości)
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

## Bot Telegram „Żółta Tablica” (`--bot`)

Interaktywny bot dla wielu osób ([@ZoltaTablicaBot](https://t.me/ZoltaTablicaBot)) – każda ustawia
**własny** obszar, rodzaj budynków, branżę i godziny raportów. Obsługa wyłącznie przyciskami:

```
┌──────────────────┬──────────────────┐
│ 📊 Inwestycje     │ ⭐ Zapisane       │   ← stałe menu na dole ekranu
│ ⚙️ Ustawienia     │ ❓ Pomoc          │
└──────────────────┴──────────────────┘
```

Przyciski i komendy poprzedniego menu (`📊 Co nowego?`, `🔎 Filtry`, `/filtry`, `/blisko`, `/branza`,
`/tryb`, `/tylkohot`…) działają dalej – nikt nie musi się uczyć od nowa.

**Pierwszy kontakt (bez dostępu):** co robi bot, dla kogo, monitorowany obszar, jak zacząć (pilotaż z ręcznym
uruchomieniem) i – gdy operator ją ustawił – cena. Przyciski: 👀 przykład (wyłącznie dane fikcyjne), 🙋 prośba
o test (do admina raz), 💳 oferta, ❓ jak to działa i ograniczenia danych. Nikt nie dostaje komunikatu
o „zaległej płatności”. `/start <kod>` zapisuje źródło wejścia (np. `strona`) – [linki](docs/TELEGRAM_PROFIL.md).

**Pierwsze kroki (po zgodzie admina):** 1/2 „Co oferujesz?” (m.in. materiały budowlane; branża ustawia kolejność
i przypomnienia o etapie, nie zawęża rodzaju budynków) → 2/2 „Gdzie działasz?” → podsumowanie: ustawienia,
zakres danych, data sprawdzenia rejestru i ile inwestycji pasuje. Pusty wynik ma nazwaną przyczynę (brak danych
w bocie, miejsce spoza obszaru, wieś bez spraw, zbyt wąskie ustawienia, nieaktualny import) – wtedy zamiast
startu: zmiana obszaru lub rodzaju, kontakt albo świadome „zacznij mimo to”. Test nigdy nie startuje sam.
Po **▶️ Zacznij 7-dniowy test** – 3–5 najlepiej dopasowanych inwestycji i pełny przegląd jednym przyciskiem;
podpowiedzi (otwórz, zapisz, notatka) pojawiają się raz i nigdy po wykonaniu czynności.
Przerwaną konfigurację `/start` wznawia od tego samego kroku; dotychczasowi użytkownicy jej nie powtarzają.

| Funkcja | Jak działa |
|---|---|
| **📊 Inwestycje** | Nowe od ostatniego raportu (lista z numerami); gdy nowych brak – pełny przegląd ostatnich 30 dni stronami „◀️ Wstecz / Dalej ▶️”. Pod spodem „🕒 Rejestr GUNB sprawdzony: …”; gdy import trwa albo się nie udał, bot to mówi (z godziną ponowienia). Raport z harmonogramu przy braku nowości nie przychodzi wcale. |
| **⚙️ Ustawienia** | 🔎 Obszar i rodzaj (powiaty, miejscowość, 📍 promień od bazy w linii prostej, rodzaj budynku, kubatura, inwestor) · 🧰 Branża · 👀 Obserwowane · ⏰ Harmonogram (od razu / rano / wieczorem) · ⏸️ Wstrzymaj powiadomienia · 👤 Konto. |
| **Karta inwestycji** | Fakty z rejestru (rodzaj, miejsce, data, dokładność lokalizacji) · 🎯 dlaczego to widzisz (Twoje ustawienia) · 📐 szacunki (odległość w linii prostej, skala, okno etapu „do sprawdzenia”) · 👤 Twoja notatka. **🔽 Szczegóły** – dane urzędowe w tej samej wiadomości. Brak inwestora = „brak informacji w rejestrze”, nazwa = „wg rejestru, bez weryfikacji”. |
| **Przyciski pod inwestycją** | 📍 Mapa · 🏛️ Geoportal · ⭐ Zapisz · **⏰ Przypomnij** (7 / 14 / 30 dni, rano w wybranym dniu) · **📝 Notatka** (prywatna, do 300 znaków) · **📋 Wynik** (do sprawdzenia / sprawdzona / rozmowa / złożona oferta / niepasująca + powód – jeden na osobę i inwestycję) · 🔽 Szczegóły · **⋯ Więcej** (✅ Przejrzane, 🗑️ Ukryj z „↩️ Przywróć”, 👀 obserwuj inwestora / 📌 gminę, 👍 / 👎). |
| **🧰 Przypomnienia o etapie** | Dach 4–6 mies. po decyzji, okna i drzwi 5–7, instalacje 6–9, elewacja 8–12, wykończenia 9–14, ogrodzenie i kostka 10–18 (bloki i hale ×1,5; fundamenty – od razu). Wiadomość mówi wprost: „Warto sprawdzić tę inwestycję”, „Orientacyjne okno dla Twojej branży”, „Szacunek na podstawie daty decyzji; rzeczywisty etap wymaga sprawdzenia”. |
| **🏗️ Skala** | Duża / średnia / mała – szacunek z prostych reguł (kubatura, rodzaj, liczba budynków; w danych `hot`/`normal`/`low`). Nie szansa na zlecenie i nie wpływa na kolejność: ta wynika z okna etapu branży, odległości od bazy (pasy po 10 km) i daty. |
| **📏 Odległość** | W linii prostej od bazy do działki (ULDK); przy lokalizacji przybliżonej karta mówi „środek obrębu”. |
| **👀 Obserwowane** | Nowa inwestycja obserwowanego inwestora lub w obserwowanej gminie → alert od razu; więcej niż 3 naraz (np. po pauzie) – jedna wiadomość zbiorcza. |
| **⏸️ Pauza** | Wstrzymuje wszystkie automatyczne wiadomości: raporty, „od razu”, obserwowane, przypomnienia o etapie i „⏰ Przypomnij”. Przeglądanie działa, dostęp biegnie dalej. Informacje o końcu dostępu są transakcyjne – przychodzą także w pauzie. |

**Dostęp: 7-dniowy test, dostęp ręczny i zamówienie z ręcznym potwierdzeniem płatności:**

- Nowa osoba po `/start` dostaje opis produktu (wyżej); admin – kartę osoby ze źródłem i przyciskami
  **🎁 Test 7 dni / ✅ 30 dni / ⛔ Odrzuć**, a po „🙋 Chcę przetestować” – jedną kartę prośby o test.
- **Oferta** (`bot.offer`, wartości z `.env`: `OFERTA_…`) – cena, sposób podatku (operator wybiera: netto + VAT,
  brutto, bez VAT), płatność, sprzedawca. Niepełna = żadnej ceny ani zamówienia, tylko „💬 Zapytaj o ofertę”.
- **Zamówienie**: „🛒 Zamawiam” → numer Z-n z migawką ceny → admin potwierdza **otrzymaną** płatność
  (przycisk albo `/zaplacone Z-n`) → dostęp +30 dni od końca obecnego, dokładnie raz. Dostęp z `/aktywuj` to
  dostęp ręczny – nigdy nie jest nazywany płatnością. Instrukcja: [docs/PILOTAZ.md](docs/PILOTAZ.md).
- **Test 7 dni**: admin tylko pozwala; zegar rusza, gdy osoba po konfiguracji kliknie **▶️ Zacznij** –
  dokładnie 7 × 24 h, z datą i godziną końca. Jeden test na konto Telegram: `/start`, restart, zmiana
  filtrów ani odblokowanie bota go nie odnawiają.
- Uprawnienia sprawdza jedno miejsce dla komend, przycisków i zadań w tle: konto i pomoc – zawsze;
  ustawienia – także, gdy test czeka na start; inwestycje – tylko z dostępem. Przycisk autoryzuje osoba,
  która go kliknęła (`from.id`).
- Po końcu dostępu raporty, alerty i przypomnienia stają; ⭐ Zapisane, notatki i wyniki pracy są do wglądu
  (karta „archiwum” tylko dla własnej pracy), stare przyciski innych inwestycji nie pokazują nic nowego.
  Dzień przed końcem – jedno przypomnienie (w teście z podsumowaniem rzeczywistych działań i ofertą), po końcu –
  jedna informacja z ofertą (obie odporne na restart, nigdy w nocy); admin dostaje listę do przedłużenia.
- 48 h po starcie testu bez aktywacji – jedna podpowiedź (nie w nocy, nie w pauzie, wyłączalna w ustawieniach).

| Komenda admina | Działanie |
|---|---|
| `/aktywuj <chat_id> <dni\|RRRR-MM-DD\|DD.MM.RRRR>` | abonament na N dni (od końca trwającego dostępu) albo do końca podanego dnia (czas polski) |
| `/przedluz <chat_id> <dni\|data>` | to samo – czytelniej przy przedłużaniu |
| `/odbierz <chat_id>` | wyłącza dostęp od razu (dane klienta zostają) |
| `/trial <chat_id>` | pozwala na 7-dniowy test (raz na osobę) |
| `/nowymodel <chat_id\|wszyscy> <dni\|data>` | dotychczasowym użytkownikom (dostęp bez terminu sprzed abonamentów) ustawia termin |
| `/uzytkownicy` | lista osób ze stanem dostępu |
| `/status` | import GUNB (trwa / ok / błąd + treść, ponowienie), ostatni pełny import, wątek zadań, wysyłki z ostatniej doby |
| `/raport [7\|30\|90]` | lejek pilotażu: źródła, kroki przed testem, wyniki i puste wyniki, otwarcia (osoby i zdarzenia), zapisane, wyniki pracy, dostęp ręczny osobno od płatności, kohorty testu z zakończoną obserwacją (wysłanie ≠ przeczytanie; kliknięć w mapę Telegram nie zgłasza) |
| `/zamowienia` · `/zaplacone <Z-nr> [uwagi]` · `/anuluj <Z-nr>` | zamówienia: lista, potwierdzenie otrzymanej płatności (raz), anulowanie |
| `/wplata <chat_id> [uwagi]` | płatność bez „🛒 Zamawiam” (np. po rozmowie): potwierdza otwarte zamówienie albo zakłada je z bieżącej oferty – liczy się jako płatność, nie dostęp ręczny |
| `/przedluztest <chat_id> <1–14> <powód>` | jednorazowe przedłużenie testu z zapisem powodu |
| `/napisz <chat_id> <tekst>` · `/firma <chat_id> <nazwa\|->` | wiadomość przez bota (np. dane do przelewu) · firma osoby (raport liczy firmy) |
| `/dane` | diagnostyka danych: nazwy obszarów przy kodach, sprawy spoza powiatów, dokładność lokalizacji, braki, historia do okien etapów, brakujące pola oferty |

`bot.access: open` wyłącza paywall (np. darmowy pilotaż).

Strona sprzedażowa do lokalnego podglądu (statyczna, bez skryptów i formularzy, z polami „DO UZUPEŁNIENIA”) oraz
szkice zasad usługi i prywatności: [`strona/`](strona/index.html). Teksty profilu bota: [docs/TELEGRAM_PROFIL.md](docs/TELEGRAM_PROFIL.md).
Odłożone pomysły z warunkami powrotu: [docs/BACKLOG.md](docs/BACKLOG.md).

**Jak działa w środku:** proces ma dwa wątki. Główny tylko odbiera wiadomości i kliknięcia (krótkie
limity czekania na Telegram – odpowiedź nie wisi minutami). Wątek zadań – z własnym połączeniem SQLite
i klientami HTTP – o `bot.fetch_times` pobiera dane GUNB, o `morning_time` / `evening_time` rusza raporty
(godziny w czasie polskim, także po zmianie czasu; w bazie UTC), co `instant_every_minutes` wysyła alerty.
Raporty i przypomnienia idą przez kolejkę `wysylki`: najpierw lista odbiorców, potem każda osoba osobno –
błąd jednej nie zatrzymuje innych, nieudana wysyłka wraca po 1, 5, 15 i 30 min (najwyżej 5 prób, potem
alert do admina), restart nie gubi ani nie dubluje obsłużonych. Timeout Telegrama bywa niejednoznaczny
(wiadomość mogła dojść) – wtedy bot woli powtórzyć raport niż go zgubić, więc „dokładnie raz” nie jest
gwarantowane. W nocy (22–6) nic automatycznego nie wychodzi. Naraz działa jeden import (blokada w bazie
wspólna dla bota, `--fetch` z crona i importu historii). `systemctl stop` / Ctrl+C kończą import między
stronami; dokończy się zaraz po starcie.

```bash
python main.py --bot          # działa do Ctrl+C / SIGTERM
python main.py --bot-once     # jeden cykl (odbierz wiadomości + zaległe zadania) – do testów
```

Na Windows najprościej: Harmonogram zadań → „Przy logowaniu” → `python main.py --bot` (albo usługa
przez NSSM). Komputer musi być jednak włączony – do pracy 24/7 służy [mały serwer](#serwer-247-vps-lub-raspberry-pi).
Tryb bota zastępuje `--notify-telegram` (nie uruchamiaj obu dla tego samego czatu).

## Serwer 24/7 (VPS lub Raspberry Pi)

Bot tylko **wychodzi** do internetu (Telegram long polling, GUNB, ULDK) – nie potrzebuje publicznego
IP, domeny ani otwartych portów. Wystarczy najmniejszy VPS z Ubuntu 24.04 (także Debian 12+, Ubuntu
22.04+, Raspberry Pi OS Bookworm+): 1 rdzeń, 512 MB RAM, 5 GB dysku.

**Krok po kroku – nowy bot, przeniesienie bota z komputera, aktualizacja, wycofanie, kopie i monitor:
[docs/WDROZENIE.md](docs/WDROZENIE.md).** Nowy bot jednym poleceniem:

```bash
curl -fsSL https://raw.githubusercontent.com/msm3107/GUNB-TOOL/main/deploy/install.sh | sudo bash
```

[`deploy/install.sh`](deploy/install.sh) instaluje każdą wersję kodu osobno (`/opt/gunb-tool/releases/`,
dokładne wersje zależności z `requirements.lock`, właściciel root) i przełącza `current` dopiero po
zatrzymaniu bota i kopii bazy. Stan trzyma w `/var/lib/gunb-tool` (config, `.env` z prawami 600, baza,
kopie). Usługa systemd [`gunb-bot`](deploy/gunb-bot.service) działa na koncie bez uprawnień: startuje
z serwerem, wstaje po awarii, a przy błędzie konfiguracji albo bazie nowszej niż kod zatrzymuje się
bez pętli restartów. `--przygotuj` przygotowuje serwer pod przeniesienie bota i niczego nie uruchamia.

| Co | Polecenie na serwerze |
|---|---|
| stan i kontrola zdrowia | `sudo gunb-admin status` · `sudo gunb-admin zdrowie` |
| dziennik | `sudo gunb-admin logi -f` |
| aktualizacja / wycofanie | `sudo gunb-admin aktualizuj <wersja>` · `sudo gunb-admin wycofaj` |
| kopia bazy / kopia do pobrania poza serwer | `sudo gunb-admin kopia` · `sudo gunb-admin kopia-do-pobrania` |

Kontrola zdrowia działa też bez serwera: `python main.py --zdrowie` (kod 0 / 1 / 2, tylko odczyt, bez
migracji). `--ping <adres>` albo `HEALTHCHECK_PING_URL` zgłasza wynik do monitora typu healthchecks.io.

> **Jeden token = jeden działający bot.** Dwa procesy odbierające aktualizacje tym samym tokenem
> dostają od Telegrama błąd 409 – bot wstrzymuje wtedy wysyłki i alarmuje admina. Drugi proces na tych
> samych danych kończy się od razu (blokada obok bazy, kod 3).

### Zmiany schematu bazy

Przy starcie nowej wersji migracje wykonują się same, każda w osobnej transakcji, a przed pierwszą
z nich powstaje kopia bazy (`backups/<baza>-przed-v<N>-<czas>.sqlite`). Użytkownicy zaakceptowani przed
abonamentami (baza < v6) dostają dostęp bez terminu – przełączasz ich świadomie: `/nowymodel wszyscy 30`
albo `/nowymodel <chat_id> 2026-12-31`. Starsza wersja programu **nie uruchomi się** na bazie z nowszym
schematem (kod wyjścia 2) – powrót do niej wymaga odtworzenia kopii sprzed aktualizacji, a zmiany
z czasu po tej kopii przepadają ([docs/WDROZENIE.md](docs/WDROZENIE.md), sekcja E).

### Przygotowanie e-mail i WhatsApp (PR 1–3, schemat v14)

Migracja dodaje cztery puste tabele: `notification_endpoints`, `notification_outbox`,
`notification_deliveries` i `notification_webhook_events` (v13). PR 3 dodaje tabelę
`email_verifications` w v14. Migracja nie tworzy odbiorców ani zadań
wysyłki; zachowuje dane i działanie obecnego bota. Kolejka i historia są przypisane do
odbiorcy nowego kanału, niezależnie od tabeli `deliveries` Telegrama.

Sekcje `email.enabled` i `whatsapp.enabled` domyślnie mają wartość `false`. Starszy
`config.yaml` działa bez ich dopisywania. Próba ustawienia `true` kończy się błędem
konfiguracji przed otwarciem bazy: rzeczywista wysyłka będzie dostępna w kolejnych PR.
To wydanie nie wymaga danych SMTP ani tokenu Meta i nie dodaje zależności.

PR 2 dodaje wewnętrzne API raportów, magazynu odbiorców i workera outbox. Raport obejmuje
do 20 inwestycji, dzieli treść na części do 32 KiB i zapisuje pierwszy snapshot w kolejce.
Historia nowego kanału nie pomija inwestycji dostarczonych przez Telegram. Worker ponownie
sprawdza zgodę, adres, dostęp, filtry i rewizje; odbiorcy nie spełniający warunków nie dostają
zakolejkowanego raportu. Status w rejestrze GUNB nie jest potwierdzeniem etapu robót.

Worker wykonuje ograniczony cykl z nadawcą przekazanym przez kod. PR 3 dodaje adapter
SMTP i API jednorazowej weryfikacji skrzynki. Obecny bot i CLI nie wywołują tych API;
konfiguracja SMTP, ustawienia nowych kanałów i weryfikacja nie są jeszcze dostępne użytkownikowi.
`accepted` oznacza przyjęcie przez dostawcę. Timeout lub crash z nieznanym
wynikiem zatrzymuje dany endpoint bez automatycznej ponownej wysyłki; nie gwarantujemy
wysyłki dokładnie raz. Limity i warunki przyszłego uruchomienia opisuje
[docs/WDROZENIE.md](docs/WDROZENIE.md#pr-2-raporty-i-trwały-outbox-bez-uruchomienia-nadawców).

Weryfikacja ma token ważny 15 minut, do 5 prób i trwałe limity owner/adres/globalnie.
Baza przechowuje skróty tokenu i adresu; surowy token trafia tylko do wiadomości.
Potwierdzenie skrzynki nie zapisuje zgody ani nie włącza raportów. SMTP wymaga STARTTLS
lub implicit TLS; Message-ID nie gwarantuje wysyłki dokładnie raz. Szczegóły i ograniczenia:
[wdrożenie PR 3](docs/WDROZENIE.md#pr-3-smtp-i-weryfikacja-adresu-v13--v14).

Kolejne kroki to integracja e-mail z konfiguracją, zgodami/UI i harmonogramem oraz
WhatsApp API z szablonami i webhookiem HTTPS. PR 3 wymaga v14 dla trwałej weryfikacji;
**powrót do PR 1–2/v13 wymaga odtworzenia kopii sprzed migracji** i cofa późniejsze dane.
Samo przełączenie kodu nie wystarcza. Aktualizacja robi kopię przez istniejący mechanizm.

Powrót z v13 do kodu znającego tylko v12 wymaga odtworzenia kopii sprzed migracji;
instrukcja i kontrola po aktualizacji są w [docs/WDROZENIE.md](docs/WDROZENIE.md#d-aktualizacja).

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

**Praca bez nadzoru (serwer 24/7):**

| Mechanizm | Jak działa |
|---|---|
| **Ponowienia** | Każde zapytanie do GUNB i ULDK: 3 ponowienia z backoffem 2 → 4 → 8 s (`http.max_retries`). |
| **Bezpiecznik** | 3 nieudane zapytania z rzędu do jednego serwera → przez 10 min kolejne są od razu odrzucane, potem jedno próbne (`http.circuit_breaker_*`). Awaria ULDK nie blokuje bota na godziny, a geokodowanie wraca samo. |
| **Ponowienie pobierania** | Nieudane poranne pobieranie w trybie `--bot` jest ponawiane co godzinę aż do skutku. |
| **Alerty admina** | Błędy z logów trafiają na osobny czat `TELEGRAM_ADMIN_CHAT_ID` z czytelnym nagłówkiem, np. „🚨 BŁĄD: ULDK zwraca HTTP 500”, „GUNB zablokował dostęp (HTTP 403)”, „Baza SQLite zablokowana”, a po awarii „✅ … znów odpowiada”. Ta sama awaria (także u wielu osób naraz) najwyżej raz na 3 h, łącznie najwyżej 20 alertów na godzinę. Klienci niczego nie widzą. Test: `python main.py --test-alert`. |
| **Kopia bazy** | Codziennie (nocne porządki bota o 03:30 albo przed pobieraniem): `data/backups/gunb_leads-RRRR-MM-DD.sqlite`, spójna kopia przez API SQLite (także w trybie WAL), 14 ostatnich; osobno kopia przed każdą zmianą schematu i wersji. |
| **Kontrola zdrowia** | `python main.py --zdrowie`: proces, odbieranie wiadomości, wątek zadań, ostatni import, zaległe i nieudane wysyłki, baza, dysk, konflikt tokenu – tylko odczyt, niczego nie restartuje. |
| **VACUUM** | Po dużym imporcie (≥ 500 nowych/zmienionych leadów, `storage.vacuum_threshold`). |

Z pól inwestora i projektanta wyciągany jest też telefon (`+48…`) i e-mail do kolumn `telefon` i
`email`; karta inwestycji pokazuje je jako „📞 Kontakt wpisany w rejestrze” (bez danych – bez linii).
W rejestrze to rzadkość: pomiar na 2 442 042 wierszach
(4 województwa i ogólnopolskie zgłoszenia) dał 5 wierszy z telefonem, wszystkie wpisane w pole numeru
uprawnień projektanta, i zero e-maili. Dopasowanie jest ostrożne: 9 cyfr bez „tel.” lub typowego
zapisu telefonu (np. REGON) jest pomijane.

---

## Testy

```bash
pip install -r requirements-dev.txt
pytest
```

Testy działają bez sieci (atrapy klienta HTTP, SQLite w pamięci, atrapa arkusza) na syntetycznych
danych w formacie GUNB. CI uruchamia je na Pythonie 3.10–3.14.

**Testy QA całego potoku** (`tests/qa/`, fixtures w `tests/qa/conftest.py`): HTTP mockowane na poziomie
transportu (`responses`), więc działa prawdziwa sesja `requests` z ponowieniami, bezpiecznikiem
i `Retry-After`. Przerwy to `Mock` z `pytest-mock`, dzięki czemu widać każdą przerwę i jej długość.

| Warstwa | Co sprawdza |
|---|---|
| Pobieranie (`test_ingestion_layer.py`) | strona HTML zamiast ZIP (4 próby, łagodny koniec), uszkodzony CRC / strumień deflate (błąd formatu, uszkodzona paczka usuwana z cache), kolumna `kubatura` → `kubatura_m3` i nieznana nazwa (alarm dla admina), plik w Windows-1250, brudne pole projektanta → nazwa + telefon + e-mail + adres |
| Geokodowanie (`test_geocoding_layer.py`) | 500/502/503 → 3 ponowienia z backoffem 2 → 4 → 8 s i wyjątek, bezpiecznik, geometria w EPSG:2180 i poza Polską odrzucana, pusty poligon → brak punktu albo środek obrębu |
| Baza (`test_storage_layer.py`) | UPSERT „wniosek” → „decyzja”: ten sam rekord, flaga do wysłania, historia; WAL: bot czyta w trakcie zapisu, odczyt nie blokuje zapisu (kontrola bez WAL), wątki scrapera i bota |
| Doręczanie (`test_delivery_layer.py`) | 429 FloodWait: pauza o `retry_after`, kolejność i tempo 1/s, limit 10 min; 403 „bot was blocked”: klient oflagowany, pozostali dostają raport, ponowne odblokowanie |

Każdy mechanizm sprawdzono też testem mutacyjnym. Celowo zepsute ponowienia, limit `Retry-After`,
rozpoznawanie blokady, walidacja EPSG, bezpiecznik, flaga wysyłki i tryb WAL są wyłapywane przez testy.

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
