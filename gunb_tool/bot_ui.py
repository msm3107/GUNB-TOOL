"""Teksty i przyciski bota – „proste jak drut”: krótkie zdania, duże przyciski, zero pisania komend.

Wszystkie funkcje są czyste (zwracają tekst HTML i/lub słownik ``reply_markup``), więc łatwo je testować
i zmieniać bez dotykania logiki bota.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Callable, Sequence

from .bot_store import BotUser, JobStatus, LeadFlags, Send, UserFilters, WatchItem
from .clock import local
from .config import BotConfig
from .exporter import CATEGORY_ICONS, escape_html
from .models import Investment
from .scoring import HOT
from .stages import TRADES, Trade, get_trade

Markup = dict[str, Any]
Distance = Callable[[Investment], float | None]

MENU_BUTTONS: tuple[str, ...] = (
    "📊 Inwestycje", "⭐ Zapisane",
    "⚙️ Ustawienia", "❓ Pomoc",
)
LEGACY_MENU_BUTTONS: tuple[str, ...] = (
    "📊 Co nowego?", "🔎 Filtry", "👀 Obserwowane", "⏰ Kiedy wysyłać", "🔥 Tylko HOT", "📍 Blisko mnie",
)
"""Przyciski poprzedniego menu – kto ma je jeszcze na ekranie, może z nich dalej korzystać."""
CANCEL_BUTTON = "↩️ Anuluj"
SEND_LOCATION_BUTTON = "📍 Wyślij moją lokalizację"

BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("nowe", "📊 Inwestycje – nowe i z ostatnich 30 dni"),
    ("zapisane", "⭐ Zapisane inwestycje"),
    ("ustawienia", "⚙️ Obszar, rodzaj, branża, godziny raportów"),
    ("konto", "👤 Twój dostęp – do kiedy"),
    ("pomoc", "❓ Jak to działa"),
)
"""Menu pod „/”; starsze komendy (/filtry, /blisko, /branza, /obserwowane, /tryb, /tylkohot) nadal działają."""

CATEGORY_CHOICES: tuple[tuple[str, str], ...] = (
    ("mieszkaniowa-jednorodzinna", "🏠 Domy jednorodzinne"),
    ("mieszkaniowa-wielorodzinna", "🏢 Bloki / wielorodzinne"),
    ("mieszana", "🏘️ Mieszkalno-usługowe"),
    ("komercyjna", "🏭 Hale, sklepy, biura"),
    ("publiczna", "🏫 Szkoły, szpitale, urzędy"),
    ("rolnicza", "🌾 Rolnicze"),
    ("inna", "🔧 Inne"),
)
VOLUME_CHOICES: tuple[int, ...] = (0, 1000, 3000, 10000, 30000)
RADIUS_CHOICES: tuple[int, ...] = (10, 15, 20, 30, 50)
DEFAULT_RADIUS_KM = 15
MODE_LABELS: dict[str, str] = {"natychmiast": "⚡ Od razu", "rano": "🌅 Raport rano", "wieczor": "🌙 Raport wieczorem"}
SAVED_PAGE_SIZE = 10


# --- Klawiatury ----------------------------------------------------------------------------

def menu_keyboard() -> Markup:
    """Stałe menu na dole ekranu – duże przyciski zamiast komend."""
    rows = [MENU_BUTTONS[i:i + 2] for i in range(0, len(MENU_BUTTONS), 2)]
    return {"keyboard": [[{"text": label} for label in row] for row in rows],
            "resize_keyboard": True, "is_persistent": True}


def inline(rows: Sequence[Sequence[tuple[str, str]]]) -> Markup:
    """Klawiatura inline z przyciskami ``(tekst, callback_data)``."""
    return {"inline_keyboard": [[{"text": text, "callback_data": data} for text, data in row] for row in rows if row]}


def number_buttons(numbers: Sequence[tuple[int, int]], per_row: int = 5) -> list[list[tuple[str, str]]]:
    """Przyciski „1 2 3…” otwierające szczegóły leadów: ``(numer na liście, nr leada)``."""
    buttons = [(str(position), f"o:{nr}") for position, nr in numbers]
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


# --- Powitanie, pomoc, dostęp -----------------------------------------------------------------

def welcome_text(name: str | None) -> str:
    who = f", {escape_html(name)}" if name else ""
    return (
        f"👷 Cześć{who}! Tu <b>Żółta Tablica</b> – pokazuję <b>nowe pozwolenia na budowę</b> z Twojej okolicy "
        "(dane z rejestru GUNB).\n\n"
        "Na dole ekranu masz cztery przyciski 👇\n"
        "📊 <b>Inwestycje</b> – nowe budowy pasujące do Twoich ustawień\n"
        "⭐ <b>Zapisane</b> – Twoja lista ciekawych inwestycji\n"
        "⚙️ <b>Ustawienia</b> – obszar, rodzaj budynków, branża, godziny raportów, konto\n"
        "❓ <b>Pomoc</b> – jak to działa"
    )


def help_text(settings: BotConfig, *, admin: bool = False) -> str:
    admin_part = ("\n\n👑 <b>Admin</b>: /aktywuj &lt;chat_id&gt; &lt;dni|data&gt; · /przedluz · /odbierz · "
                  "/trial &lt;chat_id&gt; · /nowymodel · /uzytkownicy · /status" if admin else "")
    return _help_body(settings) + admin_part


def _help_body(settings: BotConfig) -> str:
    return (
        "❓ <b>Jak to działa</b>\n\n"
        "Codziennie sprawdzam publiczny rejestr pozwoleń na budowę (GUNB) i pokazuję inwestycje pasujące "
        "do Twoich ustawień. Pod każdym raportem widać, kiedy rejestr był ostatnio sprawdzony.\n\n"
        "📊 <b>Inwestycje</b> – nowe od ostatniego raportu; gdy nowych brak – przegląd ostatnich 30 dni\n"
        "⭐ <b>Zapisane</b> – Twoja lista (⭐ Zapisz pod inwestycją)\n"
        "⚙️ <b>Ustawienia</b> – obszar i rodzaj budynków, branża, obserwowani inwestorzy i gminy, "
        f"godziny raportów (rano {settings.morning_time}, wieczorem {settings.evening_time} albo od razu), konto\n\n"
        "<b>Co oznaczają oznaczenia</b>\n"
        "🔥 HOT / 🟡 NORMAL / ⚪ LOW – szacunek skali inwestycji według prostych reguł (kubatura, rodzaj "
        "budynku, liczba budynków); nie mówi, czy zdobędziesz zlecenie.\n"
        "📏 Odległość od Twojej bazy liczę w linii prostej – droga bywa dłuższa.\n"
        "🧰 Przypomnienia o etapie budowy to szacunek na podstawie daty decyzji – rzeczywisty etap wymaga "
        "sprawdzenia na miejscu.\n"
        "🗺️ „Lokalizacja przybliżona” – rejestr nie podał dokładnej działki, pokazuję środek obrębu.\n"
        "💼 Inwestorów prywatnych rejestr nie ujawnia; bot nie ma ich danych kontaktowych."
    )


def rejected_text() -> str:
    return "⛔ Administrator nie przyznał dostępu do bota."


# --- Abonament (paywall) i panel admina --------------------------------------------------------------

TRIAL_DAYS = 7
DEFAULT_PAID_DAYS = 30
TRIAL_TEXT = ("🎁 Aktywowano darmowy okres próbny na 7 dni! Zobacz, jak szybciej docierać do klientów. "
              "Po tym czasie bot zostanie wstrzymany.")
START_TRIAL_BUTTON = ("▶️ Zacznij 7-dniowy test", "ts")


def admin_contact_html(contact: str, admins: Sequence[int]) -> str:
    """„administratorem @nick” albo – bez nicku w konfiguracji – klikalna wzmianka admina po ID."""
    if contact:
        return f"administratorem {escape_html(contact)}"
    if admins:
        return f'<a href="tg://user?id={admins[0]}">administratorem</a>'
    return "administratorem"


def gate_text(contact_html: str, expired_on: str | None = None) -> str:
    """Komunikat dla osoby bez aktywnego abonamentu (także przy każdej próbie użycia menu)."""
    if expired_on:
        return f"⛔ Twój abonament wygasł {expired_on}. Skontaktuj się z {contact_html}, aby go przedłużyć."
    return f"⛔ Twój dostęp jest nieaktywny. Skontaktuj się z {contact_html}, aby opłacić abonament."


def new_user_card(user: BotUser) -> tuple[str, Markup]:
    """Wiadomość do admina o nowej osobie – z gotowymi komendami i przyciskami."""
    login = f" (@{escape_html(user.username)})" if user.username else ""
    text = (f"🆕 Nowa osoba: <b>{escape_html(user.imie or str(user.chat_id))}</b>{login}\n"
            f"ID: <code>{user.chat_id}</code>\n"
            f"Dostęp: /trial {user.chat_id} (7 dni testu – ruszą, gdy klient kliknie ▶️) "
            f"albo /aktywuj {user.chat_id} 30")
    return text, inline([[("🎁 Test 7 dni", f"adm:trial:{user.chat_id}"),
                          ("✅ 30 dni", f"adm:ok:{user.chat_id}"), ("⛔ Odrzuć", f"adm:no:{user.chat_id}")]])


def activated_text(ends_on: str, name: str | None, *, days: int | None = None) -> str:
    head = f"✅ Twój abonament został aktywowany na {days} dni!" if days else "✅ Twój abonament został aktywowany!"
    return f"{head}\nWażny do <b>{ends_on}</b>.\n\n" + welcome_text(name)


def trial_offer(*, setup_done: bool = True) -> tuple[str, Markup | None]:
    """Admin pozwolił na test – osoba sama decyduje, kiedy go zacząć (po ustawieniu branży i obszaru)."""
    text = ("🎁 Możesz wypróbować Żółtą Tablicę przez <b>7 dni za darmo</b>.\n"
            "Test ruszy dopiero wtedy, gdy klikniesz <b>▶️ Zacznij</b>")
    if not setup_done:
        return text + " – najpierw dwa krótkie pytania 👇", None
    return text + ".", inline([[START_TRIAL_BUTTON]])


# --- Pierwsze kroki (branża → obszar → gotowe) ----------------------------------------------------------

SETUP_RESUME_BUTTON = ("⚙️ Dokończ ustawienia", "ob:resume")


def setup_trade_step() -> tuple[str, Markup]:
    rows = [[(trade.label, f"ob:{trade.key}")] for trade in TRADES]
    rows.append([("🏗️ Inna branża / wszystkie etapy", "ob:none")])
    return ("<b>1/2</b> 🧰 <b>Czym się zajmujesz?</b>\n"
            "Dam znać, gdy budowa wejdzie w orientacyjne okno Twojego etapu (szacunek od daty decyzji).",
            inline(rows))


def setup_area_step(options: Sequence[tuple[str, str]], region: str) -> tuple[str, Markup]:
    rows = [[(f"📌 {label}", f"oa:p:{code}")] for code, label in options]
    rows += [[("📍 W promieniu od mojej bazy", "oa:loc")], [("✏️ Wpisz miejscowość", "oa:txt")],
             [("🗺️ Cały monitorowany obszar", "oa:all")]]
    return ("<b>2/2</b> 📍 <b>Gdzie szukać inwestycji?</b>\n"
            f"Monitoruję: {escape_html(region)}.\n"
            "Wybierz powiat albo wpisz miejscowość – bieżącej lokalizacji nie trzeba udostępniać.", inline(rows))


def setup_summary(user: BotUser, place: str, settings: BotConfig, *, can_start_trial: bool) -> tuple[str, Markup | None]:
    trade = get_trade(user.branza)
    lines = ["✅ <b>Gotowe!</b>",
             f"🧰 Branża: {escape_html(trade.label) if trade else 'bez przypomnień o etapach budowy'}",
             f"📍 Obszar: {escape_html(place)}",
             f"⏰ Raport: {_mode_sentence(user.tryb, settings)} (zmienisz w ⚙️ Ustawienia)"]
    if can_start_trial:
        lines += ["", "▶️ Kliknij, aby zacząć 7-dniowy test – od tej chwili liczy się 7 dni."]
        return "\n".join(lines), inline([[START_TRIAL_BUTTON]])
    return "\n".join(lines), None


def first_review_head() -> list[str]:
    return ["🔎 <b>Na początek – przegląd ostatnich 30 dni.</b>",
            "To historia z rejestru, nie nowości: nowe inwestycje przyjdą w raporcie."]


def place_unknown_text(name: str, region: str) -> str:
    return (f"🤔 W monitorowanym obszarze ({escape_html(region)}) nie ma inwestycji z miejscowości "
            f"„{escape_html(name)}”. Bot pokazuje tylko ten obszar – wpisz inną nazwę albo wybierz powiat "
            "(⚙️ Ustawienia → 🔎 Obszar i rodzaj).")


def base_far_text(km: float | None, region: str) -> str:
    where = f"najbliższa znana inwestycja jest ok. {km:.0f} km stąd (w linii prostej)" if km is not None \
        else "nie mam jeszcze inwestycji z lokalizacją"
    return (f"⚠️ Ta baza jest poza monitorowanym obszarem ({escape_html(region)}) – {where}. "
            "W wybranym promieniu nic nie znajdę; wybierz powiat albo zwiększ promień.")


def place_label(filters: UserFilters, place_names: dict[str, str]) -> str:
    """Obszar słowami: promień od bazy, wybrane powiaty i miejscowości albo cały monitorowany obszar."""
    if filters.radius_active:
        return f"do {filters.promien_km} km od Twojej bazy"
    places = [place_names.get(code, code) for code in filters.powiaty] + list(filters.miejsca)
    return ", ".join(places) if places else "cały monitorowany obszar"


# --- Ustawienia -------------------------------------------------------------------------------------

def settings_screen(user: BotUser, *, place: str, settings: BotConfig, watch_count: int,
                    account: str) -> tuple[str, Markup]:
    categories = [label for key, label in CATEGORY_CHOICES if key in user.filtry.kategorie]
    lines = ["⚙️ <b>Ustawienia</b>",
             f"📍 Obszar: {escape_html(place)}",
             f"🏗️ Rodzaj: {escape_html(', '.join(categories)) if categories else 'wszystkie'}"
             f" · 📦 {_volume_label(user.filtry.min_kubatura)}",
             f"🧰 Branża: {_trade_label(get_trade(user.branza))}",
             f"⏰ Raporty: {_mode_label(user.tryb, settings)}",
             f"👀 Obserwowane: {watch_count}",
             f"👤 Dostęp: {account}",
             "", "Kliknij, co chcesz zmienić 👇"]
    rows = [[("🔎 Obszar i rodzaj", "st:f"), ("🧰 Branża", "st:b")],
            [("👀 Obserwowane", "st:w"), ("⏰ Harmonogram", "st:m")],
            [("👤 Konto", "st:k")]]
    return "\n".join(lines), inline(rows)


def trial_started_text(ends_on: str) -> str:
    return f"{TRIAL_TEXT}\nTest trwa do <b>{ends_on}</b>."


def trial_waiting() -> tuple[str, Markup]:
    """Test dozwolony, ale jeszcze nie ruszył – a osoba chce zobaczyć inwestycje."""
    return ("▶️ Twój 7-dniowy test jeszcze się nie zaczął. Kliknij, kiedy chcesz go zacząć – "
            "od tej chwili liczy się 7 dni.", inline([[START_TRIAL_BUTTON]]))


def access_reminder_text(ends_on: str, *, trial: bool, contact_html: str) -> str:
    what = "Twój darmowy test" if trial else "Twój abonament"
    return (f"⏳ {what} kończy się <b>{ends_on}</b>. Jeśli chcesz dalej dostawać inwestycje, "
            f"skontaktuj się z {contact_html}.")


def access_ended_text(ends_on: str, *, trial: bool, contact_html: str) -> str:
    """Informacja o końcu dostępu – jednorazowa i przy każdej próbie użycia danych."""
    what = "Twój darmowy test skończył się" if trial else "Twój abonament wygasł"
    return (f"⛔ {what} {ends_on}. Raporty i przypomnienia są wstrzymane, a zapisane inwestycje i ustawienia "
            f"czekają na Ciebie. Skontaktuj się z {contact_html}, aby przedłużyć dostęp.")


def access_revoked_text(contact_html: str) -> str:
    return (f"⛔ Twój dostęp do Żółtej Tablicy został wyłączony. Zapisane inwestycje i ustawienia zostają. "
            f"Jeśli to pomyłka, skontaktuj się z {contact_html}.")


def access_term_text(ends_on: str) -> str:
    """Dotychczasowy użytkownik przełączony na nowy model – dostaje termin zamiast „bez limitu”."""
    return (f"ℹ️ Zmieniamy zasady dostępu do Żółtej Tablicy: Twój dostęp jest ważny do <b>{ends_on}</b>. "
            "Wszystko działa jak dotąd.")


def account_text(*, state: str, ends_on: str | None, contact_html: str) -> str:
    """Ekran „👤 Konto”: jaki dostęp, do kiedy i jak przedłużyć."""
    lines = {
        "admin": "👑 Jesteś administratorem – pełny dostęp bez limitu.",
        "open": "✅ Pełny dostęp (bot otwarty dla wszystkich).",
        "bez_limitu": "♾️ Pełny dostęp bez terminu (dotychczasowy użytkownik).",
        "test": f"🎁 Darmowy test trwa do <b>{ends_on}</b>.",
        "platny": f"💳 Abonament ważny do <b>{ends_on}</b>.",
        "test_dostepny": "🎁 Czeka na Ciebie 7-dniowy darmowy test – ruszy, gdy klikniesz ▶️ Zacznij.",
        "test_koniec": f"⌛ Darmowy test skończył się {ends_on}.",
        "platny_koniec": f"⌛ Abonament wygasł {ends_on}.",
        "brak": "⏳ Dostęp jeszcze nieaktywny.",
    }
    extend = ("" if state in ("admin", "open", "bez_limitu")
              else f"\nAby {'przedłużyć' if state != 'brak' else 'uzyskać'} dostęp, skontaktuj się z {contact_html}.")
    return "👤 <b>Twoje konto</b>\n\n" + lines.get(state, lines["brak"]) + extend


def admin_granted_text(user: BotUser, ends_on: str, *, days: int | None, delivered: bool) -> str:
    who = f"<b>{escape_html(user.display_name)}</b> ({user.chat_id})"
    head = f"✅ Aktywowano: {who} – {f'{days} dni, ' if days else ''}ważny do {ends_on}"
    return head if delivered else head + "\n⚠️ Nie udało się wysłać mu wiadomości (zablokował bota?)."


def admin_trial_allowed_text(user: BotUser, *, delivered: bool) -> str:
    head = (f"🎁 Test 7 dni dostępny dla <b>{escape_html(user.display_name)}</b> ({user.chat_id}) – "
            "ruszy, gdy kliknie ▶️ Zacznij.")
    return head if delivered else head + "\n⚠️ Nie udało się wysłać mu wiadomości (zablokował bota?)."


def admin_trial_used_text(user: BotUser, ended_on: str) -> str:
    return (f"ℹ️ {escape_html(user.display_name)} ({user.chat_id}) wykorzystał już darmowy test (do {ended_on}). "
            f"Dostęp: /aktywuj {user.chat_id} {DEFAULT_PAID_DAYS}")


def admin_revoked_text(user: BotUser) -> str:
    return f"⛔ Wyłączono dostęp: <b>{escape_html(user.display_name)}</b> ({user.chat_id})."


def admin_new_model_text(count: int, ends_on: str) -> str:
    if not count:
        return "ℹ️ Nikt nie ma już dostępu bez terminu."
    return f"✅ Przełączono na nowy model: {count} {_plural(count, 'osoba', 'osoby', 'osób')} – dostęp do {ends_on}."


def admin_usage_text() -> str:
    return ("ℹ️ Użycie:\n"
            "/aktywuj &lt;chat_id&gt; &lt;dni albo data&gt; – np. /aktywuj 9876543 30 albo /aktywuj 9876543 2026-12-31\n"
            "/przedluz &lt;chat_id&gt; &lt;dni albo data&gt; – to samo: dni liczone od końca obecnego dostępu\n"
            "/odbierz &lt;chat_id&gt; – wyłącz dostęp od razu\n"
            "/trial &lt;chat_id&gt; – pozwól na 7-dniowy test (ruszy, gdy osoba kliknie ▶️)\n"
            "/nowymodel &lt;chat_id|wszyscy&gt; &lt;dni albo data&gt; – dostęp z terminem dla dotychczasowych")


def admin_unknown_user_text(chat_id: int) -> str:
    return f"🤔 Nie znam użytkownika {chat_id} – poproś, żeby najpierw napisał /start do bota."


def trial_skipped_text(user: BotUser, ends_on: str) -> str:
    return f"ℹ️ {escape_html(user.display_name)} ma już dostęp do {ends_on} – test pominięty."


def admin_expired_text(entries: Sequence[tuple[BotUser, str]]) -> str:
    lines = [f"• {escape_html(user.display_name)} ({user.chat_id}) – {'test' if user.on_trial else 'abonament'} "
             f"do {ends_on} · przedłuż: /aktywuj {user.chat_id} {DEFAULT_PAID_DAYS}" for user, ends_on in entries]
    return "⌛ <b>Koniec dostępu</b>\n" + "\n".join(lines)


def users_list(users: Sequence[BotUser], subscription: Callable[[BotUser], str]) -> str:
    """Lista dla admina: status, abonament (np. „do 29.10.2026”, „nieaktywny”) i tryb raportów."""
    if not users:
        return "Brak użytkowników."
    icons = {"aktywny": "✅", "oczekuje": "⏳", "odrzucony": "⛔", "zablokowany": "🚫"}
    lines = [f"{icons.get(u.status, '•')} {escape_html(u.display_name)} ({u.chat_id}) · {subscription(u)}"
             f" · {MODE_LABELS.get(u.tryb, u.tryb)}" for u in users]
    return "👥 <b>Użytkownicy</b>\n" + "\n".join(lines)


JOB_LABELS = {"raport_rano": "Raport poranny", "raport_wieczor": "Raport wieczorny",
              "przypomnienia_etap": "Przypomnienia o etapie budowy", "dostep_przypomnienie": "Przypomnienie o końcu dostępu",
              "dostep_koniec": "Informacja o końcu dostępu"}
SEND_STATE_LABELS = {"wyslano": "wysłano", "pusto": "bez nowości", "oczekuje": "czeka na ponowienie",
                     "wysylanie": "w trakcie", "pominieto": "pominięto", "zablokowany": "zablokowali bota",
                     "blad": "❌ nieudane"}
HEARTBEAT_LATE = timedelta(minutes=5)


def status_text(*, now: datetime, import_status: JobStatus | None, last_import: datetime | None,
                retry_at: datetime | None, heartbeat: datetime | None, sends: dict[str, dict[str, int]],
                failed: Sequence[Send]) -> str:
    """``/status`` dla admina: import GUNB, wątek zadań i wysyłki z ostatniej doby (czas polski)."""
    lines = ["🩺 <b>Stan bota</b>", "", "📥 Import danych GUNB: " + _import_label(import_status)]
    if retry_at is not None:
        lines.append(f"   ponowienie o {local(retry_at):%H:%M}")
    lines.append(f"   ostatni pełny import: {_when(last_import)}")
    if heartbeat is None:
        lines.append("⚙️ Wątek zadań: ⚠️ jeszcze nie ruszył")
    else:
        minutes = int((now - heartbeat).total_seconds() // 60)
        mark = "✅" if now - heartbeat < HEARTBEAT_LATE else "⚠️"
        lines.append(f"⚙️ Wątek zadań: {mark} ostatni cykl {_when(heartbeat)} ({minutes} min temu)")
    lines += ["", "📤 <b>Wysyłki z ostatniej doby</b>"]
    for zadanie, counts in sends.items():
        parts = ", ".join(f"{SEND_STATE_LABELS.get(state, state)} {n}" for state, n in counts.items())
        lines.append(f"• {escape_html(_job_label(zadanie))}: {parts}")
    if not sends:
        lines.append("brak")
    for item in failed[:10]:
        lines.append(f"❌ {item.chat_id} · {escape_html(_job_label(item.zadanie))}: "
                     f"{escape_html(item.ostatni_blad or '?')}")
    return "\n".join(lines)


def _import_label(status: JobStatus | None) -> str:
    if status is None or status.stan is None:
        return "jeszcze się nie odbył"
    detail = f" – {escape_html(status.opis)}" if status.opis else ""
    if status.stan == "trwa":
        return f"⏳ trwa od {_when(status.start)}"
    if status.stan == "ok":
        return f"✅ {_when(status.koniec)}{detail}"
    if status.stan == "pominieto":
        return f"⏭️ pominięty {_when(status.koniec)}{detail}"
    return f"❌ błąd {_when(status.koniec)}{detail}"


def _job_label(zadanie: str) -> str:
    """``raport_rano:2026-09-29`` → „Raport poranny 29.09”; runda ręczna ma dopisek."""
    job, _, key = zadanie.partition(":")
    try:
        day = date.fromisoformat(key[:10]).strftime("%d.%m")
    except ValueError:
        day = key
    return f"{JOB_LABELS.get(job, job)} {day}" + (" (ręcznie)" if "#" in key else "")


def _when(moment: datetime | str | None) -> str:
    if not moment:
        return "—"
    return local(datetime.fromisoformat(moment) if isinstance(moment, str) else moment).strftime("%d.%m %H:%M")


# --- Filtry ------------------------------------------------------------------------------------

def filters_screen(user: BotUser, place_names: dict[str, str], settings: BotConfig, prefix: str = "") -> tuple[str, Markup]:
    f = user.filtry
    places = [place_names.get(code, code) for code in f.powiaty] + list(f.miejsca)
    categories = [label for key, label in CATEGORY_CHOICES if key in f.kategorie]
    if f.radius_active:
        place_label = f"do {f.promien_km} km od Twojej bazy"
    else:
        place_label = escape_html(", ".join(places)) if places else "cały monitorowany obszar"
    lines = [
        prefix + "🔎 <b>Twoje filtry</b>" if prefix else "🔎 <b>Twoje filtry</b>",
        f"📍 Miejsce: {place_label}",
        f"🏗️ Rodzaj: {escape_html(', '.join(categories)) if categories else 'wszystkie'}",
        f"📦 Kubatura: {_volume_label(f.min_kubatura)}",
        f"💼 Inwestor: {_investor_label(f.inwestor)}",
        f"🔥 Tylko HOT: {'tak' if user.tylko_hot else 'nie'}",
        f"⏰ Wysyłka: {_mode_label(user.tryb, settings)}",
        f"🧰 Branża: {_trade_label(get_trade(user.branza))}",
        "",
        "Kliknij, co chcesz zmienić 👇",
    ]
    markup = inline([
        [("📍 Miejsce", "f:place"), ("🏗️ Rodzaj", "f:type")],
        [("📦 Kubatura", "f:vol"), ("💼 Inwestor", "f:inv")],
        [("🧰 Branża – przypomnienia o etapie", "f:trade")],
        [("🧹 Wyczyść filtry", "f:clear")],
        [("📊 Pokaż pasujące", "f:go")],
    ])
    return "\n".join(lines), markup


# --- Przypomnienia o etapie budowy („Kiedy dzwonić”) ---------------------------------------------------

def trade_picker(current: str | None) -> tuple[str, Markup]:
    """Wybór branży: przypomnienie przychodzi, gdy budowa wchodzi w orientacyjne okno etapu tej branży."""
    rows = [[(("✅ " if trade.key == current else "") + f"{trade.label} – {trade_window_label(trade)}",
              f"fb:{trade.key}")] for trade in TRADES]
    rows.append([(("✅ " if current is None else "") + "🚫 Bez przypomnień", "fb:none")])
    rows.append([("◀️ Filtry", "f:show")])
    text = ("🧰 <b>Twoja branża</b>\n"
            "Dekarz nie potrzebuje budowy w dniu pozwolenia – wtedy jest za wcześnie. Wybierz branżę, "
            "a dam znać, gdy budowa wejdzie w orientacyjne okno Twojego etapu. To szacunek od daty decyzji "
            "(bloki i hale budują się ok. półtora raza dłużej) – rzeczywisty etap trzeba sprawdzić.")
    return text, inline(rows)


def trade_window_label(trade: Trade) -> str:
    if trade.months is None:
        return "od razu"
    start, end = trade.months
    return f"{start:g}–{end:g} mies."


def trade_saved_text(trade: Trade | None) -> str:
    if trade is None:
        return "🔕 Przypomnienia o etapach budowy wyłączone."
    return (f"✅ Branża: {trade.label}. Jesteś potrzebny od razu – nowe budowy dostajesz jak dotąd, "
            "bez dodatkowych przypomnień.")


def stage_none_text(trade: Trade) -> str:
    return (f"✅ Branża: {trade.label}.\n⏰ Na razie żadna inwestycja z Twoich filtrów nie jest w orientacyjnym "
            f"oknie etapu {trade.stage}. Dam znać rano, gdy któraś w nie wejdzie.")


STAGE_ESTIMATE_NOTE = "Szacunek na podstawie daty decyzji; rzeczywisty etap wymaga sprawdzenia."


def stage_reminder(trade: Trade, leads: Sequence[Investment], total: int,
                   distance: Distance | None = None) -> tuple[str, Markup]:
    """Przypomnienie o etapie: inwestycje, które według szacunku weszły w okno etapu branży klienta."""
    what = _plural(total, "Warto sprawdzić tę inwestycję", "Warto sprawdzić te inwestycje",
                   "Warto sprawdzić te inwestycje")
    lines = [f"⏰ <b>{what}</b> – {escape_html(trade.label)}",
             f"{_count(total, 'inwestycja', 'inwestycje', 'inwestycji')} z Twoich filtrów może być teraz "
             f"na etapie {trade.stage}.",
             f"🗓️ Orientacyjne okno dla Twojej branży: {trade_window_label(trade)} po decyzji "
             "(bloki i hale ok. 1,5 raza dłużej).",
             f"ℹ️ {STAGE_ESTIMATE_NOTE}", ""]
    lines += [_stage_entry(position, inv, distance) for position, inv in enumerate(leads, start=1)]
    if total > len(leads):
        lines.append(f"\n…i {total - len(leads)} więcej – kolejne jutro rano.")
    lines.append("\n👇 Kliknij numer: mapa i szczegóły.")
    return "\n".join(lines), inline(number_buttons([(p, inv.nr) for p, inv in enumerate(leads, start=1)]))


def place_picker(filters: UserFilters, options: Sequence[tuple[str, str]]) -> tuple[str, Markup]:
    nearby = f"✅ 📍 Blisko mnie: do {filters.promien_km} km" if filters.radius_active else "📍 Blisko mnie (promień od bazy)"
    rows = [[(nearby, "f:near")]]
    rows += [[(("✅ " if code in filters.powiaty else "▫️ ") + label, f"fp:{code}")] for code, label in options]
    rows += [[(f"❌ {place}", f"fpr:{index}")] for index, place in enumerate(filters.miejsca)]
    rows.append([("✏️ Wpisz miejscowość", "fp:txt")])
    rows.append([("✔️ Gotowe", "f:show")])
    text = ("📍 <b>Gdzie szukać?</b>\nPromień od Twojej bazy albo powiaty / wpisana miejscowość lub gmina.\n"
            "Nic nie zaznaczone = cały monitorowany obszar.")
    return text, inline(rows)


def location_request() -> tuple[str, Markup]:
    """Prośba o pinezkę bazy – przycisk z natywnym udostępnieniem lokalizacji Telegrama."""
    text = ("📍 <b>Gdzie jest Twoja baza?</b>\n"
            f"Kliknij na dole <b>{SEND_LOCATION_BUTTON}</b> – wyślesz miejsce, w którym teraz jesteś.\n"
            "Jesteś gdzie indziej? Wyślij pinezkę bazy: 📎 → Lokalizacja → przesuń mapę na bazę.")
    markup = {"keyboard": [[{"text": SEND_LOCATION_BUTTON, "request_location": True}], [{"text": CANCEL_BUTTON}]],
              "resize_keyboard": True, "one_time_keyboard": True}
    return text, markup


def base_saved_text(filters: UserFilters) -> str:
    return (f"✅ Baza zapisana. Pokazuję budowy do <b>{filters.promien_km} km</b> od niej "
            "(w linii prostej) – przy każdej zobaczysz odległość 📏.")


def nearby_screen(filters: UserFilters) -> tuple[str, Markup]:
    """„📍 Blisko mnie”: wybór promienia od bazy (albo prośba o pinezkę, gdy bazy jeszcze nie ma)."""
    if filters.baza is None:
        text = ("📍 <b>Blisko mnie</b>\nWyślij pinezkę swojej bazy, a pokażę tylko budowy "
                "w wybranym promieniu – np. do 15 km w linii prostej.")
        return text, inline([[("📍 Wyślij pinezkę bazy", "fr:loc")], [("◀️ Filtry", "f:show")]])
    state = f"do <b>{filters.promien_km} km</b> od bazy" if filters.radius_active else "wyłączony"
    choices = [((f"✅ {km} km" if filters.radius_active and filters.promien_km == km else f"{km} km"), f"fr:{km}")
               for km in RADIUS_CHOICES]
    rows = [choices[:3], choices[3:], [("📍 Zmień bazę", "fr:loc")] + ([("🚫 Wyłącz", "fr:0")]
                                                                     if filters.radius_active else [])]
    rows += [[("📊 Pokaż pasujące", "f:go")], [("◀️ Filtry", "f:show")]]
    text = (f"📍 <b>Blisko mnie</b>\nPromień: {state}.\n"
            "Jak daleko jeździsz? Odległość liczę w linii prostej od Twojej bazy.")
    return text, inline(rows)


def distance_label(km: float) -> str:
    return "<1 km" if km < 1 else f"{km:.0f} km"


def type_picker(filters: UserFilters) -> tuple[str, Markup]:
    rows = [[(("✅ " if key in filters.kategorie else "▫️ ") + label, f"ft:{index}")]
            for index, (key, label) in enumerate(CATEGORY_CHOICES)]
    rows.append([("✔️ Gotowe", "f:show")])
    return "🏗️ <b>Jakie budynki?</b>\nZaznacz, co Cię interesuje. Nic nie zaznaczone = wszystkie.", inline(rows)


def volume_picker(filters: UserFilters) -> tuple[str, Markup]:
    rows = []
    for value in VOLUME_CHOICES:
        label = "Dowolna" if value == 0 else f"od {_thousands(value)} m³"
        current = (filters.min_kubatura or 0) == value
        rows.append([(("✅ " if current else "") + label, f"fv:{value}")])
    rows.append([("✏️ Wpisz liczbę", "fv:txt"), ("◀️ Wróć", "f:show")])
    return "📦 <b>Minimalna kubatura</b>\nDla porównania: typowy dom to ok. 900 m³, blok – kilkanaście tysięcy.", inline(rows)


def investor_picker(filters: UserFilters) -> tuple[str, Markup]:
    rows = [
        [(("✅ " if not filters.inwestor else "") + "Dowolny", "fi:any")],
        [(("✅ " if filters.inwestor == "firma" else "") + "Tylko firmy (jawny inwestor)", "fi:firm")],
        [("✏️ Wpisz nazwę inwestora", "fi:txt")],
        [("◀️ Wróć", "f:show")],
    ]
    text = ("💼 <b>Inwestor</b>\nOsoby prywatne są w rejestrze ukryte. „Tylko firmy” = deweloperzy, spółki, gminy.")
    return text, inline(rows)


def prompt_text(what: str) -> str:
    return {
        "miejsce": "✏️ Napisz nazwę miejscowości lub gminy, np. <b>Warszawa</b> albo <b>Kostrzyn</b>.",
        "inwestor": "✏️ Napisz nazwę inwestora (albo jej część), np. <b>Budimex</b>.",
        "kubatura": "✏️ Napisz minimalną kubaturę w m³, np. <b>5000</b>.",
    }[what]


# --- Tryb wysyłki ---------------------------------------------------------------------------------

def mode_screen(user: BotUser, settings: BotConfig) -> tuple[str, Markup]:
    rows = [[(("✅ " if user.tryb == mode else "") + _mode_label(mode, settings), f"m:{mode}")] for mode in MODE_LABELS]
    text = ("⏰ <b>Kiedy mam wysyłać nowe inwestycje?</b>\n"
            "⚡ Od razu – każda nowa inwestycja osobno\n"
            f"🌅 Rano – jeden raport o {settings.morning_time}\n"
            f"🌙 Wieczorem – jeden raport o {settings.evening_time}\n\n"
            "Alerty 👀 obserwowanych inwestorów i gmin przychodzą zawsze od razu.")
    return text, inline(rows)


# --- Lead ------------------------------------------------------------------------------------------

def lead_keyboard(inv: Investment, *, flags: LeadFlags, watching_investor: bool, watching_gmina: bool) -> Markup:
    """Przyciski pod inwestycją; każdy niesie docelowy stan (``s1``/``s0``), więc ponowione kliknięcie nic nie psuje."""
    rows: list[list[dict[str, str]]] = []
    links = [{"text": text, "url": url} for text, url in (("📍 Mapa", inv.google_maps_url),
                                                          ("🏛️ Geoportal", inv.geoportal_url)) if url]
    if links:
        rows.append(links)
    nr = inv.nr
    actions = [
        ("⭐ Zapisany ✓", f"s0:{nr}") if flags.saved else ("⭐ Zapisz", f"s1:{nr}"),
        ("✅ Przejrzany ✓", f"r0:{nr}") if flags.reviewed else ("✅ Przejrzane", f"r1:{nr}"),
        ("🗑️ Ukryj", f"h:{nr}"),
    ]
    rows.append([{"text": t, "callback_data": d} for t, d in actions])
    if inv.inwestor:
        label = "👀 Obserwujesz inwestora ✓" if watching_investor else "👀 Obserwuj inwestora"
        rows.append([{"text": label, "callback_data": f"wi:{nr}"}])
    if inv.gmina_teryt:
        label = "📌 Obserwujesz gminę ✓" if watching_gmina else "📌 Obserwuj gminę"
        rows.append([{"text": label, "callback_data": f"wg:{nr}"}])
    return {"inline_keyboard": rows}


def hidden_card(inv: Investment) -> tuple[str, Markup]:
    title = escape_html(_short(inv.nazwa_zamierzenia or "inwestycja", 80))
    return (f"🗑️ Ukryte: {title}\nNie pokażę więcej tej inwestycji.",
            inline([[("↩️ Przywróć", f"u:{inv.nr}")]]))


def watch_header(item: WatchItem, inv: Investment) -> tuple[str, str, str]:
    if item.rodzaj == "inwestor":
        return "👀", "WATCHLISTA", "nowa inwestycja obserwowanego inwestora"
    return "👀", "WATCHLISTA", f"nowa inwestycja w obserwowanej gminie {item.etykieta}"


# --- Raport ---------------------------------------------------------------------------------------

HISTORY_PAGE_SIZE = 10


def _watched_text(watched: int) -> str:
    return (f"{watched} {_plural(watched, 'dotyczy obserwowanego inwestora lub gminy', 'dotyczą obserwowanych inwestorów lub gmin', 'dotyczy obserwowanych inwestorów lub gmin')}"
            if watched else "")


def report(date_label: str, *, total_new: int, leads: Sequence[Investment], matching: int, hot: int, watched: int,
           distance: Distance | None = None, freshness: str = "") -> tuple[str, Markup | None]:
    """Raport nowych inwestycji: podsumowanie + ponumerowana lista (numery otwierają szczegóły).

    Args:
        total_new: nowe (jeszcze niewysłane) inwestycje od ostatniego raportu.
        leads: pokazywane nowe inwestycje pasujące do filtrów; ``matching`` – ile pasuje łącznie.
        distance: odległość od bazy użytkownika.
    """
    summary = [
        f"Znaleziono {_count(total_new, 'nową inwestycję', 'nowe inwestycje', 'nowych inwestycji')}.",
        f"{matching} {_plural(matching, 'spełnia', 'spełniają', 'spełnia')} Twoje filtry.",
    ]
    extra = []
    if hot:
        extra.append(f"{hot} to 🔥 HOT (duża skala).")
    if watched:
        extra.append(_watched_text(watched) + ".")
    lines = [f"📊 <b>Raport {date_label}</b>", " ".join(summary)] + ([" ".join(extra)] if extra else [])
    lines.append("")
    lines += [_report_entry(position, inv, distance) for position, inv in enumerate(leads, start=1)]
    if matching > len(leads):
        lines.append(f"\n…i {matching - len(leads)} więcej – kliknij 📊 Inwestycje, żeby zobaczyć kolejne.")
    lines.append("\n👇 Kliknij numer, żeby zobaczyć szczegóły i zapisać.")
    if freshness:
        lines.append(freshness)
    return "\n".join(lines), inline(number_buttons([(p, inv.nr) for p, inv in enumerate(leads, start=1)]))


def freshness_line(checked_on: str | None) -> str:
    """Kiedy rejestr GUNB był ostatnio sprawdzony w całości (pod raportami i przeglądem)."""
    return f"🕒 Rejestr GUNB sprawdzony: {checked_on}" if checked_on else "🕒 Rejestr GUNB jeszcze nie był sprawdzony"


def import_note(state: str | None, *, retry_at: str | None) -> str | None:
    """Dlaczego może nie być nowości: import w toku albo nieudany / niepełny (``None`` – wszystko w porządku)."""
    retry = f" – ponowię o {retry_at}" if retry_at else ""
    if state == "trwa":
        return "⏳ Właśnie sprawdzam rejestr GUNB – nowe inwestycje mogą dojść za kilka minut."
    if state == "blad":
        return f"⚠️ Ostatnie sprawdzenie rejestru się nie udało{retry}. Dane mogą być nieaktualne."
    if state == "pominieto":
        return f"⚠️ Ostatnie sprawdzenie rejestru nie zakończyło się{retry}. Dane mogą być niepełne."
    return None


def nothing_new_head(*, total_new: int, watched: int, note: str | None = None) -> list[str]:
    """Nagłówek, gdy żadna nowa inwestycja nie pasuje – nad przeglądem historii."""
    if total_new:
        head = [f"Znaleziono {_count(total_new, 'nową inwestycję', 'nowe inwestycje', 'nowych inwestycji')}"
                " – żadna nie pasuje do Twoich filtrów."]
    else:
        head = ["📭 <b>Nic nowego</b> od ostatniego raportu."]
    if note:
        head.append(note)
    if watched:
        head.append(f"👀 {_watched_text(watched)} (alert już wysłany).")
    return head


def history_page(leads: Sequence[Investment], *, page: int, pages: int, total: int, days: int,
                 head: Sequence[str] = (), distance: Distance | None = None,
                 filters: str = "", freshness: str = "") -> tuple[str, Markup | None]:
    """Przegląd historii: pasujące z ostatnich ``days`` dni, stronami „◀️ Wstecz / Dalej ▶️”.

    ``total`` to wszystkie pasujące – dokładnie tyle da się przejrzeć; przegląd niczego nie oznacza jako wysłane.
    """
    lines = list(head)
    if not total:
        lines.append(f"\n🔎 Z ostatnich {days} dni brak pasujących do Twoich filtrów.")
        if filters:
            lines.append(filters)
        lines.append("Możesz poszerzyć obszar albo zmienić rodzaj inwestycji 👇")
        if freshness:
            lines.append(freshness)
        return "\n".join(lines), inline([[("🗺️ Poszerz obszar", "f:place"), ("🏗️ Zmień rodzaj", "f:type")]])
    pager = f" · strona {page + 1}/{pages}" if pages > 1 else ""
    lines += ["", f"🔎 <b>Pasujące do Twoich filtrów z ostatnich {days} dni</b> ({total}){pager}:", ""]
    first = page * HISTORY_PAGE_SIZE + 1
    lines += [_report_entry(first + offset, inv, distance) for offset, inv in enumerate(leads)]
    lines.append("\n👇 Kliknij numer, żeby otworzyć inwestycję.")
    if freshness:
        lines.append(freshness)
    navigation = []
    if page > 0:
        navigation.append(("◀️ Wstecz", f"hp:{page - 1}"))
    if page + 1 < pages:
        navigation.append(("Dalej ▶️", f"hp:{page + 1}"))
    rows = number_buttons([(first + offset, inv.nr) for offset, inv in enumerate(leads)])
    return "\n".join(lines), inline(rows + [navigation])


def filters_summary(user: BotUser, place_names: dict[str, str]) -> str:
    """Jednolinijkowe podsumowanie aktywnych filtrów (np. przy braku wyników)."""
    f = user.filtry
    if f.radius_active:
        place = f"do {f.promien_km} km od Twojej bazy"
    else:
        places = [place_names.get(code, code) for code in f.powiaty] + list(f.miejsca)
        place = ", ".join(places) if places else "cały monitorowany obszar"
    categories = [label for key, label in CATEGORY_CHOICES if key in f.kategorie]
    parts = [f"📍 {place}", f"🏗️ {', '.join(categories) if categories else 'wszystkie rodzaje'}",
             f"📦 {_volume_label(f.min_kubatura)}"]
    if f.inwestor:
        parts.append(f"💼 {_investor_label(f.inwestor)}")
    if user.tylko_hot:
        parts.append("🔥 tylko HOT")
    return "Twoje filtry: " + escape_html(" · ".join(parts)).replace("&amp;", "&")


def saved_list(leads: Sequence[Investment], page: int, total: int,
               distance: Distance | None = None) -> tuple[str, Markup | None]:
    if total == 0:
        return "⭐ Nie masz jeszcze zapisanych inwestycji.\nKliknij <b>⭐ Zapisz</b> pod ciekawą inwestycją.", None
    first = page * SAVED_PAGE_SIZE + 1
    lines = [f"⭐ <b>Zapisane</b> ({total})", ""]
    lines += [_report_entry(first + offset, inv, distance) for offset, inv in enumerate(leads)]
    lines.append("\n👇 Kliknij numer, żeby otworzyć inwestycję.")
    rows = number_buttons([(first + offset, inv.nr) for offset, inv in enumerate(leads)])
    navigation = []
    if page > 0:
        navigation.append(("◀️ Wstecz", f"sv:{page - 1}"))
    if first + len(leads) - 1 < total:
        navigation.append(("Dalej ▶️", f"sv:{page + 1}"))
    return "\n".join(lines), inline(rows + [navigation])


def watch_screen(items: Sequence[WatchItem]) -> tuple[str, Markup | None]:
    if not items:
        return ("👀 Nikogo jeszcze nie obserwujesz.\n"
                "Pod inwestycją kliknij <b>👀 Obserwuj inwestora</b> albo <b>📌 Obserwuj gminę</b> – "
                "o ich nowych inwestycjach dam znać od razu."), None
    lines = ["👀 <b>Obserwujesz</b>"]
    lines += [f"{'💼' if item.rodzaj == 'inwestor' else '📌'} {escape_html(item.etykieta)}" for item in items]
    lines.append("\nKliknij ❌, żeby przestać obserwować.")
    return "\n".join(lines), inline([[(f"❌ {item.etykieta}", f"wd:{item.id}")] for item in items])


def hot_only_text(enabled: bool) -> str:
    if enabled:
        return "✅ Od teraz wysyłam tylko 🔥 HOT – inwestycje o największej skali (szacunek z kubatury i rodzaju)."
    return "✅ Wysyłam wszystkie inwestycje pasujące do filtrów (🔥 HOT i pozostałe)."


def unknown_text() -> str:
    return "🤔 Nie rozumiem. Użyj przycisków na dole ekranu 👇"


# --- Pomocnicze ---------------------------------------------------------------------------------

def _report_entry(position: int, inv: Investment, distance: Distance | None = None) -> str:
    icon = ("🔥" if inv.priorytet == HOT else "") + CATEGORY_ICONS.get(inv.kategoria or "inna", "•")
    km = distance(inv) if distance else None
    facts = [p for p in (
        f"📏 {distance_label(km)}" if km is not None else None,
        inv.adres_opisowy or inv.miejscowosc,
        f"{_thousands(inv.kubatura)} m³" if inv.kubatura else None,
        _short_date(inv.data_aktualizacji),
    ) if p]
    title = escape_html(_short(inv.nazwa_zamierzenia or "(brak opisu)", 90))
    return f"{position}. {icon} <b>{title}</b>\n    {escape_html(' · '.join(facts))}"


def _stage_entry(position: int, inv: Investment, distance: Distance | None = None) -> str:
    """Pozycja przypomnienia – z pełną datą pozwolenia (bywa sprzed roku)."""
    icon = ("🔥" if inv.priorytet == HOT else "") + CATEGORY_ICONS.get(inv.kategoria or "inna", "•")
    km = distance(inv) if distance else None
    decided = inv.data_decyzji or inv.data_wplywu
    facts = [p for p in (
        f"📏 {distance_label(km)}" if km is not None else None,
        inv.adres_opisowy or inv.miejscowosc,
        f"{_thousands(inv.kubatura)} m³" if inv.kubatura else None,
        f"decyzja {decided[8:10]}.{decided[5:7]}.{decided[:4]}" if decided and len(decided) >= 10 else None,
    ) if p]
    title = escape_html(_short(inv.nazwa_zamierzenia or "(brak opisu)", 90))
    return f"{position}. {icon} <b>{title}</b>\n    {escape_html(' · '.join(facts))}"


def _trade_label(trade: Trade | None) -> str:
    if trade is None:
        return "nie wybrano (bez przypomnień)"
    if trade.months is None:
        return f"{trade.label} – nowe budowy od razu"
    return f"{trade.label} – przypomnę orientacyjnie {trade_window_label(trade)} po decyzji"


def _mode_label(mode: str, settings: BotConfig) -> str:
    if mode == "rano":
        return f"🌅 Raport rano ({settings.morning_time})"
    if mode == "wieczor":
        return f"🌙 Raport wieczorem ({settings.evening_time})"
    return MODE_LABELS.get(mode, mode)


def _mode_sentence(mode: str, settings: BotConfig) -> str:
    if mode == "rano":
        return f"codziennie rano o {settings.morning_time}"
    if mode == "wieczor":
        return f"codziennie wieczorem o {settings.evening_time}"
    return "od razu, gdy pojawi się coś nowego"


def _volume_label(value: float | None) -> str:
    return f"od {_thousands(value)} m³" if value else "dowolna"


def _investor_label(value: str | None) -> str:
    if not value:
        return "dowolny"
    if value == "firma":
        return "tylko firmy"
    return f"zawiera „{escape_html(value)}”"


def _thousands(value: float) -> str:
    return format(value, ",.0f").replace(",", " ")


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _short_date(iso_date: str | None) -> str | None:
    if not iso_date or len(iso_date) < 10:
        return None
    return f"{iso_date[8:10]}.{iso_date[5:7]}"


def _plural(count: int, one: str, few: str, many: str) -> str:
    if count == 1:
        return one
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return few
    return many


def _count(count: int, one: str, few: str, many: str) -> str:
    return f"{count} {_plural(count, one, few, many)}"
