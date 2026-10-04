# Profil bota w Telegramie – gotowe teksty

Ustaw je ręcznie u [@BotFather](https://t.me/BotFather). Bot **nie zmienia** swojego profilu przez API (sam ustawia
tylko listę komend pod „/”).

| Pole | Polecenie u BotFathera | Limit Telegrama |
|---|---|---|
| Nazwa | `/setname` | 64 znaki |
| Krótki opis (profil, udostępnienia) | `/setabouttext` | 120 znaków |
| Opis (ekran przed „Start”) | `/setdescription` | 512 znaków |

## Nazwa

<!-- nazwa -->
Żółta Tablica
<!-- /nazwa -->

## Krótki opis

<!-- about -->
Nowe budowy z Warmii i Mazur – codziennie z publicznego rejestru GUNB. Pilotaż z 7-dniowym testem.
<!-- /about -->

## Opis

<!-- description -->
Żółta Tablica codziennie sprawdza publiczny rejestr pozwoleń na budowę i zgłoszeń (GUNB) i pokazuje nowe inwestycje z województwa warmińsko-mazurskiego: rodzaj budynku, miejscowość, datę decyzji i mapę. Dla składów budowlanych, handlowców i wykonawców. To lista budów do sprawdzenia – nie zamówienia i nie kontakty do inwestorów. Kliknij Start: zobaczysz przykład i możesz poprosić o 7-dniowy test.
<!-- /description -->

## Linki z oznaczeniem źródła

Bot zapisuje krótki kod źródła z linku (tylko przy pierwszym wejściu; dozwolone: małe litery, cyfry, `_`, `-`,
do 32 znaków, zaczyna się literą; inne wartości – w tym numery telefonów – zapisuje jako `inne`):

| Gdzie | Link |
|---|---|
| strona – główny przycisk | `https://t.me/ZoltaTablicaBot?start=strona` |
| strona – sekcja oferty | `https://t.me/ZoltaTablicaBot?start=strona_oferta` |
| strona – stopka | `https://t.me/ZoltaTablicaBot?start=strona_dol` |
| ulotka / wizytówka | `https://t.me/ZoltaTablicaBot?start=ulotka` |
| rozmowa osobista | `https://t.me/ZoltaTablicaBot?start=polecenie` |

`/raport 30` pokazuje nowe konta według źródła.
