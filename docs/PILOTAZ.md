# Pilotaż sprzedaży – jak prowadzić go z prawdziwą firmą

Cel: pokazać firmie użyteczną inwestycję, przyjąć zamówienie, odnotować rzeczywistą płatność i zobaczyć,
co doprowadziło do zakupu. Wszystko w bocie i jednym pliku `.env` – bez operatora płatności i bez panelu WWW.

## 1. Co zmieniło się w bocie

| Problem klienta | Zmiana | Oczekiwany efekt | Jak sprawdzić |
|---|---|---|---|
| Nowa osoba widziała „⛔ … aby opłacić abonament”, zanim zobaczyła, co to jest | Pierwszy kontakt: co robi bot, dla kogo, obszar, przykład, jak zacząć, cena (gdy ustawiona); bramka bez sugerowania zaległości | Więcej osób prosi o test zamiast wychodzić | `/raport 30`: przykład, oferta, prośba o test |
| Nie wiadomo, ile kosztuje i jak kupić | Jedna oferta w konfiguracji; ekran oferty, konto, przypomnienie przed końcem i informacja po końcu pokazują cenę i „🛒 Zamawiam” | Zamówienia bez pisania do admina | `/zamowienia`, zdarzenie `zamowienie` |
| Brak śladu płatności, dostęp ręczny wyglądał jak abonament | Zamówienie z migawką ceny → admin potwierdza **otrzymaną** płatność (raz) → dostęp +30 dni od końca obecnego; `/aktywuj` to dostęp „ręczny”, nie płatność | Wiadomo, kto zapłacił, a kto dostał dostęp promocyjnie | `/raport`: „Dostęp nadany ręcznie” osobno od „płatności potwierdzone” |
| Pierwszy przegląd był historią ostatnich 30 dni, także dla dekarza | Po starcie 3–5 najlepiej dopasowanych: okno etapu branży, odległość, data; przegląd całości jednym przyciskiem | Szybciej pierwsza przydatna inwestycja | zdarzenia `wyniki`, `szczegoly`, aktywacja w kohorcie |
| Pusty wynik wyglądał jak „nic się nie dzieje” | Przyczyna wprost: brak danych w bocie, miejsce spoza obszaru, wieś bez spraw, zbyt wąskie ustawienia, nieaktualny import; test nie startuje sam | Mniej porzuceń po pustym ekranie | zdarzenie `pusto` z przyczyną |
| 🔥 HOT sugerował „gotowego klienta” | Skala: duża/średnia/mała (szacunek), bez wpływu na kolejność; karta dzieli fakty z rejestru, „dlaczego to widzisz”, szacunki i własną notatkę | Mniej rozczarowań | testy `test_bot_card.py` |
| Brak inwestora = „osoba fizyczna” | „brak informacji w rejestrze”; nazwa inwestora „wg rejestru, bez weryfikacji” | Bez fałszywych wniosków | karta → 🔽 Szczegóły |
| Nie wiadomo, co firma zrobiła z inwestycją | 📋 Wynik: do sprawdzenia / sprawdzona / rozmowa / złożona oferta / niepasująca (+ powód) – jeden na osobę i inwestycję | Widać, co działa i czego brakuje | `/raport`: wyniki pracy i powody |
| Test mijał bez użycia | Jedna podpowiedź 48 h po starcie bez aktywacji (nie w nocy, nie w pauzie, wyłączalna); podsumowanie z liczb przed końcem | Więcej aktywacji w teście | `/raport`: „Podpowiedź po 48 h”, kohorta |

## 2. Oferta – ustaw w `.env` (repozytorium jest publiczne)

Na serwerze dopisz do `/opt/gunb-tool/.env` (wzór: `.env.example`) i zrestartuj bota (`systemctl restart gunb-bot`):

```bash
OFERTA_CENA=99                   # Twoja decyzja – 99 zł to tylko hipoteza pilotażu
OFERTA_PODATEK=bez_vat           # netto_vat | brutto | bez_vat – jak pokazać podatek (kod nic nie zakłada)
OFERTA_PLATNOSC=przelew – numer konta prześlę w wiadomości po zamówieniu
OFERTA_SPRZEDAWCA=Imię Nazwisko
OFERTA_KONTAKT=@twoj_nick
OFERTA_ODPOWIEDZ=w dni robocze 16:00–20:00
```

Dopóki brakuje ceny, sposobu podatku, płatności albo danych sprzedawcy, bot **nie pokazuje ceny ani przycisku
zamówienia** – tylko „💬 Zapytaj o ofertę” (pytanie trafia do Ciebie). `/dane` mówi, czego brakuje.

Błędna wartość (np. `OFERTA_CENA=99 zł` zamiast `99`) **nie zatrzymuje bota** – wyłącza tylko ofertę (jak wyżej).
Przy starcie dostajesz alert, a `/dane` pokazuje, którą zmienną poprawić; po poprawce zrestartuj bota.

## 3. Zamówienie i płatność krok po kroku

1. Klient klika **🛒 Zamawiam** (oferta, konto, przypomnienie przed końcem albo informacja po końcu).
   Dostaje numer (np. **Z-7**), kwotę, sposób płatności i zdanie „To nie jest faktura ani potwierdzenie płatności”.
2. Ty dostajesz kartę zamówienia. Dane do przelewu wyślij przez bota: `/napisz <chat_id> <tekst>`.
3. **Dopiero gdy pieniądze są na koncie**: przycisk **✅ Płatność otrzymana** albo `/zaplacone Z-7 przelew 12.10`.
   Dostęp przedłuża się o 30 dni od końca obecnego (dni testu nie przepadają), klient dostaje potwierdzenie.
   Drugie kliknięcie niczego nie przedłuża.
4. Pomyłka albo rezygnacja: **✖️ Anuluj** albo `/anuluj Z-7` (anulowanego nie da się opłacić).
5. Klient zapłacił **bez** „🛒 Zamawiam” (np. po rozmowie): `/wplata <chat_id> [uwagi]` – bot potwierdza jego
   otwarte zamówienie albo zakłada nowe z bieżącą ceną i od razu je potwierdza. Każde `/wplata` to jedna wpłata.
   `/aktywuj` zostaw na dostęp **bez** płatności (promocja, wyjątek) – inaczej `/raport` nie policzy płatności.

Bot nie wystawia faktur i nie przyjmuje pieniędzy – tylko zapisuje, co zamówiono i kiedy potwierdziłeś wpłatę.

## 4. Komendy admina

| Komenda | Działanie |
|---|---|
| `/zamowienia` | otwarte (czekają na płatność) i ostatnio opłacone |
| `/zaplacone <Z-nr> [uwagi]` · `/anuluj <Z-nr>` | płatność otrzymana (raz) · anulowanie |
| `/wplata <chat_id> [uwagi]` | płatność bez zamówienia w bocie – zakłada je z bieżącej oferty i potwierdza |
| `/napisz <chat_id> <tekst>` | wiadomość do osoby przez bota (np. dane do przelewu, odpowiedź na pytanie) |
| `/trial <chat_id>` | pozwala na 7-dniowy test (to samo co 🎁 na karcie prośby) |
| `/przedluztest <chat_id> <1–14> <powód>` | jednorazowe przedłużenie testu z zapisem powodu |
| `/aktywuj` · `/przedluz <chat_id> <dni\|data>` | dostęp nadany ręcznie (promocja, wyjątek) – **nie** płatność |
| `/firma <chat_id> <nazwa\|->` | firma osoby – raport liczy wtedy firmy, nie tylko konta |
| `/raport [7\|30\|90]` | lejek: źródła, kroki przed testem, wyniki, zamówienia, płatności, kohorty |
| `/dane` | co jest w bazie i czego brakuje (nazwy obszarów, lokalizacja, historia, oferta) |
| `/uzytkownicy` · `/status` · `/odbierz` | jak dotąd |

## 5. Pomiar

- **Zdarzenia** (tabela `zdarzenia`, retencja 13 miesięcy): `start` (+ źródło), `demo`, `oferta`, `prosba_o_test`,
  `pytanie_oferta`, `konfiguracja_start`, `konfiguracja`, `test_start`, `wyniki`, `pusto` (+ przyczyna),
  `szczegoly`, `zapis`, `wynik`, `wynik_powod`, `przydatne`/`nieprzydatne`, `podpowiedz_test`, `zamowienie`,
  `platnosc`, `odnowienie`, `dostep_przedluzony` (ręczny), `test_przedluzony`, `koniec_dostepu`.
- **Źródło**: link `t.me/ZoltaTablicaBot?start=<kod>` – zapisywane raz, przy pierwszym wejściu
  ([gotowe linki](TELEGRAM_PROFIL.md)).
- **Aktywacja (v1)**: w 48 h od startu testu ≥ 3 różne otwarte inwestycje i ≥ 1 zapisana albo oceniona
  pozytywnie (👍, „rozmowa”, „złożona oferta”). Definicja jest w `gunb_tool/funnel.py` i liczy się z surowych
  zdarzeń – nową wersję (np. `v2`) można wprowadzić bez zmiany historii.
- **Kohorty**: osoby, które zaczęły test w okresie raportu. Konwersje (aktywacja, zamówienie, płatność) tylko
  dla tych, których obserwacja (14 dni od startu) już minęła; trwające – osobno, poza mianownikiem.
- **Nie mierzymy**: odczytów wiadomości ani kliknięć w mapę – Telegram ich nie zgłasza. Wysłanie ≠ przeczytanie.

## 6. Wdrożenie

Ta wersja podnosi schemat bazy do **v12** (nowe kolumny i tabele, nic nie jest usuwane). Przy pierwszym starcie
bot sam robi kopię `backups/gunb_leads-przed-v12-*.sqlite`, a potem migruje – każdy krok w osobnej transakcji.

```bash
cd /opt/gunb-tool && git pull
.venv/bin/pip install -q -r requirements.txt -c requirements.lock
systemctl restart gunb-bot && sleep 5 && systemctl is-active gunb-bot
runuser -u gunb -- .venv/bin/python main.py --config config.yaml --zdrowie
```

- Dotychczasowi użytkownicy: dostęp, ustawienia, zapisane, notatki i przypomnienia zostają; nikt nie dostaje testu
  ani dostępu „przy okazji”. Dostęp nadany wcześniej przez `/aktywuj` dalej widnieje jako 💳 (nie wiadomo, czy był
  opłacony); nowe nadania ręczne – jako 🔑.
- Stare przyciski w historii czatu działają (kliknięcie „✅ Przejrzane” na starej karcie przełącza na widok „⋯ Więcej”).
- Wycofanie: zatrzymaj bota, odtwórz kopię `przed-v12`, wróć do poprzedniej wersji kodu (starszy kod nie
  wystartuje na bazie v12 – kod wyjścia 2). Zmiany z czasu po kopii przepadają.
- Migracja jest sprawdzona testami na bazach v6 i v11 oraz na kopii lokalnej bazy (`tests/test_sales_store.py`,
  `tests/test_pilot_verification.py`). Prawdziwej bazy nie migruj „na próbę” – najpierw kopia.

## 7. Ręczny odbiór (15 minut, drugie konto Telegram)

1. `/start strona` z drugiego konta → opis, obszar, „🙋 Chcę przetestować”, przykład oznaczony „dane fikcyjne”.
2. „🙋 Chcę przetestować” → na Twoim koncie karta prośby ze źródłem `strona` → **🎁 Test 7 dni**.
3. Drugie konto: „Co oferujesz?” → „Gdzie działasz?” → podsumowanie z zakresem danych, liczbą pasujących i datą
   sprawdzenia rejestru → **▶️ Zacznij 7-dniowy test** → 3–5 najlepiej dopasowanych.
4. Otwórz kartę (fakty / dlaczego / szacunki), **🔽 Szczegóły**, ⭐ Zapisz, 📝 notatka, 📋 Wynik → „rozmowa”.
5. `/konto` → cena i **🛒 Zamawiam** (po ustawieniu oferty) → Ty: karta zamówienia → `/zaplacone Z-1 test` →
   drugie konto: „✅ Płatność … potwierdzona”, `/konto`: dostęp do daty i numer zamówienia.
6. `/raport 30` i `/dane` – sprawdź liczby z tego, co przed chwilą zrobiłeś.
7. Osoba, która nie kupuje: po końcu testu raporty stoją, „⭐ Zapisane” i notatki są do wglądu, stare numery
   innych inwestycji nic nie pokazują.

## 8. Do uzupełnienia przez operatora

- `.env`: `OFERTA_CENA`, `OFERTA_PODATEK`, `OFERTA_PLATNOSC`, `OFERTA_SPRZEDAWCA`, `OFERTA_KONTAKT`
  (opcjonalnie `OFERTA_ODPOWIEDZ`, `OFERTA_ZASADY_URL`, `OFERTA_PRYWATNOSC_URL`).
- `strona/index.html`, `strona/zasady.html`, `strona/prywatnosc.html`: wszystkie pola „DO UZUPEŁNIENIA”
  (dane sprzedawcy, cena, płatność, miejsce serwera i kopii, czas przechowywania kopii). Strony to szkice –
  nie są zatwierdzonymi dokumentami prawnymi.
- Profil bota u BotFathera: [teksty](TELEGRAM_PROFIL.md).

Podgląd strony lokalnie: otwórz `strona/index.html` w przeglądarce (bez serwera, bez internetu poza linkiem do Telegrama).

## 9. Usunięcie osoby na jej prośbę

Bot nie przyjmuje takich próśb sam (odpowiada tylko na przyciski i komendy) – przychodzą do Ciebie. Jedno polecenie
na serwerze usuwa konto razem z ustawieniami, zapisanymi, notatkami, przypomnieniami, wynikami, zamówieniami,
zdarzeniami i kolejką wysyłek (`CHAT_ID` – numer z `/uzytkownicy`):

```bash
cd /opt/gunb-tool && runuser -u gunb -- .venv/bin/python -c "import sqlite3,sys; c=sqlite3.connect('data/gunb_leads.sqlite'); c.execute('PRAGMA foreign_keys=ON'); [c.execute(f'DELETE FROM {t} WHERE chat_id=?', (int(sys.argv[1]),)) for t in ('wysylki','zdarzenia','bot_users')]; c.commit()" CHAT_ID
```

`PRAGMA foreign_keys=ON` jest konieczne – bez niego (np. samo `DELETE FROM bot_users` w konsoli `sqlite3`) notatki,
zamówienia i reszta danych tej osoby zostałyby w bazie. Dane zostają jeszcze w kopiach bazy (`data/backups`, także
kopie `*-przed-v*` sprzed aktualizacji, i kopie poza serwerem), dopóki tych kopii nie usuniesz.
