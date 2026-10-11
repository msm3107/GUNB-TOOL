# Adapter Meta i granica webhooka — PR 5

## Cel i zakres

Kontynuacja zleconego wdrożenia e-mail/WhatsApp po scaleniu PR #10.
Baza: `a88cf7642a136de104de072e8c7a3dfe2fa3e6d7`. Istniejący Telegram,
aktywny e-mail, SQLite/WAL i schemat v14 zachowują swoje interfejsy.
Ten etap dodaje wewnętrzny adapter `Sender` oraz czysty parser podpisanych
statusów. `whatsapp.enabled: true` nadal kończy się `ConfigError` przed bazą.
Nie uruchamia HTTP, nie podłącza nadawcy do harmonogramu ani komend użytkownika.

## Moduły i przepływ

`whatsapp_models.py`: walidacja identyfikatorów, ograniczony ścisły JSON oraz
niemutowalny status bez surowego payloadu i błędów dostawcy.
`whatsapp_sender.py`: `WhatsAppSettings` i `WhatsAppSender.send(endpoint, report,
*, idempotency_key) -> DeliveryResult`, zgodne z istniejącym workerem.
`whatsapp_webhook.py`: `WhatsAppWebhookSettings`, `WhatsAppWebhook.challenge`
i `parse_statuses(raw_body, signature) -> tuple[WhatsAppStatus, ...]`.

Przepływ wysyłki: sprawdzenie własności/wersji/proof/zgody → walidacja szablonu
→ jeden POST do stałego `https://graph.facebook.com/{version}/{phone_id}/messages`
→ ograniczony odczyt → `DeliveryResult`. Sieć zawsze poza transakcją workera.
Przepływ webhooka: limit bajtów → HMAC surowych bajtów → ścisły JSON → właściwy
WABA/phone_id → do 100 typowanych statusów. Parser nie zapisuje nic w bazie.

## Szablon i treść

Operator podaje jawnie wersję API (`vN.0`), phone number ID, nazwę zatwierdzonego
szablonu, język i token. Brak domyślnej wersji, własnego hosta, URL ani fallbacku
do wiadomości sesyjnej. Token ma `repr=False`; przyszła konfiguracja użyje env.
Nazwa: małe ASCII, cyfry i `_`, do 512; język: dwie/trzy małe litery, opcjonalnie
`_` i dwie wielkie. ID: 1–32 cyfry, pierwsza niezerowa. Timeout 0.1–10 s.

Kontrakt szablonu: dokładnie dwa pozycyjne parametry tekstowe BODY: tytuł i
cała treść raportu. Białe znaki raportu są składane do pojedynczych spacji;
niepuste linie rozdziela ` | `. Bez obcinania faktów, dzielenia lub ponownego
budowania snapshotu podczas wysyłki. Łącznie maks. 900 znaków parametrów to
konserwatywny limit aplikacji, zostawiający miejsce na stałe zdania szablonu.
Operator musi zatwierdzić szablon z maksymalnymi przykładami, treścią rejestrową
„inwestycje do sprawdzenia” i działającą rezygnacją przed aktywacją. Nie jest to
deklaracja kwalifikacji UTILITY ani obietnica bezpłatnych wiadomości.
Niepasujący raport daje `failed` bez HTTP; przyszły planner musi zmieścić treść
PRZED enqueue. Token, numer, tytuł, body i odpowiedzi nie są logowane.

## Transport i wynik

Prywatna sesja `requests`, `trust_env=False`, TLS z weryfikacją, brak redirectów,
automatycznych retry, proxy z otoczenia i netrc. Odpowiedź strumieniowana do
16 KiB, budżet współpracujący 30 s, ograniczone connect/read timeouty. To nie
jest twardy limit DNS; nadzór procesu i lease muszą poprzedzić uruchomienie.
Sesja/odpowiedź zamykane także przy błędzie; cleanup nie zmienia wyniku ACK.

HTTP 200 z `messaging_product=whatsapp`, dokładnie jednym zgodnym kontaktem
i jednym poprawnym `wamid.` daje tylko `accepted`. Nigdy delivered/read.
Poprawny błąd Graph (numeryczny `error.code`, brak messages) dla 429 daje retry
z ograniczonym numerycznym Retry-After. Pozostałe jednoznaczne 4xx dają failed,
z wyjątkiem 408/409. 5xx, redirect, 408/409, wyjątki po rozpoczęciu HTTP,
uszkodzona/zbyt duża/niespójna odpowiedź dają unknown. Lokalna walidacja failed.
Brak samoczynnego ponowienia. `idempotency_key` jest walidowanym interfejsem
workera; nie wysyłamy nieudokumentowanego nagłówka ani nie obiecujemy idempotencji
Meta. Zgubiony ACK pozostaje unknown do rozliczenia operacyjnego.

## Granica bezpieczeństwa webhooka

App secret i verify token to oddzielne wartości (`repr=False`). POST wymaga
`sha256=` i 64 cyfr hex, HMAC-SHA256 po niezmienionym body, `compare_digest`.
Limit body 64 KiB, JSON UTF-8, bez duplikatów kluczy/NaN/Infinity i głębokości >20.
Niepoprawne dane zgłaszają ogólny `WebhookError`, bez surowych wartości/łańcucha.
GET: wyłącznie subscribe, poprawny verify token i cyfrowy challenge do 256 znaków.

Statusy wyłącznie z `object=whatsapp_business_account`, własnego WABA i numeru,
`field=messages`, `messaging_product=whatsapp`. Do 20 entries i 20 changes/entry,
do 100 statuses łącznie; przekroczenie odrzuca całość. Zdarzenia innych kont,
pól i przyszłe typy statusów ignorowane. Wiadomości przychodzące nie są komendami.
Obsługiwane sent→accepted, delivered, read, failed; wymagane wamid, kanoniczny
recipient_id i poprawny sekundowy timestamp UTC. Typ wyniku nie zachowuje
errors, pricing, contacts, body ani danych rozmów. ID/recipient ukryte w repr.
Deterministyczny hash event_key i deduplikacja wewnątrz paczki; podpis nie jest
ochroną przed replay. Trwała deduplikacja, monotoniczne przejścia i korelacja
z pierwotnym odbiorcą/wiadomością należą do następnego PR integracji inbox.

## Weryfikacja i kolejne etapy

TDD: błędna konfiguracja i snapshot bez sieci, transport TLS/proxy/redirect/retry,
limity odpowiedzi, statusy HTTP, niepewna wysyłka i cleanup, poufność logów/repr;
HMAC nad bajtami, błędny podpis przed parsowaniem, challenge, zakres konta,
malformed/duplikaty/depth/rozmiar, wszystkie statusy i replay w paczce.
Rzeczywisty SQLite + worker sprawdzają accepted/history i unknown/quarantine,
niezależność Telegrama/e-mail i brak transakcji przy HTTP. Pełny pytest,
CI Python 3.10–3.14/deploy-scripts, niezależne review całego PR z lokalnego promptu.
Wyłącznie dane syntetyczne i transporty testowe; bez prawdziwych wiadomości.

Następne etapy: trwały inbox/statusy/retencja; kontrolowany HTTPS z limitami
przed odczytem; weryfikacja własności numeru i osobna zgoda/rezygnacja;
krótsze raporty/harmonogram z nadzorem procesu; pilotaż operatora z rzeczywistym
kontem, zatwierdzonym szablonem i pomiarem kosztów. Brak nowej usługi w tym PR.

## Źródła sprawdzone 2026-10-10

Oficjalne przykłady Meta potwierdzają endpoint/Bearer/parametry BODY:
[message_helper.py](https://github.com/fbsamples/whatsapp-api-examples/blob/main/send-messages-flight-app-python/message_helper.py).
Przykład granicy HMAC/challenge:
[app.py](https://github.com/fbsamples/whatsapp-api-examples/blob/main/signature-validation-with-webhooks-payloads/app.py).
Nie kopiujemy jego porównania `!=`, logowania ani mylącego użycia TOKEN dla obu ról.
Typy odpowiedzi/statusów sprawdzono w oficjalnym, archiwalnym SDK:
[messages.ts](https://github.com/WhatsApp/WhatsApp-Nodejs-SDK/blob/main/src/types/messages.ts),
[webhooks.ts](https://github.com/WhatsApp/WhatsApp-Nodejs-SDK/blob/main/src/types/webhooks.ts).
Portal developers.facebook.com zwrócił HTTP 429; nie potwierdzono najnowszej
wersji, aktualnego cennika ani kategorii konkretnego szablonu. Te punkty pozostają
bramką operatora przed przyszłą aktywacją, a nie założeniem produkcyjnym.
