"""Teksty i przyciski bota – „proste jak drut”: krótkie zdania, duże przyciski, zero pisania komend.

Wszystkie funkcje są czyste (zwracają tekst HTML i/lub słownik ``reply_markup``), więc łatwo je testować
i zmieniać bez dotykania logiki bota.
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

from .bot_store import BotUser, LeadFlags, UserFilters, WatchItem
from .config import BotConfig
from .exporter import CATEGORY_ICONS, escape_html
from .models import Investment
from .scoring import HOT
from .stages import TRADES, Trade, get_trade

Markup = dict[str, Any]
Distance = Callable[[Investment], float | None]

MENU_BUTTONS: tuple[str, ...] = (
    "📊 Co nowego?", "🔎 Filtry",
    "⭐ Zapisane", "👀 Obserwowane",
    "⏰ Kiedy wysyłać", "🔥 Tylko HOT",
    "📍 Blisko mnie",
)
CANCEL_BUTTON = "↩️ Anuluj"
SEND_LOCATION_BUTTON = "📍 Wyślij moją lokalizację"

BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("nowe", "📊 Pokaż nowe leady"),
    ("filtry", "🔎 Ustaw, jakie inwestycje chcesz dostawać"),
    ("blisko", "📍 Budowy blisko Twojej bazy"),
    ("branza", "🧰 Twoja branża – przypomnę, kiedy dzwonić"),
    ("zapisane", "⭐ Twoje zapisane leady"),
    ("obserwowane", "👀 Obserwowani inwestorzy i gminy"),
    ("tryb", "⏰ Kiedy wysyłać leady"),
    ("tylkohot", "🔥 Włącz/wyłącz tylko HOT"),
    ("pomoc", "❓ Jak to działa"),
)

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
        f"👷 Cześć{who}! Będę Ci wysyłał <b>nowe pozwolenia na budowę</b> z Twojej okolicy.\n\n"
        "Wszystko ustawisz przyciskami na dole ekranu 👇\n"
        "• <b>📍 Blisko mnie</b> – wyślij pinezkę bazy, a dostaniesz budowy w promieniu, np. 15 km\n"
        "• <b>🧰 Branża</b> (w 🔎 Filtry) – przypomnę o budowie, gdy dojdzie do Twojego etapu, np. dachu\n"
        "• <b>🔎 Filtry</b> – gdzie i jakie inwestycje chcesz dostawać\n"
        "• <b>⏰ Kiedy wysyłać</b> – od razu, raport rano albo wieczorem\n"
        "• <b>🔥 Tylko HOT</b> – tylko najlepsze, duże roboty\n\n"
        "Pod każdym leadem masz przyciski: ⭐ Zapisz, ✅ Przejrzane, 🗑️ Ukryj, 👀 Obserwuj."
    )


def help_text(settings: BotConfig, *, admin: bool = False) -> str:
    admin_part = ("\n\n👑 <b>Admin</b>: /aktywuj &lt;chat_id&gt; &lt;dni&gt; · /trial &lt;chat_id&gt; · /uzytkownicy"
                  if admin else "")
    return _help_body(settings) + admin_part


def _help_body(settings: BotConfig) -> str:
    return (
        "❓ <b>Jak to działa</b>\n\n"
        "Codziennie sprawdzam rejestr pozwoleń na budowę (GUNB) i wysyłam Ci to, co pasuje do filtrów.\n\n"
        "📊 <b>Co nowego?</b> – pokaż nowe leady teraz\n"
        "📍 <b>Blisko mnie</b> – budowy w promieniu od Twojej bazy (pinezka w Telegramie)\n"
        "🧰 <b>Branża</b> – ⏰ „Kiedy dzwonić”: przypomnę o budowie, gdy dojdzie do Twojego etapu\n"
        "🔎 <b>Filtry</b> – miejsce, rodzaj budynku, kubatura, inwestor\n"
        "⭐ <b>Zapisane</b> – Twoja lista ciekawych inwestycji\n"
        "👀 <b>Obserwowane</b> – inwestorzy i gminy, o których dostajesz alert od razu\n"
        f"⏰ <b>Kiedy wysyłać</b> – od razu, rano ({settings.morning_time}) albo wieczorem ({settings.evening_time})\n"
        "🔥 <b>Tylko HOT</b> – tylko największe roboty (duża kubatura, bloki, hale, kilka budynków)\n\n"
        "🔥 HOT / 🟡 NORMAL / ⚪ LOW – punkty za kubaturę, rodzaj budynku, liczbę budynków i nową budowę."
    )


def rejected_text() -> str:
    return "⛔ Administrator nie przyznał dostępu do bota."


# --- Abonament (paywall) i panel admina --------------------------------------------------------------

TRIAL_DAYS = 3
DEFAULT_PAID_DAYS = 30
TRIAL_TEXT = ("🎁 Aktywowano darmowy okres próbny na 3 dni! Zobacz, jak szybciej docierać do klientów. "
              "Po tym czasie bot zostanie wstrzymany.")


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
            f"Dostęp: /trial {user.chat_id} (3 dni za darmo) albo /aktywuj {user.chat_id} 30")
    return text, inline([[("🎁 Trial 3 dni", f"adm:trial:{user.chat_id}"),
                          ("✅ 30 dni", f"adm:ok:{user.chat_id}"), ("⛔ Odrzuć", f"adm:no:{user.chat_id}")]])


def activated_text(days: int, ends_on: str, name: str | None) -> str:
    return (f"✅ Twój abonament został aktywowany na {days} dni!\nWażny do {ends_on}.\n\n" + welcome_text(name))


def trial_text(ends_on: str, name: str | None) -> str:
    return f"{TRIAL_TEXT}\nWażny do {ends_on}.\n\n" + welcome_text(name)


def admin_granted_text(user: BotUser, ends_on: str, *, trial: bool, days: int, delivered: bool) -> str:
    who = f"<b>{escape_html(user.display_name)}</b> ({user.chat_id})"
    head = f"🎁 Trial: {who} – ważny do {ends_on}" if trial else f"✅ Aktywowano: {who} – {days} dni, ważny do {ends_on}"
    return head if delivered else head + "\n⚠️ Nie udało się wysłać mu wiadomości (zablokował bota?)."


def admin_usage_text() -> str:
    return ("ℹ️ Użycie:\n/aktywuj &lt;chat_id&gt; &lt;liczba_dni&gt; – np. /aktywuj 9876543 30\n"
            "/trial &lt;chat_id&gt; – 3 dni za darmo")


def admin_unknown_user_text(chat_id: int) -> str:
    return f"🤔 Nie znam użytkownika {chat_id} – poproś, żeby najpierw napisał /start do bota."


def trial_skipped_text(user: BotUser, ends_on: str) -> str:
    return f"ℹ️ {escape_html(user.display_name)} ma już abonament do {ends_on} – trial pominięty."


def admin_expired_text(entries: Sequence[tuple[BotUser, str]]) -> str:
    lines = [f"• {escape_html(user.display_name)} ({user.chat_id}) – {ends_on} · przedłuż: "
             f"/aktywuj {user.chat_id} {DEFAULT_PAID_DAYS}" for user, ends_on in entries]
    return "⌛ <b>Wygasłe abonamenty</b>\n" + "\n".join(lines)


def users_list(users: Sequence[BotUser], subscription: Callable[[BotUser], str]) -> str:
    """Lista dla admina: status, abonament (np. „do 29.10.2026”, „nieaktywny”) i tryb raportów."""
    if not users:
        return "Brak użytkowników."
    icons = {"aktywny": "✅", "oczekuje": "⏳", "odrzucony": "⛔", "zablokowany": "🚫"}
    lines = [f"{icons.get(u.status, '•')} {escape_html(u.display_name)} ({u.chat_id}) · {subscription(u)}"
             f" · {MODE_LABELS.get(u.tryb, u.tryb)}" for u in users]
    return "👥 <b>Użytkownicy</b>\n" + "\n".join(lines)


# --- Filtry ------------------------------------------------------------------------------------

def filters_screen(user: BotUser, place_names: dict[str, str], settings: BotConfig, prefix: str = "") -> tuple[str, Markup]:
    f = user.filtry
    places = [place_names.get(code, code) for code in f.powiaty] + list(f.miejsca)
    categories = [label for key, label in CATEGORY_CHOICES if key in f.kategorie]
    if f.radius_active:
        place_label = f"do {f.promien_km} km od Twojej bazy"
    else:
        place_label = escape_html(", ".join(places)) if places else "wszędzie"
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
        [("🧰 Branża – kiedy dzwonić", "f:trade")],
        [("🧹 Wyczyść filtry", "f:clear")],
        [("📊 Pokaż pasujące", "f:go")],
    ])
    return "\n".join(lines), markup


# --- „⏰ Kiedy dzwonić” ------------------------------------------------------------------------------

def trade_picker(current: str | None) -> tuple[str, Markup]:
    """Wybór branży: przypomnienie przychodzi, gdy budowa dochodzi do etapu tej branży."""
    rows = [[(("✅ " if trade.key == current else "") + f"{trade.label} – {trade_window_label(trade)}",
              f"fb:{trade.key}")] for trade in TRADES]
    rows.append([(("✅ " if current is None else "") + "🚫 Bez przypomnień", "fb:none")])
    rows.append([("◀️ Filtry", "f:show")])
    text = ("🧰 <b>Twoja branża – kiedy dzwonić?</b>\n"
            "Dekarz nie potrzebuje budowy w dniu pozwolenia – wtedy jest za wcześnie. Wybierz branżę, "
            "a przypomnę o budowie, gdy dojdzie do Twojego etapu (liczę od daty pozwolenia; "
            "bloki i hale budują się ok. półtora raza dłużej).")
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
    return (f"✅ Branża: {trade.label}.\n⏰ Na razie żadna budowa z Twoich filtrów nie jest na etapie "
            f"{trade.stage}. Przypomnę rano, gdy któraś do niego dojdzie.")


def stage_reminder(trade: Trade, leads: Sequence[Investment], total: int,
                   distance: Distance | None = None) -> tuple[str, Markup]:
    """Przypomnienie „Kiedy dzwonić”: budowy, które właśnie są na etapie branży klienta."""
    count = _count(total, "budowa", "budowy", "budów")
    verb = _plural(total, "jest", "są", "jest")
    lines = [f"⏰ <b>Kiedy dzwonić – {escape_html(trade.label)}</b>",
             f"{count} z Twoich filtrów {verb} teraz na etapie {trade.stage} – "
             "to dobry moment na telefon albo wizytę na budowie.", ""]
    lines += [_stage_entry(position, inv, distance) for position, inv in enumerate(leads, start=1)]
    if total > len(leads):
        lines.append(f"\n…i {total - len(leads)} więcej – kolejne jutro rano.")
    lines.append("\n👇 Kliknij numer: mapa, nawigacja i szczegóły.")
    return "\n".join(lines), inline(number_buttons([(p, inv.nr) for p, inv in enumerate(leads, start=1)]))


def place_picker(filters: UserFilters, options: Sequence[tuple[str, str]]) -> tuple[str, Markup]:
    nearby = f"✅ 📍 Blisko mnie: do {filters.promien_km} km" if filters.radius_active else "📍 Blisko mnie (promień od bazy)"
    rows = [[(nearby, "f:near")]]
    rows += [[(("✅ " if code in filters.powiaty else "▫️ ") + label, f"fp:{code}")] for code, label in options]
    rows += [[(f"❌ {place}", f"fpr:{index}")] for index, place in enumerate(filters.miejsca)]
    rows.append([("✏️ Wpisz miejscowość", "fp:txt")])
    rows.append([("✔️ Gotowe", "f:show")])
    text = ("📍 <b>Gdzie szukać?</b>\nPromień od Twojej bazy albo powiaty / wpisana miejscowość lub gmina.\n"
            "Nic nie zaznaczone = wszędzie.")
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
            "(w linii prostej) – przy każdej zobaczysz odległość 🚗.")


def nearby_screen(filters: UserFilters) -> tuple[str, Markup]:
    """„📍 Blisko mnie”: wybór promienia od bazy (albo prośba o pinezkę, gdy bazy jeszcze nie ma)."""
    if filters.baza is None:
        text = ("📍 <b>Blisko mnie</b>\nWyślij pinezkę swojej bazy, a pokażę tylko budowy, "
                "do których dojedziesz – np. do 15 km.")
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
    text = ("⏰ <b>Kiedy mam wysyłać leady?</b>\n"
            "⚡ Od razu – każdy nowy lead osobno\n"
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
    title = escape_html(_short(inv.nazwa_zamierzenia or "lead", 80))
    return (f"🗑️ Ukryte: {title}\nNie pokażę więcej tego leada.",
            inline([[("↩️ Przywróć", f"u:{inv.nr}")]]))


def watch_header(item: WatchItem, inv: Investment) -> tuple[str, str, str]:
    if item.rodzaj == "inwestor":
        return "👀", "WATCHLISTA", "nowa inwestycja obserwowanego inwestora"
    return "👀", "WATCHLISTA", f"nowa inwestycja w obserwowanej gminie {item.etykieta}"


# --- Raport ---------------------------------------------------------------------------------------

def report(date_label: str, *, total_new: int, leads: Sequence[Investment], matching: int, hot: int, watched: int,
           recent: Sequence[Investment] = (), recent_total: int = 0, recent_days: int = 30,
           distance: Distance | None = None) -> tuple[str, Markup | None]:
    """Raport: podsumowanie + ponumerowana lista (numery otwierają szczegóły leada).

    Args:
        total_new: nowe (jeszcze niewidziane) inwestycje od ostatniego raportu.
        leads: pokazywane nowe leady pasujące do filtrów; ``matching`` – ile pasuje łącznie.
        recent: gdy nowych pasujących brak – pasujące z ostatnich ``recent_days`` dni (już widziane).
        distance: odległość leada od bazy użytkownika (pokazywana jako „🚗 12 km”).
    """
    watched_text = (f"{watched} {_plural(watched, 'dotyczy obserwowanego inwestora lub gminy', 'dotyczą obserwowanych inwestorów lub gmin', 'dotyczy obserwowanych inwestorów lub gmin')}"
                    if watched else "")
    if leads:
        summary = [
            f"Znaleziono {_count(total_new, 'nową inwestycję', 'nowe inwestycje', 'nowych inwestycji')}.",
            f"{matching} {_plural(matching, 'spełnia', 'spełniają', 'spełnia')} Twoje filtry.",
        ]
        extra = []
        if hot:
            extra.append(f"{hot} to 🔥 {_plural(hot, 'HOT LEAD', 'HOT LEADY', 'HOT LEADÓW')}.")
        if watched_text:
            extra.append(watched_text + ".")
        lines = [f"📊 <b>Raport {date_label}</b>", " ".join(summary)] + ([" ".join(extra)] if extra else [])
        lines.append("")
        lines += [_report_entry(position, inv, distance) for position, inv in enumerate(leads, start=1)]
        if matching > len(leads):
            lines.append(f"\n…i {matching - len(leads)} więcej – kliknij 📊 Co nowego?, żeby zobaczyć kolejne.")
        lines.append("\n👇 Kliknij numer, żeby zobaczyć szczegóły i zapisać.")
        return "\n".join(lines), inline(number_buttons([(p, inv.nr) for p, inv in enumerate(leads, start=1)]))

    if total_new:
        head = [f"📊 <b>Raport {date_label}</b>",
                f"Znaleziono {_count(total_new, 'nową inwestycję', 'nowe inwestycje', 'nowych inwestycji')}"
                " – żadna nie pasuje do Twoich filtrów."]
    else:
        head = ["📭 <b>Nic nowego</b> od ostatniego raportu."]
    if watched_text:
        head.append(f"👀 {watched_text} (alert już wysłany).")
    if not recent:
        head.append(f"\n🔎 Z ostatnich {recent_days} dni brak pasujących do Twoich filtrów – poszerz 🔎 Filtry.")
        return "\n".join(head), None
    lines = head + ["", f"🔎 <b>Pasujące do Twoich filtrów z ostatnich {recent_days} dni</b> ({recent_total}):", ""]
    lines += [_report_entry(position, inv, distance) for position, inv in enumerate(recent, start=1)]
    if recent_total > len(recent):
        lines.append(f"\n…i {recent_total - len(recent)} więcej – zawęź 🔎 Filtry.")
    lines.append("\n👇 Kliknij numer, żeby otworzyć lead.")
    return "\n".join(lines), inline(number_buttons([(p, inv.nr) for p, inv in enumerate(recent, start=1)]))


def saved_list(leads: Sequence[Investment], page: int, total: int,
               distance: Distance | None = None) -> tuple[str, Markup | None]:
    if total == 0:
        return "⭐ Nie masz jeszcze zapisanych leadów.\nKliknij <b>⭐ Zapisz</b> pod ciekawą inwestycją.", None
    first = page * SAVED_PAGE_SIZE + 1
    lines = [f"⭐ <b>Zapisane</b> ({total})", ""]
    lines += [_report_entry(first + offset, inv, distance) for offset, inv in enumerate(leads)]
    lines.append("\n👇 Kliknij numer, żeby otworzyć lead.")
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
                "Pod leadem kliknij <b>👀 Obserwuj inwestora</b> albo <b>📌 Obserwuj gminę</b> – "
                "o ich nowych inwestycjach dam znać od razu."), None
    lines = ["👀 <b>Obserwujesz</b>"]
    lines += [f"{'💼' if item.rodzaj == 'inwestor' else '📌'} {escape_html(item.etykieta)}" for item in items]
    lines.append("\nKliknij ❌, żeby przestać obserwować.")
    return "\n".join(lines), inline([[(f"❌ {item.etykieta}", f"wd:{item.id}")] for item in items])


def hot_only_text(enabled: bool) -> str:
    if enabled:
        return "✅ Od teraz wysyłam tylko 🔥 HOT leady – największe roboty."
    return "✅ Wysyłam wszystkie leady pasujące do filtrów (🔥 HOT i pozostałe)."


def unknown_text() -> str:
    return "🤔 Nie rozumiem. Użyj przycisków na dole ekranu 👇"


# --- Pomocnicze ---------------------------------------------------------------------------------

def _report_entry(position: int, inv: Investment, distance: Distance | None = None) -> str:
    icon = ("🔥" if inv.priorytet == HOT else "") + CATEGORY_ICONS.get(inv.kategoria or "inna", "•")
    km = distance(inv) if distance else None
    facts = [p for p in (
        f"🚗 {distance_label(km)}" if km is not None else None,
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
        f"🚗 {distance_label(km)}" if km is not None else None,
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
    return f"{trade.label} – przypomnę {trade_window_label(trade)} po pozwoleniu"


def _mode_label(mode: str, settings: BotConfig) -> str:
    if mode == "rano":
        return f"🌅 Raport rano ({settings.morning_time})"
    if mode == "wieczor":
        return f"🌙 Raport wieczorem ({settings.evening_time})"
    return MODE_LABELS.get(mode, mode)


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
