# Backlog – po pilotażu „Żółtej Tablicy”

Nic z tej listy nie jest zaimplementowane. Kolejność ustalamy **na podstawie danych z pilotażu**
(`/raport 30`, `/dane`, `/zamowienia`, rozmowy z firmami), nie z góry. Trudność: S – dni, M – tydzień, L – kilka tygodni.

| # | Pomysł | Jaki problem rozwiązuje | Sygnał od klientów, który uzasadni wdrożenie | Zależności | Trudność |
|---|---|---|---|---|---|
| 1 | **Ranking osobisty z ocen i wyników pracy** | Wyżej to, co dana firma uznaje za przydatne | ≥ 30 ocen/wyników na osobę i wyraźna różnica między rodzajami, kubaturą albo gminami | wyniki pracy (są), więcej danych | M |
| 2 | **Automatyczne płatności** (np. Przelewy24/Stripe + webhook → potwierdzenie zamówienia) | Koniec ręcznego `/zaplacone` | > 10 płacących albo admin nie nadąża z potwierdzeniami; prośby o kartę/BLIK | działalność z możliwością przyjmowania płatności online, regulamin, faktury | L |
| 3 | **CSV na żądanie** (`/eksport` – zapisane i wyniki pracy) | Przeniesienie do Excela/CRM firmy | Prośby firm; dużo zapisanych na osobę | – | S |
| 4 | **Konta zespołowe** (kilka osób, wspólne zapisane, notatki i wyniki) | Firmy z biurem i handlowcami | Kilka kont z tej samej firmy (`/firma`), przekazywanie inwestycji dalej | model firmy w bazie, uprawnienia | L |
| 5 | **Panel WWW z mapą** (lista, mapa, notatki) | Wygodniejsza praca przy biurku, widok wielu budów naraz | Użytkownicy proszą o większy ekran; dużo notatek i zapisanych | logowanie (np. Telegram Login), hosting z domeną | L |
| 6 | **Ranking AI** (opis inwestycji → dopasowanie do oferty) | Lepsze dopasowanie przy nietypowych opisach | Ranking z ocen (1) nie wystarcza; wiele „niepasujących” z powodu „zły rodzaj” mimo filtrów | ranking osobisty (1), budżet na API, ocena jakości | M |
| 7 | **Dodatkowe, zweryfikowane źródła danych firm-inwestorów** (KRS/CEIDG – tylko podmioty, nigdy osoby fizyczne) | Szybszy kontakt z deweloperem | Wysoki odsetek otwarć kart z nazwą inwestora i wyników „rozmowa” | podstawa prawna, weryfikacja dopasowań | M |
| 8 | **Masowe wzbogacanie kontaktów** | – (ryzyko prawne i reputacyjne) | Nie planujemy; wrócić tylko przy jasnej podstawie prawnej i wyłącznie dla firm | (7), opinia prawna | L |
| 9 | **Cold mailing / kampanie** | Docieranie do nowych firm | Nie z bota – uruchomienie bota nie jest zgodą na kampanie; ewentualnie osobny, świadomy zapis | osobna zgoda, wypis jednym kliknięciem | M |
| 10 | **Integracja z CRM** (np. eksport do popularnych CRM) | Brak przepisywania do systemu firmy | ≥ 3 płacące firmy używające tego samego CRM | CSV (3), klucze API klienta | M |
| 11 | **Nowe kanały powiadomień** (e-mail, SMS) | Firmy bez Telegrama | Utracone zainteresowane osoby bez Telegrama; prośby o e-mail | dostawca e-mail/SMS, zgody, wypis | M |
| 12 | **Ekspansja poza Olsztyn** (kolejne powiaty, potem województwa) | Większy rynek | Płacący klienci w Olsztynie i pytania z innych regionów (`/raport`: źródła, „poza obszarem”) | import historii dla nowych powiatów, wydajność geokodowania | M |

Zasady na przyszłość:

- Dane osób prywatnych z rejestru pozostają ukryte – żaden punkt nie dodaje kontaktów do osób fizycznych.
- Każda funkcja zaczyna się od testu regresyjnego i nie zmienia zasad dostępu ani kolejki wysyłek.
- Bot wysyła tylko wiadomości usługowe; marketing wymaga osobnej, świadomej zgody (punkt 9).
