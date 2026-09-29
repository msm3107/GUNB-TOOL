# Backlog P2 – po pilotażu „Żółtej Tablicy”

Nic z tej listy nie jest zaimplementowane. Kolejność ustalamy **na podstawie danych z pilotażu**
(`/raport 7` i `/raport 30`, `/status`, rozmowy z firmami), nie z góry.

| # | Pomysł | Po co | Sygnał z pilotażu, który go uzasadni | Szacunek |
|---|---|---|---|---|
| 1 | **Ranking osobisty z ocen 👍/👎** | Wyżej to, co dana firma uznaje za przydatne | ≥ 30 ocen na osobę i wyraźna różnica 👍/👎 między rodzajami / kubaturą | M |
| 2 | **Płatności automatyczne** (np. Przelewy24/Stripe + webhook → `set_access`) | Koniec ręcznego `/aktywuj` | > 10 płacących albo admin nie nadąża z przedłużeniami | L |
| 3 | **CSV na żądanie** (`/eksport` – zapisane i ostatnie 30 dni) | Przeniesienie do Excela/CRM firmy | Prośby firm; duża liczba zapisanych na osobę | S |
| 4 | **Konta zespołowe** (kilka osób, wspólne zapisane i notatki) | Firmy z biurem i ekipami | Kilka kont z tej samej firmy; przekazywanie inwestycji dalej | L |
| 5 | **Panel WWW** (lista, mapa, notatki) | Wygodniejsza praca przy biurku | Użytkownicy proszą o większy ekran; dużo notatek | L |
| 6 | **Dodatkowe, zweryfikowane źródła kontaktu do firm** (tylko inwestorzy-firmy, np. KRS/CEIDG) | Szybszy kontakt z deweloperem | Wysoki odsetek otwarć kart z jawnym inwestorem-firmą | M |

Zasady na przyszłość:

- Dane osób prywatnych z rejestru pozostają ukryte – żaden punkt nie dodaje kontaktów do osób fizycznych.
- Każda funkcja zaczyna się od testu regresyjnego i nie zmienia zasad dostępu (P0.5) ani kolejki wysyłek (P0.1).
