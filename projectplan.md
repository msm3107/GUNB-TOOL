# Żółta Tablica V2 — plan produktu i wdrożenia

Status: propozycja po audycie 21.10.2026, do decyzji właściciela w PR.
Ten dokument zastępuje poprzednią kolejność 18 PR-ów. Nie oznacza wdrożenia opisanych funkcji.
Poprzednia wersja pozostaje w historii Git. Audyt: [docs/REVIEW_V2_2026-10-21.md](docs/REVIEW_V2_2026-10-21.md).

## 1. Decyzja produktowa

Sprzedajemy przygotowaną listę inwestycji do pracy handlowej: dopasowanie do oferty,
sprawdzalne źródła, jawne braki, poprawnie przypisany kontakt i aktualizacje.
Sam eksport publicznego rejestru do Excela nie uzasadnia trwałej przewagi.

Pierwszy produkt: paczka jednorazowa, przygotowana przez silnik i zatwierdzona przez operatora.
Docelowo: ten sam proces z zamówieniem przez stronę oraz opcjonalnymi kolejnymi wydaniami.
Abonament i automatyczne płatności uruchamiamy dopiero po potwierdzeniu ponownych zakupów.
Nie przywracamy botów Telegram/WhatsApp.

Hipoteza pierwszego segmentu: handlowcy dostawców pokryć dachowych i obudów hal,
obsługujący inwestycje komercyjne oraz wielorodzinne, z jawnym podmiotem inwestującym.
To segment do sprawdzenia, nie potwierdzony zwycięzca. Nie mieszamy w pierwszym pilotażu
lokalnego dekarza domów jednorodzinnych, hurtowni, HVAC i producenta konstrukcji.

Geografia: 2–3 uzgodnione regiony obsługiwane przez klientów pilotażu, także poza
warmińsko-mazurskim. Zasięg handlowy może być ogólnopolski; dostępność paczek deklarujemy
wyłącznie tam, gdzie import i jakość zostały sprawdzone. Brak arbitralnej obietnicy
liczby inwestycji w każdym powiecie.

## 2. Co klient rzeczywiście kupuje

Paczka: 15–25 zaakceptowanych inwestycji, jeżeli tyle spełnia uzgodnione kryteria.
Liczba jest celem oferty pilotażowej, nie wynikiem gwarantowanym przez bazę.
Przed zakupem klient zna faktyczną liczbę, okres, obszar, zakres weryfikacji i cenę.
Nie dopełniamy paczki słabymi rekordami. Przy braku podaży proponujemy zmianę zakresu
przed płatnością albo rezygnujemy z realizacji.

Każdy rekord odpowiada na sześć pytań:

1. Co i gdzie zaplanowano? Opis, typ obiektu, skala, lokalizacja i precyzja mapy.
2. Dlaczego pasuje do mojej oferty? Konkretna reguła branżowa z dowodem w opisie.
3. Co wiadomo o stanie sprawy? Fakt urzędowy, data zdarzenia, źródło i data sprawdzenia.
4. Z kim można rozmawiać? Podmiot, rola kontaktu, kanał, źródło i poziom weryfikacji.
5. Czego nie wiadomo? Etap fizycznej budowy, brak kontaktu, niepewne powiązanie itp.
6. Co zmieniło się od poprzedniej paczki? Czytelne stare/nowe wartości, nie sam hash.

Nie nazywamy wpisu z pozwoleniem otwartym przetargiem, potwierdzonym zapotrzebowaniem
ani zamówieniem gotowym do zdobycia. Nie obiecujemy wyłączności na publiczne dane.

### Poziomy weryfikacji

- Źródłowy: odczyt z rejestru z datą i identyfikatorem sprawy.
- Sprawdzony przez operatora: potwierdzone dopasowanie i źródła informacji.
- Potwierdzony kontaktowo: tylko gdy rzeczywiście uzyskano potwierdzenie, z datą i zakresem.

Publiczny telefon firmy nie oznacza kontaktu do osoby decyzyjnej ani potwierdzonego
zainteresowania ofertą. Projektant pozostaje projektantem.
Nie pozyskujemy ani nie odtwarzamy ukrytych kontaktów prywatnych inwestorów.

## 3. Wyróżnik i alternatywy

| Wariant | Zaleta | Koszt lub ryzyko | Decyzja |
|---|---|---|---|
| Tani eksport GUNB | Szybki do uruchomienia | Łatwy do skopiowania, praca pozostaje po stronie klienta | Narzędzie wewnętrzne i próbka |
| Paczka branżowa po kontroli jakości | Mały wysiłek klienta, jawna jakość, wynik do użycia | Wymaga pomiaru pracy operatora | Pierwszy produkt |
| Pełna platforma bazodanowa | Samoobsługa i szeroki zakres | Dużo kodu przed dowodem popytu, silna konkurencja | Odłożone |

Przewaga do udowodnienia: mniej nietrafionych rekordów i krótszy czas znalezienia
inwestycji wartych dalszej pracy niż przy samodzielnej pracy na rejestrze lub używanej bazie.
Mapa, scoring, dane GUNB i newsletter są funkcjami pomocniczymi, nie deklaracją unikalności.

Mierzymy: akceptację dopasowania, oszczędzony czas, poprawność roli kontaktu,
przejście do rozmowy/oferty, ponowny zakup oraz koszt przygotowania.
Nie utożsamiamy otwarcia pliku z wartością biznesową.

## 4. Interakcje klienta i operatora

### Klient

1. Wybiera branżę i obszar, podaje typ obsługiwanych inwestycji i wykluczenia.
   Na początek formularz ma kilka pól; nie wymaga znajomości TERYT ani ustawiania wag.
2. Ogląda trzy rzeczywiste, reprezentatywne przykłady wraz z brakami danych.
   Demo publiczne jest zanonimizowane; próbka prywatna pokazuje rzeczywisty standard paczki.
3. Otrzymuje ofertę: faktyczna liczba zaakceptowanych rekordów, okres, cena, warunki
   korekty i termin realizacji. Może odmówić. Brak dopasowań nie prowadzi do płatności.
4. Po potwierdzeniu płatności otrzymuje XLSX i CSV przez uzgodniony kanał.
   Pilotaż może korzystać z ręcznej dostawy plików; docelowo bezpośredni, wygasający
   link do wydania. Konto i panel nie są warunkiem pierwszego zakupu.
5. Oznacza wynik pracy prostymi wartościami: pasuje / nie pasuje / znane /
   błędny kontakt / rozmowa / oferta. W pilotażu odsyła arkusz lub odpowiada na mail.
6. Zamawia kolejną paczkę lub kończy. Oferta cykliczna pojawia się po sprawdzeniu
   podaży i ponownego zapotrzebowania. Zero nowości = jawny komunikat, nie sztuczna paczka.

### Operator

1. Sprawdza wynik ostatniego importu i pokrycie obszaru.
2. Wybiera profil klienta; silnik przygotowuje kandydatów i liczbę już znanych.
3. Przegląda kolejkę: zaakceptuj / odrzuć z powodem / sprawdź dane.
   Uzupełnia źródło, rolę i datę weryfikacji kontaktu; nie zgaduje adresów e-mail.
4. Zatwierdza zamrożony podgląd wydania. Oferta odnosi się do tego samego zestawu,
   a nie do ponownie policzonej, zmieniającej się bazy.
5. Potwierdza płatność i publikuje wydanie; widzi wynik doręczenia oraz możliwość
   ponowienia tej samej dostawy.
6. Rejestruje korekty i czas pracy. Uwagi klienta wymagają oceny operatora przed
   zmianą danych udostępnianych wszystkim klientom.

Docelowy panel operatora: kolejka przeglądu, zamówienia/wydania, uwagi i zdrowie importu.
Nie budujemy pełnego CRM.

## 5. Kontrakt pliku

XLSX otwiera się na krótkiej liście roboczej. Pierwsze kolumny:
priorytet dla profilu, miejscowość, inwestycja, dlaczego pasuje, stan sprawy,
podmiot i rola, kontakt, data weryfikacji, brakujące informacje, następna czynność,
mapa, ID inwestycji. Kolumny techniczne trafiają dalej lub do szczegółów.

Zakładki:
- Nowe — pierwszy raz przekazane danemu klientowi; stara decyzja nie udaje nowej budowy.
- Zmiany — znane projekty ze wskazaniem, co zmieniono; także ostrzeżenia i korekty.
- Pełna baza — aktualny snapshot zakresu zakupionego przez klienta, zawiera też pozycje z dwóch pierwszych arkuszy.
- Instrukcja i jakość — zakres, daty, liczby, definicje statusów i ograniczenia.
- Historia — czytelne wiersze zmian; JSON dopuszczalny w eksporcie technicznym.

XLSX: filtry, zamrożony nagłówek, szerokości kolumn, zawijanie tekstu i linki.
CSV: stabilny schemat do CRM, UTF-8 BOM, udokumentowany separator, jeden wiersz
na inwestycję. Kolumna typu zmiany pozwala uniknąć wielokrotnego importu tego samego
rekordu występującego w kilku arkuszach XLSX.
Oba formaty chronione przed formułami i niepoprawnymi znakami.
Liczniki paczki liczą unikalne inwestycje, nie sumę wierszy we wszystkich arkuszach.

Po reklamacji błędnego faktu oferujemy korektę lub zastępstwo według warunków sprzedaży;
brak wygranego zlecenia sam w sobie nie jest błędem danych.

## 6. Architektura: jeden silnik, jedna baza, mała aplikacja

Pozostają Python, SQLite WAL, import oficjalnych ZIP/CSV, cache ULDK,
backupy i CLI. Bez mikroserwisów, brokera kolejek i automatycznego AI-researchu w MVP.

| Moduł | Odpowiedzialność | Wejście / wyjście |
|---|---|---|
| Import i normalizacja | Pobranie, identyfikacja źródła, fakty i daty | ZIP/CSV → sprawy + wynik przebiegu |
| Powiązania i rewizje | Grupowanie spraw w inwestycje, istotne zmiany | Sprawy → investment_id + rewizja |
| Dopasowanie | Reguły konkretnej branży, wykluczenia i powody | Profil + inwestycje → kandydaci |
| Jakość i kontakty | Rola, źródło, data, decyzja operatora | Kandydaci → zaakceptowane rekordy |
| Paczki i realizacja | Snapshot, eksport, pamięć klienta, ponowienia | Zamówienie → niezmienne wydanie |
| App | Formularz, panel operatora, później pobieranie | HTTP → krótkie operacje usług silnika |

FastAPI/Jinja2 nadaje się do małego panelu. Publiczną stronę w ZlotaTablicaWeb
można zachować i zmienić CTA oraz treść; nie tworzymy dwóch równoległych landing pages.
W pilotażu strona przyjmuje zapytanie o paczkę. Wewnętrzny panel nie jest publiczny
bez uwierzytelnienia, kontroli dostępu i ochrony formularzy.

## 7. Wymagania danych przed sprzedażą

### Fakty, hipotezy, daty

- Rozdzielamy status postępowania, datę decyzji i potwierdzony etap fizycznej budowy.
- Brak informacji o rozstrzygnięciu nie pozwala oznaczyć decyzji jako pozytywnej.
- Obecne okna branżowe są heurystyką wieku sprawy. Nie stanowią dowodu, że trwa montaż dachu.
  Domyślnie pokazujemy propozycję weryfikacji, nie polecenie „dzwoń teraz”.
- RWDZ w obecnym importerze nie dostarcza potwierdzenia rozpoczęcia robót ani wyboru kierownika.
  Takie dane wymagają osobnego, zweryfikowanego źródła.
- Zapisujemy source_event_at, first_seen_at, last_checked_at, source_snapshot_id
  i zakres ukończonego importu. Data decyzji nie jest datą publikacji ani odczytu.
- W źródle pobieranym przez program aktualizacja jest nocna. Obietnica dotyczy
  dostawy po odświeżeniu źródła, nie powiadomienia w chwili wpisu urzędnika.
- Import częściowy lub przerwany nie potwierdza świeżości całego obszaru.

### Dopasowanie i zakres

Reguły branżowe są wersjonowane i działają przy selekcji paczki.
Ogólna ocena wielkości obiektu nie zastępuje dopasowania do oferty.
Rozbiórka może być bezużyteczna dla dostawcy okien, a przydatna dla odbiorcy gruzu.
Nie odrzucamy bezpowrotnie takich danych przed decyzją, jakie profile obsługujemy.

W MVP zachowujemy snapshot źródła lub wystarczający materiał do ponownej klasyfikacji
z uzgodnioną retencją. Każdy profil ma przypadki poprawnych i błędnych dopasowań.
Szacunek promienia wymaga jawnej precyzji lokalizacji. Brak współrzędnych nie oznacza,
że inwestycja jest poza obszarem; takie rekordy trafiają do weryfikacji.

### Kontakty i lokalizacja

Kontakt to osobny rekord: podmiot, rola, telefon/email, źródło, data sprawdzenia,
metoda weryfikacji i pewność powiązania z inwestycją.
Rozpoznanie firm opiera się na identyfikatorach i adresie, nie wyłącznie podobnej nazwie.
Dane projektanta nie zastępują kontaktu inwestora.
Zmiana działek unieważnia stare współrzędne do ponownego sprawdzenia.

### Pamięć klienta

Obecny klasyfikator już sprawdza historię zamówień całego customer_id — zachowujemy to.
Jeden klient nie otrzymuje znanej inwestycji jako nowej po zmianie zamówienia.
ID sprawy urzędowej nie zawsze oznacza osobną inwestycję: powiązania wymagają ostrożnego
dopasowania po podmiocie, działkach i zakresie robót. Wątpliwe grupy zatwierdza operator;
sama wspólna działka nie wystarcza.

Rewizja handlowa obejmuje istotny status, lokalizację, podmiot, kontakt i korekty jakości.
Zmiana daty sprawdzenia, stylu tekstu lub wag rankingu nie generuje płatnej nowości.
Korekta danych już sprzedanych trafia do klienta także po wypadnięciu rekordu z filtra.
Pełny snapshot może powtarzać rekordy dla wygody; nie są liczone jako nowe.
Nowość dla klienta, nowy wpis w źródle i aktualizacja to trzy różne pojęcia.

### Odświeżanie

Domyślne 60 dni nie wystarcza do wielomiesięcznych paczek i aktualizacji starszych spraw.
Import najpierw obejmuje uzgodniony zakres historyczny, a następnie monitoruje świeże
rekordy i wszystkie sprawy dostarczone klientom w okresie obsługi. Starsze sprawy sprawdzamy
z aktualnych snapshotów źródła niezależnie od daty decyzji.
Brak rekordu w niepełnym imporcie nie oznacza anulowania inwestycji.
Pełne, szerokie przeliczenia uruchamiamy okresowo; geokodowanie tylko dla zmienionych
lub potrzebnych kandydatów. Pomiar pamięci przed importem całej Polski.

## 8. Niezawodne wydania i dostęp

Stan zamówienia i stan płatności są oddzielne od stanu wydania.
Wydanie: draft → approved → building → ready → released; błędy budowy osobno.
Doręczenie maila: pending → accepted_by_provider / failed / unknown.
Akceptacja przez SMTP nie jest dowodem przeczytania ani dostarczenia do skrzynki odbiorczej.

1. W transakcji rezerwujemy wydanie i jego idempotency_key; pobieramy snapshot danych.
2. Poza transakcją tworzymy pliki tymczasowe, sprawdzamy format i sumy kontrolne.
3. Atomowo przenosimy pliki, a następnie zapisujemy gotowy manifest i uprawnienie do wydania.
4. Po zatwierdzeniu publikujemy wydanie i zapisujemy historię klienta atomowo w bazie.
5. Osobne zadanie doręcza powiadomienie. Ponowienie używa tego samego wydania i plików.

Awaria przed publikacją nie konsumuje nowości klienta. Awaria SMTP po publikacji
nie tworzy kolejnej płatnej paczki. W pilotażu ręczne przekazanie ma jawny zapis operatora;
stan niepewny wymaga wyjaśnienia zamiast automatycznej nowej sprzedaży.
Równoległe wydania tego samego klienta są serializowane.
Nie wywołujemy importu, SMTP ani generowania XLSX w webhooku płatności.

Minimalne uzupełnienia istniejącego schematu: import_runs, referencja źródła i rewizji,
kontakty z rolą, decisions operatora, status/idempotency_key/manifeste/hash wydania,
snapshot zaakceptowanych danych oraz feedback.
Nazwy i migracje należy uzgodnić w pierwszym PR; nie zmieniamy już wydanych migracji.
Backup i odtworzenie obejmują bazę i pliki paczek, także kopię poza serwerem.

Bezpieczne pobieranie pojawia się przed uruchomieniem publicznego automatycznego wydawania:
losowy ograniczony token, hash w bazie, termin ważności, powiązanie z wydaniem,
możliwość odwołania i brak tokenów w logach. Pliki poza katalogiem statycznym.
Magic link do sesji ma jednorazowy nonce; sam HMAC z datą nie zapewnia jednorazowości.
Sesje: Secure, HttpOnly, SameSite, rotacja; limity prób i kontrola dostępu per klient.
Ewentualne skanowanie linków przez system pocztowy nie może bezpowrotnie zużyć dostępu.

## 9. Kolejność prac — małe PR-y i bramki biznesowe

Każdy PR zatwierdza i scala właściciel. Agent nie scala automatycznie.
Nie określamy z góry modeli wykonawców; ważniejsze są kontrakt zmiany i weryfikacja.

| Etap | Zakres | Odbiór |
|---|---|---|
| A: integralność danych | Pochodzenie statusu, rozdział kontaktów, unieważnianie geolokalizacji, daty importu | Reprodukcje z audytu stają się testami regresji; brak fałszywego pozytywnego statusu |
| B: jeden profil + kontrola jakości | Reguły branżowe, historia zakresu, grupowanie spraw, akceptacja operatora | Próbka kandydatów oceniona ręcznie; pomiar dopasowania i braków |
| C: użyteczna paczka | Rozwinięcie PR #20: czytelny XLSX, kontakt z rolą, źródła, pełny snapshot, delta | Jedna zaakceptowana inwestycja przechodzi cały przepływ; klient znajduje ją bez instruktażu |
| D: trwałe wydanie | Snapshot, pamięć klienta, rewizja handlowa, manifest, retry i korekty | Awaria i ponowienie nie zmieniają pliku ani nie duplikują nowości |
| E: płatny pilotaż | Zmieniona strona/próbka, prosty formularz, ręczna płatność i kontrolowana dostawa | Mierzone zakupy, czas operatora i użyteczność; bez abonamentu |
| F: cykliczne wydania | Harmonogram, raport pustego zakresu, nowe/zmiany, bezpieczne pobieranie | Powtórny zakup i niezawodność w kilku cyklach |
| G: samoobsługa | Checkout, webhooki, opcjonalny panel i abonament | Dopiero po bramce biznesowej; testy izolacji, retry i anulowania |

PR #20 jest w toku — nie dublujemy jego eksportera. Rozszerzenia można wykonać jako
jego korektę lub następny PR po akceptacji właściciela.
Testy pełnego minimalnego przepływu powstają z etapami C–D, nie jako ostatni osiemnasty PR.

Dla przyszłych płatności: ceny wylicza serwer z zatwierdzonej oferty, weryfikujemy podpis,
kwotę, walutę i uprawnienie. Zdarzenie zakończenia Checkout nie zawsze oznacza opłacenie.
Obsługujemy opóźnione płatności, ponowienia i zdarzenia poza kolejnością.
Anulowanie odnowienia nie odbiera automatycznie dostępu do opłaconego okresu
ani wcześniej kupionych plików. Zwrot nie usuwa historii ujawnionych danych.

## 10. Pilotaż i ekonomika

Proponowany test decyzyjny: 10 kwalifikowanych firm w jednej branży, z czego każda
ocenia próbkę i otrzymuje konkretną ofertę. Nie jest to statystyczne badanie całego rynku.
Pozyskanie: formularz z próbką, polecenia, partner branżowy i rozmowy z zainteresowanymi.
Kampanii nie oceniamy samą liczbą wysłanych maili.

Przed większą automatyzacją wymagamy:
- co najmniej 3 płatnych paczek;
- co najmniej 2 ponownych zakupów w ciągu około 30–45 dni, zgodnie z cyklem klienta;
- co najmniej 80% rekordów uznanych za trafnie dopasowane, a nie „mających numer telefonu”;
- dodatniego wyniku po uwzględnieniu pracy operatora i kosztu pozyskania;
- braku nierozwiązanych krytycznych błędów przypisania kontaktu i lokalizacji.

To proponowane wewnętrzne progi. Niespełnienie oznacza zmianę segmentu/oferty albo
zatrzymanie inwestycji w portal, nie automatyczną wysyłkę do kolejnego tysiąca firm.

Cena do testu: 299–499 zł za paczkę o jasno opisanym zakresie weryfikacji.
Nie jest to potwierdzona cena rynkowa. Przed ofertą określamy kwotę końcową i zasady VAT
stosownie do statusu sprzedawcy. Brak automatycznej obietnicy 99 zł miesięcznie za ręczny research.

Przykład wyłącznie kalkulacyjny: przychód 399 zł bez VAT do rozliczenia,
2 godziny pracy × 60 zł + 20 zł kosztów zmiennych = 140 zł kosztu realizacji,
259 zł pozostaje przed pozyskaniem klienta, kosztami stałymi i podatkiem.
Przy 5 godzinach pozostaje 79 zł — taki proces wymaga podniesienia ceny lub uproszczenia.
Rejestrujemy rzeczywisty czas, a wspólne dane weryfikujemy ponownie tylko gdy się zmieniły.
Bez pomiaru ponownego zakupu nie prognozujemy MRR.

## 11. Ograniczenia i warunki uruchomienia

Oddzielamy prawo do przetwarzania/udostępniania danych od prawa do kontaktu marketingowego.
Publiczny adres lub telefon sam w sobie nie oznacza zgody na marketing.
Przed sprzedażą: przegląd zasad ponownego wykorzystania źródeł, obowiązków informacyjnych,
retencji i uprawnień do eksportowanych danych. JDG może obejmować dane osobowe.
W programie nie ma automatycznego wysyłania ofert do osób z paczki.
Realizacja zamówienia i zgody marketingowe mają osobne cele.

Zakres danych, korekty, dostęp do kupionych plików i zasady odpowiedzialności muszą być
zrozumiałe przed płatnością. Reklamacje dotyczące poprawności danych nie wymagają abonamentu.

## 12. Stan zastany i źródła

Na main 63a47cb: silnik, SQLite v16 i klasyfikator pamięci dostaw.
Na PR #20 / 2c504fa: generator plików; app pozostaje szkieletem.
Nie ma jeszcze pełnego przepływu zakupu i dostawy. Szczegóły testów i luk są w audycie.

Źródła sprawdzone 21.10.2026:
- GUNB, nocna aktualizacja ZIP/CSV: https://wyszukiwarka.gunb.gov.pl/pobranie.html
- GUNB, zakres i ograniczenia: https://wyszukiwarka.gunb.gov.pl/o-wyszukiwarce/
- Budowo, publicznie deklarowana oferta: https://www.budowo.pl/
- Kompas Inwestycji, publicznie deklarowana oferta: https://kompasinwestycji.pl/oferta
- PKE, art. 398: https://eli.gov.pl/api/acts/DU/2024/1221/text.html

Opis konkurencji opiera się na ofertach publicznych, nie na niezależnym teście jakości.
Nie umieszczamy w repozytorium treści prywatnej korespondencji ani list adresatów.
