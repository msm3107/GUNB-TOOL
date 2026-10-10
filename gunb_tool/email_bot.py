"""Owned mailbox commands. The interactive bot never calls SMTP."""

from __future__ import annotations

from collections import deque
import html
import threading
import time

from .bot_store import MODES
from .email_verification import EmailVerification
from .notification_models import parse_time
from .notification_store import NotificationStore, normalize_address


class VerificationRequests:
    """Volatile, bounded IDs only; one queued/in-flight request per owner."""

    def __init__(self, *, _clock=time.monotonic):
        self._clock, self._lock = _clock, threading.Lock()
        self._queue = deque()
        self._pending = set()

    def _expire(self):
        while self._queue and self._queue[0][2] <= self._clock():
            chat_id, _, _ = self._queue.popleft()
            self._pending.discard(chat_id)

    def submit(self, chat_id: int, endpoint_id: int) -> bool:
        with self._lock:
            self._expire()
            if chat_id in self._pending or len(self._pending) >= 25:
                return False
            self._pending.add(chat_id)
            self._queue.append((chat_id, endpoint_id, self._clock() + 60))
            return True

    def take(self) -> tuple[int, int] | None:
        with self._lock:
            self._expire()
            if not self._queue:
                return None
            chat_id, endpoint_id, _ = self._queue.popleft()
            return chat_id, endpoint_id

    def done(self, chat_id: int) -> None:
        with self._lock:
            self._pending.discard(chat_id)


class _ConsumeOnly:
    def send_verification(self, *args, **kwargs):
        raise RuntimeError('Weryfikację wysyła wyłącznie osobny worker')


HELP = ('E-mail: /email ustaw ADRES, /email ponow, /email potwierdz KOD.\n'
        'Po potwierdzeniu: /email zgoda — zgadzam się na raporty o inwestycjach według moich filtrów.\n'
        'Tryb: /email tryb rano|wieczor|natychmiast.\n'
        'Rezygnacja: /email wylacz. Usunięcie adresu i jego historii: /email usun.\n'
        'Potwierdzenie skrzynki samo nie włącza raportów.')
GENERIC = 'Nie udało się wykonać operacji. Sprawdź polecenie w /email i spróbuj ponownie.'


class EmailCommands:
    def __init__(self, store: NotificationStore, requests: VerificationRequests, send):
        self.store, self.requests, self.send = store, requests, send
        self.verification = EmailVerification(store, _ConsumeOnly())

    def _allowed(self, user) -> bool:
        if user.status != 'aktywny' or user.wstrzymane:
            return False
        if self.store.access == 'open' or user.chat_id in self.store.admins or user.bez_limitu:
            return True
        try:
            return user.is_active and parse_time(user.subscription_ends or '') > self.store.repo.now()
        except (TypeError, ValueError):
            return False

    def handle(self, user, args) -> None:
        if user is None:
            return
        try:
            # Fresh access state; do not authorize using a stale BotUser snapshot.
            current = self.store._bot.get_user(user.chat_id)
            reply = self._handle(current, args) if current is not None else GENERIC
        except Exception:
            reply = GENERIC  # No address, token, SQL error or provider diagnostic in logs/replies.
        self.send(user.chat_id, reply)

    def _handle(self, user, args) -> str:
        endpoints = self.store.list_endpoints(user.chat_id)
        if len(endpoints) > 1:
            return 'Konfiguracja adresów wymaga sprawdzenia przez administratora.'
        endpoint = endpoints[0] if endpoints else None
        verb = args[0].lower() if args else ''
        if not args:
            if endpoint is None:
                return 'Nie ustawiono adresu e-mail.\n' + HELP
            mask = '***@' + endpoint.address.split('@')[1]
            state = 'włączone' if endpoint.enabled else 'wyłączone'
            proof = 'potwierdzony' if endpoint.verified_at else 'niepotwierdzony'
            warning = ('\nWysyłka wymaga sprawdzenia przez administratora.'
                       if self.store.has_unresolved(user.chat_id, endpoint.id) else '')
            return f'E-mail: {html.escape(mask)}, {proof}, raporty {state}, tryb {endpoint.mode}.{warning}\n' + HELP
        if verb in ('wylacz', 'usun') and len(args) == 1:
            if endpoint:
                if verb == 'usun':
                    self.store.delete_endpoint(user.chat_id, endpoint.id)
                else:
                    self.store.revoke_consent(user.chat_id, endpoint.id)
            return ('Adres usunięty.' if verb == 'usun' else 'Zgoda wycofana, raporty wyłączone.') + (
                ' Wiadomość, której wysyłka już się rozpoczęła, może jeszcze dotrzeć.')
        if not self._allowed(user):
            return 'Zmiana ustawień e-mail wymaga aktywnego, niewstrzymanego dostępu. Rezygnacja: /email wylacz.'
        if verb == 'ustaw' and len(args) == 2:
            address = normalize_address('email', args[1])
            endpoint = (self.store.change_address(user.chat_id, endpoint.id, address) if endpoint
                        else self.store.add_endpoint(user.chat_id, 'email', address))
            if endpoint.verified_at:
                return 'Adres jest już potwierdzony. Ustawienia i rezygnacja: /email.'
            return self._request(endpoint)
        if endpoint is None:
            return 'Najpierw ustaw adres: /email ustaw ADRES.'
        if verb == 'ponow' and len(args) == 1:
            return self._request(endpoint)
        if verb == 'potwierdz' and len(args) == 2:
            if self.verification.consume(user.chat_id, endpoint.id, args[1]):
                return 'Adres potwierdzony. Raporty wymagają osobnej zgody: /email zgoda. Rezygnacja: /email wylacz.'
            return 'Nie potwierdzono adresu. Sprawdź kod i jego ważność; ponowienie: /email ponow.'
        if verb == 'zgoda' and len(args) == 1:
            if self.store.has_unresolved(user.chat_id, endpoint.id):
                return 'Przed wznowieniem wysyłki potrzebne jest sprawdzenie przez administratora.'
            if not endpoint.verified_at:
                return 'Najpierw potwierdź adres kodem z wiadomości.'
            if not endpoint.enabled:
                endpoint = self.store.record_consent(user.chat_id, endpoint.id, expected_version=endpoint.version,
                                                     source='telegram:/email zgoda:v1')
                self.store.set_enabled(user.chat_id, endpoint.id, True, expected_version=endpoint.version)
            return 'Zgoda zapisana, raporty e-mail włączone. Obejmą nowe zmiany po aktywacji. Rezygnacja: /email wylacz.'
        if verb == 'tryb' and len(args) == 2 and args[1] in MODES:
            self.store.set_mode(user.chat_id, endpoint.id, args[1], expected_version=endpoint.version)
            return 'Tryb e-mail zapisany. Niepotwierdzony adres może wymagać nowego kodu: /email ponow.'
        return GENERIC

    def _request(self, endpoint) -> str:
        if endpoint.verified_at or endpoint.enabled:
            return 'Adres jest już potwierdzony. Ustawienia i rezygnacja: /email.'
        self.requests.submit(endpoint.chat_id, endpoint.id)
        return ('Zlecenie weryfikacji przyjęte do obsługi. Jeśli limity i dostęp pozwolą, kod otrzymasz e-mailem. '
                'Kod jest ważny 15 minut. Gdy nie dotrze, odczekaj minutę i użyj /email ponow. '
                'Samo zlecenie nie włącza raportów.')
