# Fundament powiadomień e-mail i WhatsApp — PR 1

## Cel i zakres

Przygotować działający backend GUNB-TOOL do dodatkowych kanałów bez uruchamiania
wysyłki i bez zmiany obecnego Telegrama. Bazą jest `main` / `646ad22`.

Ten PR obejmuje wyłącznie schemat v13, wyłączone flagi konfiguracyjne i sprawdzenie
migracji, kopii oraz przenoszenia danych. Dane dostępowe SMTP/Meta, nadawcy, harmonogram
nowych kanałów i webhook HTTPS należą do następnych PR. Bez nowych zależności.

## Dane

Nowe tabele są puste po migracji; istniejące tabele i wiersze nie są zmieniane.

- `notification_endpoints`: własny liczbowy identyfikator, właściciel `chat_id`,
  kanał `email`/`whatsapp`, adres, tryb raportu, weryfikacja, zgoda i jej wycofanie,
  moment aktywacji, wersja adresu i znaczniki czasu. Domyślnie wyłączony.
  Włączenie wymaga weryfikacji i niewycofanej zgody. Unikalność: właściciel–kanał–adres.
- `notification_outbox`: odbiorca, klucz zdarzenia, numer części (od 0), utrwalona treść,
  stan, licznik prób, następna próba, termin ważności, dane zajęcia zadania,
  identyfikator dostawcy i znaczniki czasu. Unikalność: odbiorca–zdarzenie–część.
- `notification_deliveries`: odbiorca–sprawa–rewizja jako klucz; rodzaj, wynik,
  moment obsługi i opcjonalne powiązanie z wysyłką. Telegram zachowuje `deliveries`.
- `notification_webhook_events`: kanał, klucz deduplikacji, opcjonalny odbiorca,
  identyfikator wiadomości dostawcy, treść zdarzenia, czas otrzymania i przetworzenia.
  Unikalność: kanał–klucz. Może przechować zdarzenie przed skojarzeniem z wysyłką.

Tabele zależne od odbiorcy mają `ON DELETE CASCADE`; historia doręczeń może przetrwać
porządkowanie outbox (`ON DELETE SET NULL`). Usunięcie konta usuwa powiązane dane nowych
kanałów. Niepowiązane zdarzenia webhooków wymagają retencji w przyszłym workerze.
Indeksy pokrywają kolejkę według stanu i terminu, identyfikator wiadomości dostawcy,
historię odbiorcy i nieprzetworzone webhooki. Bez zależności od SQLite JSON1.

## Konfiguracja i wykonanie

`AppConfig.email` i `AppConfig.whatsapp` to niemutowalne konfiguracje z `enabled=False`.
Brak sekcji działa tak samo jak jawne `enabled: false`. `enabled: true` jest odrzucane
z czytelnym `ConfigError`: nadawcy nie istnieją w wydaniu przygotowawczym. Flaga nie
może sugerować, że niewdrożona wysyłka działa. Ten PR nie wprowadza credentiali.

Nie dodajemy wątków, połączeń sieciowych ani obsługi nowych tabel podczas cyklu bota.
Nie zmieniamy filtrów, uprawnień, rankingu, doręczeń, treści wiadomości ani CLI Telegrama.

## Aktualizacja i powrót

Nowy krok migracji używa istniejącej transakcji i kopii bazy przed migracją. Przerwany
krok nie pozostawia części nowych tabel ani podbitej wersji. Ponowny start nie tworzy
drugiej kopii ani zadań nowych kanałów.

Kod v12 nadal odmawia pracy na v13. Powrót do v12 wymaga zatrzymania zapisujących
procesów i odtworzenia kopii sprzed migracji, co cofa późniejsze dane. Kolejne wydanie
z nadawcami powinno zachować v13, aby można było wrócić do tego przygotowawczego wydania
bez cofania danych. Przed dodaniem innych kolumn trzeba ponownie ocenić rollback.

## Weryfikacja

Testy: populated v12 → v13 bez zmiany starych danych; kopia nadal v12 i daje się
odtworzyć; brak kopii blokuje migrację; przerwany krok jest atomowy; powtórny start
nie dubluje niczego; klucze, domyślne wyłączenie i relacje tabel; pełny eksport/import
z wypełnionymi nowymi tabelami; starsza konfiguracja działa; włączenie kanału kończy
się błędem przed otwarciem bazy. Pełne istniejące testy oraz CI Python 3.10–3.14.
