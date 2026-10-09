# SMTP i weryfikacja adresu — PR 3

## Cel i decyzja

Kontynuacja zaakceptowanego wdrożenia e-mail/WhatsApp po merge PR #8.
Baza `f197bda2162849db95714a11720ec438b6856c9c`, gałąź `codex/email-smtp`.
Standardowa biblioteka Python >=3.10, dotychczasowy worker i SQLite/WAL.
Adapter SMTP i wewnętrzne request/consume weryfikacji; brak podłączenia do bota,
CLI, HTTP i harmonogramu. Flagi nadal wyłączone; true nadal ConfigError.

Alternatywy: token stateless nie daje samodzielnie trwałych limitów i jednorazowości;
użycie tabeli webhooków dla tokenów miesza odpowiedzialności. Wybór: jedna nowa
tabela w v14, przez istniejące migracje i kopię sprzed aktualizacji. V13 nie zmieniać.
Powrót do kodu v13 wymaga odtworzenia kopii, z utratą zmian od jej utworzenia.

## Adapter SMTP

`email_sender.py`: frozen `SMTPSettings(host, port, from_address, tls='starttls',
username='', password='', timeout_seconds=5)` oraz `SMTPEmailSender(settings)`.
Host i port wyłącznie z zaufanej konfiguracji kodu. Bez URL/sterowania w host,
bez jawnego transportu. STARTTLS z EHLO po negocjacji albo implicit TLS;
`ssl.create_default_context`, kontrola certyfikatu/hosta, brak fallbacku plaintext.
Login dopiero po TLS, debug wyłączony. Sekrety/adres nadawcy ukryte w repr.

`send(endpoint, report, *, idempotency_key) -> DeliveryResult` spełnia Sender PR 2.
`send_verification(endpoint, token, *, expires_at, idempotency_key)` wysyła wyłącznie
wiadomość o potwierdzeniu skrzynki, bez inwestycji i bez zgody na marketing.
Jedna koperta MAIL/RCPT, jeden To, bez Cc/Bcc, tekst UTF-8, standardowy EmailMessage.
Subject jednowierszowy; adresy kanoniczne ASCII. Stabilny Message-ID ze skrótu klucza
i domeny nadawcy. To identyfikator nagłówka aplikacji, nie queue ID dostawcy i nie
gwarancja exactly-once. Date może odpowiadać dacie próby; treść/temat raportu są frozen.

Operacje SMTP są jawne: MAIL, RCPT, DATA. Brak DATA = brak przyjęcia wiadomości.
Jawne odpowiedzi 4xx -> retry, 5xx -> failed; końcowe 250 DATA -> accepted.
Utrata połączenia/timeout w DATA -> unknown. Błąd certyfikatu lub brak TLS -> failed.
Błąd cleanup nie nadpisuje odebranego wyniku. Żadnych logów surowego błędu, body,
adresu, tokenu lub credentials. Socket timeout 0.1–10 s; sprawdzany budżet sesji 45 s
między poleceniami, dopasowanie socket timeout do pozostałego budżetu. Nie obiecujemy
twardego przerwania DNS ani funkcji biblioteki w środku polecenia. Integracja musi
dobrać lease i nadzór czasu; PR 2 nadal bezpiecznie kwarantannuje wygasłe sending.

## Trwała weryfikacja

`email_verification_schema.py`: migracja v14, tabela `email_verifications`:
id, nullable chat_id (FK bot_users SET NULL), nullable endpoint_id (FK endpoint SET NULL),
endpoint_version, address_digest, token_digest UNIQUE, issued_at, expires_at,
consumed_at, invalidated_at, attempts (0–5). Indeksy endpoint, chat/czas,
digest_adresu/czas, issued_at. SET NULL zachowuje limit adresu/globalny po usunięciu
endpointu/konta. Skróty są nadal metadanymi wrażliwymi, nie anonimizacją.

`EmailVerification(store, sender).request(chat_id, endpoint_id) -> DeliveryResult | None`
i `consume(chat_id, endpoint_id, token) -> bool`. Wszystkie zapisy wymagają
połączenia bez zewnętrznej transakcji; BEGIN IMMEDIATE chroni limity i zużycie.
Własny, wyłączony, niezweryfikowany endpoint email, konto aktywne, bez pauzy,
dostęp admin/open/bez_limitu albo przyszły test/abonament. Brak enumeracji obcych
endpointów; denied request zwraca None, consume False. UI przyszłego PR musi dawać
jednakowy komunikat o żądaniu, bez ujawniania wyniku SMTP lub istnienia skrzynki.

Token `secrets.token_urlsafe(32)` (43 znaki) trafia tylko do maila, nie do zwrotki API,
bazy, repr ani logów. SHA-256 tokenu w bazie; compare_digest przy consume. Związanie
z owner/endpoint/version i skrótem adresu. Ponowne żądanie unieważnia wcześniejszy
token endpointu. Wygasa po 900 s, maks. 5 błędnych prób prawidłowego formatu.
Consume atomowo zapisuje consumed_at i verified_at. Nie zmienia zgody, version
ani enabled. Zmiana adresu/zgody/wersji, usunięcie i ponowne utworzenie endpointu,
przeterminowanie i powtórne użycie nie potwierdzają adresu.

Limity stałe: min. 60 s pomiędzy żądaniami ownera lub adresu; maks. 3/h dla ownera
i adresu, 100/h globalnie. Adres na potrzeby limitu lower-case w całości, aby zmiana
wielkości liter local-part nie omijała limitu (kanoniczny odbiorca zachowuje local-part).
Limity obejmują także próby wysyłki nieprzyjęte; generowanie/INSERT/limit atomowe.
Provider wywoływany poza transakcją. Retry/failed unieważnia token, unknown zostawia
możliwość potwierdzenia otrzymanego maila. Brak automatycznego retry weryfikacji;
po crashu można ponowić po limicie z nowym tokenem. Treść tokenu nie jest utrwalana.
Retencja 7 dni, usuwanie do 200 najstarszych rekordów na request. Dane limitujące
z ostatniej godziny pozostają; timestamp future nie może ułatwiać obejścia limitów.

## Weryfikacja i dalsze kroki

Testy wyłącznie fake SMTP/FakeSender i tymczasowe bazy. TLS przed AUTH, brak Bcc,
CRLF, MIME, stabilny klucz, wszystkie klasy odpowiedzi przed/w DATA, cleanup,
budżet i nieujawnianie danych. Upgrade v13 z niepustym bot/outbox, kopia/rollback
migracji, restart/odmowa starego kodu; historyczne testy v13 pozostają przypięte do v13.
Ownership, dostęp, TTL, replay, wersja/adres, próby, limity po restarcie/usunięciu,
retencja i konkurencyjne request/consume. Cały pytest i CI 3.10–3.14 + deploy-scripts.

Przed uruchomieniem: konfiguracja SMTP/secrets z istniejącym maskowaniem i eksportem,
UI opt-in i rezygnacja, integracja harmonogramu/TTL/ciszy nocnej, nadzór czasu SMTP,
operacyjne unknown/failed, retencja outbox i pomiar PERF-01 z PR #8. Weryfikacja
skrzynki nie jest zgodą na kontakt z inwestorami GUNB ani dowodem doręczania raportów.

Źródła techniczne sprawdzone 2026-10-09:
[smtplib](https://docs.python.org/3/library/smtplib.html),
[SSLContext](https://docs.python.org/3/library/ssl.html#ssl.create_default_context).
