"""Teksty i przyciski bota – „proste jak drut”: krótkie zdania, duże przyciski, zero pisania komend.

Wszystkie funkcje są czyste (zwracają tekst HTML i/lub słownik ``reply_markup``), więc łatwo je testować
i zmieniać bez dotykania logiki bota.
"""

from __future__ import annotations

from typing import Any, Sequence

from .bot_store import BotUser, UserFilters, WatchItem
from .config import BotConfig
from .exporter import CATEGORY_ICONS, escape_html
from .models import Investment
from .scoring import HOT

Markup = dict[str, Any]

MENU_BUTTONS: tuple[str, ...] = (
    "📊 Co nowego?", "🔎 Filtry",
    "⭐ Zapisane", "👀 Obserwowane",
    "⏰ Kiedy wysyłać", "🔥 Tylko HOT",
)

BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("nowe", "📊 Pokaż nowe leady"),
    ("filtry", "🔎 Ustaw, jakie inwestycje chcesz dostawać"),
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
        "• <b>🔎 Filtry</b> – gdzie i jakie inwestycje chcesz dostawać\n"
        "• <b>⏰ Kiedy wysyłać</b> – od razu, raport rano albo wieczorem\n"
        "• <b>🔥 Tylko HOT</b> – tylko najlepsze, duże roboty\n\n"
        "Pod każdym leadem masz przyciski: ⭐ Zapisz, ✅ Przejrzane, 🗑️ Ukryj, 👀 Obserwuj."
    )


def help_text(settings: BotConfig) -> str:
    return (
        "❓ <b>Jak to działa</b>\n\n"
        "Codziennie sprawdzam rejestr pozwoleń na budowę (GUNB) i wysyłam Ci to, co pasuje do filtrów.\n\n"
        "📊 <b>Co nowego?</b> – pokaż nowe leady teraz\n"
        "🔎 <b>Filtry</b> – miejsce, rodzaj budynku, kubatura, inwestor\n"
        "⭐ <b>Zapisane</b> – Twoja lista ciekawych inwestycji\n"
        "👀 <b>Obserwowane</b> – inwestorzy i gminy, o których dostajesz alert od razu\n"
        f"⏰ <b>Kiedy wysyłać</b> – od razu, rano ({settings.morning_time}) albo wieczorem ({settings.evening_time})\n"
        "🔥 <b>Tylko HOT</b> – tylko największe roboty (duża kubatura, bloki, hale, kilka budynków)\n\n"
        "🔥 HOT / 🟡 NORMAL / ⚪ LOW – punkty za kubaturę, rodzaj budynku, liczbę budynków i nową budowę."
    )


def pending_text() -> str:
    return "⏳ Dziękuję! Twoje zgłoszenie czeka na akceptację administratora. Dam znać, gdy dostęp zostanie włączony."


def rejected_text() -> str:
    return "⛔ Administrator nie przyznał dostępu do bota."


def admin_approval(user: BotUser) -> tuple[str, Markup]:
    login = f" (@{escape_html(user.username)})" if user.username else ""
    text = f"🆕 Nowa osoba chce korzystać z bota: <b>{escape_html(user.imie or str(user.chat_id))}</b>{login}"
    return text, inline([[("✅ Wpuść", f"adm:ok:{user.chat_id}"), ("⛔ Odrzuć", f"adm:no:{user.chat_id}")]])


def users_list(users: Sequence[BotUser]) -> str:
    if not users:
        return "Brak użytkowników."
    icons = {"aktywny": "✅", "oczekuje": "⏳", "odrzucony": "⛔", "zablokowany": "🚫"}
    lines = [f"{icons.get(u.status, '•')} {escape_html(u.display_name)} · {MODE_LABELS.get(u.tryb, u.tryb)}"
             for u in users]
    return "👥 <b>Użytkownicy</b>\n" + "\n".join(lines)


# --- Filtry ------------------------------------------------------------------------------------

def filters_screen(user: BotUser, place_names: dict[str, str], settings: BotConfig, prefix: str = "") -> tuple[str, Markup]:
    f = user.filtry
    places = [place_names.get(code, code) for code in f.powiaty] + list(f.miejsca)
    categories = [label for key, label in CATEGORY_CHOICES if key in f.kategorie]
    lines = [
        prefix + "🔎 <b>Twoje filtry</b>" if prefix else "🔎 <b>Twoje filtry</b>",
        f"📍 Miejsce: {escape_html(', '.join(places)) if places else 'wszędzie'}",
        f"🏗️ Rodzaj: {escape_html(', '.join(categories)) if categories else 'wszystkie'}",
        f"📦 Kubatura: {_volume_label(f.min_kubatura)}",
        f"💼 Inwestor: {_investor_label(f.inwestor)}",
        f"🔥 Tylko HOT: {'tak' if user.tylko_hot else 'nie'}",
        f"⏰ Wysyłka: {_mode_label(user.tryb, settings)}",
        "",
        "Kliknij, co chcesz zmienić 👇",
    ]
    markup = inline([
        [("📍 Miejsce", "f:place"), ("🏗️ Rodzaj", "f:type")],
        [("📦 Kubatura", "f:vol"), ("💼 Inwestor", "f:inv")],
        [("🧹 Wyczyść filtry", "f:clear")],
    ])
    return "\n".join(lines), markup


def place_picker(filters: UserFilters, options: Sequence[tuple[str, str]]) -> tuple[str, Markup]:
    rows = [[(("✅ " if code in filters.powiaty else "▫️ ") + label, f"fp:{code}")] for code, label in options]
    rows += [[(f"❌ {place}", f"fpr:{index}")] for index, place in enumerate(filters.miejsca)]
    rows.append([("✏️ Wpisz miejscowość", "fp:txt")])
    rows.append([("✔️ Gotowe", "f:show")])
    text = ("📍 <b>Gdzie szukać?</b>\nZaznacz powiaty albo wpisz miejscowość lub gminę.\n"
            "Nic nie zaznaczone = wszędzie.")
    return text, inline(rows)


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

def lead_keyboard(inv: Investment, *, state: str | None, watching_investor: bool, watching_gmina: bool) -> Markup:
    rows: list[list[dict[str, str]]] = []
    links = [{"text": text, "url": url} for text, url in (("📍 Mapa", inv.google_maps_url),
                                                          ("🏛️ Geoportal", inv.geoportal_url)) if url]
    if links:
        rows.append(links)
    nr = inv.nr
    actions = [
        ("⭐ Zapisany ✓" if state == "zapisany" else "⭐ Zapisz", f"s:{nr}"),
        ("✅ Przejrzany ✓" if state == "przejrzany" else "✅ Przejrzane", f"r:{nr}"),
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

def report(date_label: str, total_new: int, leads: Sequence[Investment], matching: int, hot: int,
           watched: int) -> tuple[str, Markup | None]:
    """Raport zbiorczy: podsumowanie + ponumerowana lista; numery otwierają szczegóły leada."""
    if total_new == 0 and matching == 0:
        return ("📭 <b>Brak nowych inwestycji</b> od ostatniego raportu.\n"
                "Zajrzyj później albo poszerz 🔎 Filtry.", None)
    summary = [f"Znaleziono {_count(total_new, 'nową inwestycję', 'nowe inwestycje', 'nowych inwestycji')}."]
    if matching:
        summary.append(f"{matching} {_plural(matching, 'spełnia', 'spełniają', 'spełnia')} Twoje filtry.")
    else:
        summary.append("Żadna nie spełnia Twoich filtrów.")
    if hot:
        summary.append(f"{hot} to 🔥 {_plural(hot, 'HOT LEAD', 'HOT LEADY', 'HOT LEADÓW')}.")
    if watched:
        summary.append(f"{watched} {_plural(watched, 'dotyczy obserwowanego inwestora lub gminy', 'dotyczą obserwowanych inwestorów lub gmin', 'dotyczy obserwowanych inwestorów lub gmin')}.")
    lines = [f"📊 <b>Raport {date_label}</b>", " ".join(summary[:2]), " ".join(summary[2:])]
    lines = [line for line in lines if line]
    if leads:
        lines.append("")
        for position, inv in enumerate(leads, start=1):
            lines.append(_report_entry(position, inv))
        if matching > len(leads):
            lines.append(f"\n…i {matching - len(leads)} więcej – zawęź 🔎 Filtry, żeby widzieć najlepsze.")
        lines.append("\n👇 Kliknij numer, żeby zobaczyć szczegóły i zapisać.")
        markup = inline(number_buttons([(position, inv.nr) for position, inv in enumerate(leads, start=1)]))
        return "\n".join(lines), markup
    return "\n".join(lines), None


def saved_list(leads: Sequence[Investment], page: int, total: int) -> tuple[str, Markup | None]:
    if total == 0:
        return "⭐ Nie masz jeszcze zapisanych leadów.\nKliknij <b>⭐ Zapisz</b> pod ciekawą inwestycją.", None
    first = page * SAVED_PAGE_SIZE + 1
    lines = [f"⭐ <b>Zapisane</b> ({total})", ""]
    lines += [_report_entry(first + offset, inv) for offset, inv in enumerate(leads)]
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

def _report_entry(position: int, inv: Investment) -> str:
    icon = ("🔥" if inv.priorytet == HOT else "") + CATEGORY_ICONS.get(inv.kategoria or "inna", "•")
    facts = [p for p in (
        inv.adres_opisowy or inv.miejscowosc,
        f"{_thousands(inv.kubatura)} m³" if inv.kubatura else None,
        _short_date(inv.data_aktualizacji),
    ) if p]
    title = escape_html(_short(inv.nazwa_zamierzenia or "(brak opisu)", 90))
    return f"{position}. {icon} <b>{title}</b>\n    {escape_html(' · '.join(facts))}"


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
