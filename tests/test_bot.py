"""Zachowanie bota z perspektywy użytkownika („Pan Mietek”) – atrapa API Telegrama, prawdziwa baza SQLite."""

from datetime import datetime, timedelta, timezone

import pytest

from gunb_tool.bot import MENU_BUTTONS, LeadBot
from gunb_tool.bot_store import BotStore, UserFilters
from gunb_tool.config import BotConfig
from gunb_tool.exporter import MessageFormatter
from gunb_tool.models import Investment
from gunb_tool.storage import LeadRepository
from gunb_tool.telegram_api import TelegramApiError

ADMIN, MIETEK, OBCY = 1001, 2002, 3003


class Clock:
    """Wspólny zegar: UTC dla bazy, czas lokalny (naiwny) dla harmonogramu bota."""

    def __init__(self) -> None:
        self.utc = datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc)

    def now_utc(self) -> datetime:
        return self.utc

    def now_local(self) -> datetime:
        return (self.utc + timedelta(hours=2)).replace(tzinfo=None)

    def advance(self, **delta) -> None:
        self.utc += timedelta(**delta)


class FakeApi:
    """Rejestruje wywołania API Telegrama; ``blocked`` = czaty, które zablokowały bota."""

    def __init__(self) -> None:
        self.sent, self.edits, self.answers, self.commands = [], [], [], []
        self.blocked: set[int] = set()
        self._message_id = 500

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.blocked:
            raise TelegramApiError("sendMessage", 403, "Forbidden: bot was blocked by the user")
        self._message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "markup": reply_markup, "message_id": self._message_id})
        return {"message_id": self._message_id}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "markup": reply_markup})

    def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": None, "markup": reply_markup})

    def answer_callback_query(self, callback_query_id, text=None):
        self.answers.append(text)

    def set_my_commands(self, commands):
        self.commands = commands

    # pomocnicze dla testów
    def last_to(self, chat_id):
        return [m for m in self.sent if m["chat_id"] == chat_id][-1]

    def to(self, chat_id):
        return [m for m in self.sent if m["chat_id"] == chat_id]


def buttons(markup):
    """Płaska lista (tekst, callback_data | url) z klawiatury inline."""
    return [(b["text"], b.get("callback_data") or b.get("url")) for row in (markup or {}).get("inline_keyboard", [])
            for b in row]


def callback_for(markup, text_fragment):
    return next(data for text, data in buttons(markup) if text_fragment in text)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def repo(clock):
    repository = LeadRepository(":memory:", now=clock.now_utc)
    yield repository
    repository.close()


@pytest.fixture
def api():
    return FakeApi()


def make_bot(repo, api, clock, **settings):
    params = dict(admins=(ADMIN,), access="approval", fetch_times=(), morning_time="07:00", evening_time="19:00",
                  instant_every_minutes=10, welcome_backlog_days=7, max_leads_in_report=20)
    params.update(settings)
    return LeadBot(repo, api, settings=BotConfig(**params), powiat_codes=("1465", "3021"),
                   formatter=MessageFormatter(), clock=clock.now_local)


@pytest.fixture
def bot(repo, api, clock):
    return make_bot(repo, api, clock)


def message(chat_id, text, first_name="Mietek"):
    return {"update_id": 1, "message": {"message_id": 1, "text": text,
                                        "chat": {"id": chat_id, "type": "private"},
                                        "from": {"id": chat_id, "first_name": first_name, "username": None}}}


def click(chat_id, data, message_id=777):
    return {"update_id": 2, "callback_query": {"id": "cb", "data": data, "from": {"id": chat_id},
                                               "message": {"message_id": message_id, "chat": {"id": chat_id}}}}


def lead(id_sprawy, **overrides) -> Investment:
    base = dict(
        id_sprawy=id_sprawy, zrodlo="pozwolenia", status="decyzja", kategoria="mieszkaniowa-jednorodzinna",
        nazwa_zamierzenia="Budowa budynku mieszkalnego jednorodzinnego", kubatura=878.0, priorytet="normal",
        punkty=3, adres_opisowy="Wróblewo", miejscowosc="Wróblewo", gmina="Kostrzyn", powiat="powiat poznański",
        powiat_teryt="3021", gmina_teryt="3021085", data_aktualizacji="2026-09-28",
        google_maps_url="https://www.google.com/maps?q=52.379110,17.211380",
    )
    base.update(overrides)
    return Investment(**base)


BIG_WARSAW = dict(kategoria="mieszkaniowa-wielorodzinna", kubatura=26265.0, priorytet="hot", punkty=10,
                  nazwa_zamierzenia="Budowa zespołu dwóch budynków wielorodzinnych",
                  adres_opisowy="Warszawa", miejscowosc="Warszawa", gmina="Warszawa (miasto)",
                  powiat="powiat Warszawa", powiat_teryt="1465", gmina_teryt="1465038",
                  inwestor="Napollo 3 Sp. z o.o.")


def activate(bot, api, chat_id=MIETEK):
    bot.handle_update(message(chat_id, "/start"))
    if chat_id != ADMIN:
        bot.handle_update(click(ADMIN, f"adm:ok:{chat_id}"))
    api.sent.clear()
    api.edits.clear()
    api.answers.clear()


# --- Rejestracja i menu ------------------------------------------------------------------------

def test_admin_is_active_at_once_and_gets_big_menu_buttons(bot, api):
    bot.handle_update(message(ADMIN, "/start"))
    welcome = api.last_to(ADMIN)
    keyboard = [button["text"] for row in welcome["markup"]["keyboard"] for button in row]
    assert keyboard == list(MENU_BUTTONS)
    assert welcome["markup"]["resize_keyboard"] is True


def test_stranger_waits_for_admin_approval(bot, api):
    bot.handle_update(message(MIETEK, "/start"))
    assert "akceptac" in api.last_to(MIETEK)["text"]
    approval = api.last_to(ADMIN)
    assert "Mietek" in approval["text"]
    assert buttons(approval["markup"]) == [("✅ Wpuść", f"adm:ok:{MIETEK}"), ("⛔ Odrzuć", f"adm:no:{MIETEK}")]

    bot.handle_update(message(MIETEK, "🔎 Filtry"))
    assert "akceptac" in api.last_to(MIETEK)["text"]

    bot.handle_update(click(ADMIN, f"adm:ok:{MIETEK}"))
    assert BotStore(bot.repo).get_user(MIETEK).status == "aktywny"
    assert api.last_to(MIETEK)["markup"]["keyboard"]


def test_rejected_user_is_informed(bot, api):
    bot.handle_update(message(OBCY, "/start"))
    bot.handle_update(click(ADMIN, f"adm:no:{OBCY}"))
    assert BotStore(bot.repo).get_user(OBCY).status == "odrzucony"
    assert "⛔" in api.last_to(OBCY)["text"]


def test_non_admin_cannot_approve(bot, api):
    activate(bot, api)
    bot.handle_update(message(OBCY, "/start"))
    bot.handle_update(click(MIETEK, f"adm:ok:{OBCY}"))
    assert BotStore(bot.repo).get_user(OBCY).status == "oczekuje"


def test_open_access_activates_everyone(repo, api, clock):
    bot = make_bot(repo, api, clock, access="open")
    bot.handle_update(message(OBCY, "/start"))
    assert BotStore(repo).get_user(OBCY).status == "aktywny"


def test_unknown_text_shows_help_with_menu(bot, api):
    activate(bot, api)
    bot.handle_update(message(MIETEK, "dzień dobry"))
    assert api.last_to(MIETEK)["markup"]["keyboard"]


# --- Filtry przyciskami -------------------------------------------------------------------------

def test_filters_are_set_with_buttons_and_one_typed_place(bot, api):
    activate(bot, api)
    bot.handle_update(message(MIETEK, "🔎 Filtry"))
    screen = api.last_to(MIETEK)
    assert "Twoje filtry" in screen["text"]

    bot.handle_update(click(MIETEK, callback_for(screen["markup"], "Rodzaj")))
    types = api.edits[-1]["markup"]
    bot.handle_update(click(MIETEK, callback_for(types, "Bloki")))
    assert "✅" in [t for t, _ in buttons(api.edits[-1]["markup"]) if "Bloki" in t][0]

    bot.handle_update(click(MIETEK, "f:vol"))
    bot.handle_update(click(MIETEK, callback_for(api.edits[-1]["markup"], "10 000")))

    bot.handle_update(click(MIETEK, "f:place"))
    bot.handle_update(click(MIETEK, callback_for(api.edits[-1]["markup"], "Wpisz")))
    assert "Napisz" in api.last_to(MIETEK)["text"]
    bot.handle_update(message(MIETEK, "Warszawa"))

    assert BotStore(bot.repo).get_user(MIETEK).filtry == UserFilters(
        miejsca=("Warszawa",), kategorie=("mieszkaniowa-wielorodzinna",), min_kubatura=10000)
    assert "Warszawa" in api.last_to(MIETEK)["text"]


def test_powiat_can_be_toggled_from_list(bot, api, repo):
    repo.upsert(lead("A/1"))
    activate(bot, api)
    bot.handle_update(click(MIETEK, "f:place"))
    places = buttons(api.edits[-1]["markup"])
    assert ("▫️ powiat poznański", "fp:3021") in places
    bot.handle_update(click(MIETEK, "fp:3021"))
    assert BotStore(repo).get_user(MIETEK).filtry.powiaty == ("3021",)


def test_clear_filters(bot, api):
    activate(bot, api)
    BotStore(bot.repo).set_filters(MIETEK, UserFilters(min_kubatura=5000))
    bot.handle_update(click(MIETEK, "f:clear"))
    assert BotStore(bot.repo).get_user(MIETEK).filtry.is_empty()


# --- Raport i przyciski pod leadem ------------------------------------------------------------------

def seed_leads(repo):
    repo.upsert(lead("WAW/1", **BIG_WARSAW))
    repo.upsert(lead("DOM/1"))
    repo.upsert(lead("HALA/1", kategoria="komercyjna", kubatura=5000.0, priorytet="hot", punkty=7,
                     nazwa_zamierzenia="Budowa hali magazynowej"))


def test_report_summarises_and_numbers_matching_leads(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    BotStore(repo).set_filters(MIETEK, UserFilters(miejsca=("Warszawa",), kategorie=("mieszkaniowa-wielorodzinna",),
                                                   min_kubatura=10000))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))

    report = api.last_to(MIETEK)
    assert "📊 <b>Raport 29.09</b>" in report["text"]
    assert "Znaleziono 3 nowe inwestycje. 1 spełnia Twoje filtry." in report["text"]
    assert "1 to 🔥 HOT LEAD." in report["text"]
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in report["text"]
    nr = repo.get("WAW/1").nr
    assert buttons(report["markup"]) == [("1", f"o:{nr}")]

    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    again = api.last_to(MIETEK)["text"]  # nic nowego → pasujące z ostatnich dni (już widziane)
    assert "Nic nowego" in again
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in again


def test_lead_card_has_action_buttons_and_save_updates_them(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("WAW/1").nr
    bot.handle_update(click(MIETEK, f"o:{nr}"))

    card = api.last_to(MIETEK)
    assert card["text"].startswith("🔥 <b>HOT LEAD</b>")
    labels = [t for t, _ in buttons(card["markup"])]
    assert labels[:2] == ["📍 Mapa", "🏛️ Geoportal"] or labels[0] == "📍 Mapa"
    assert {"⭐ Zapisz", "✅ Przejrzane", "🗑️ Ukryj", "👀 Obserwuj inwestora", "📌 Obserwuj gminę"} <= set(labels)

    bot.handle_update(click(MIETEK, f"s:{nr}", message_id=card["message_id"]))
    assert api.answers[-1].startswith("⭐ Zapisano")
    assert "⭐ Zapisany ✓" in [t for t, _ in buttons(api.edits[-1]["markup"])]

    bot.handle_update(message(MIETEK, "⭐ Zapisane"))
    saved = api.last_to(MIETEK)
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in saved["text"]
    assert (f"1", f"o:{nr}") in buttons(saved["markup"])


def test_hide_collapses_card_and_undo_restores_it(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    nr = repo.get("DOM/1").nr
    bot.handle_update(click(MIETEK, f"h:{nr}"))
    assert "Ukryte" in api.edits[-1]["text"]
    assert BotStore(repo).lead_state(MIETEK, "DOM/1") == "ukryty"
    bot.handle_update(click(MIETEK, f"u:{nr}"))
    assert BotStore(repo).lead_state(MIETEK, "DOM/1") is None
    assert "Budowa budynku mieszkalnego" in api.edits[-1]["text"]


def test_hot_only_toggle_filters_report(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "🔥 Tylko HOT"))
    assert "tylko 🔥 HOT" in api.last_to(MIETEK)["text"]
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    report = api.last_to(MIETEK)["text"]
    assert "2 spełniają Twoje filtry" in report
    assert "Budowa budynku mieszkalnego jednorodzinnego" not in report


# --- Watchlista ---------------------------------------------------------------------------------

def test_watched_investor_triggers_instant_alert_even_in_morning_mode(bot, api, repo, clock):
    activate(bot, api)
    repo.upsert(lead("WAW/1", **BIG_WARSAW))
    bot.handle_update(click(MIETEK, f"wi:{repo.get('WAW/1').nr}"))
    assert api.answers[-1].startswith("👀 Obserwujesz")
    BotStore(repo).record_delivery(MIETEK, [repo.get("WAW/1")], "raport")

    clock.advance(hours=1)
    repo.upsert(lead("WAW/2", **{**BIG_WARSAW, "inwestor": "Napollo 4 Sp. z o.o.",
                                 "nazwa_zamierzenia": "Budowa budynku biurowego"}))
    bot.deliver_instant()

    alert = api.last_to(MIETEK)
    assert alert["text"].startswith("👀 <b>WATCHLISTA</b> · nowa inwestycja obserwowanego inwestora")
    clock.advance(hours=1)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    assert "1 dotyczy obserwowanego inwestora" in api.last_to(MIETEK)["text"]


def test_watchlist_screen_allows_removal(bot, api, repo):
    activate(bot, api)
    repo.upsert(lead("DOM/1"))
    bot.handle_update(click(MIETEK, f"wg:{repo.get('DOM/1').nr}"))
    bot.handle_update(message(MIETEK, "👀 Obserwowane"))
    screen = api.last_to(MIETEK)
    assert "Kostrzyn" in screen["text"]
    bot.handle_update(click(MIETEK, callback_for(screen["markup"], "Kostrzyn")))
    assert BotStore(repo).watchlist(MIETEK) == []


# --- Tryby wysyłki i harmonogram -------------------------------------------------------------------

def test_instant_mode_sends_cards_and_digest_above_threshold(bot, api, repo):
    activate(bot, api)
    bot.handle_update(message(MIETEK, "⏰ Kiedy wysyłać"))
    bot.handle_update(click(MIETEK, "m:natychmiast"))
    assert BotStore(repo).get_user(MIETEK).tryb == "natychmiast"

    seed_leads(repo)
    api.sent.clear()
    bot.deliver_instant()
    assert len(api.to(MIETEK)) == 3
    assert all("⭐ Zapisz" in [t for t, _ in buttons(m["markup"])] for m in api.to(MIETEK))

    for n in range(12):
        repo.upsert(lead(f"NOWY/{n}"))
    api.sent.clear()
    bot.deliver_instant()
    (report,) = api.to(MIETEK)
    assert "📊 <b>Raport" in report["text"]


def test_morning_report_runs_once_per_day(bot, api, repo, clock):
    activate(bot, api)
    seed_leads(repo)
    clock.utc = datetime(2026, 9, 29, 5, 5, tzinfo=timezone.utc)  # 07:05 czasu lokalnego
    assert "raport_rano" in bot.run_due_jobs()
    assert "📊 <b>Raport" in api.last_to(MIETEK)["text"]
    sent = len(api.sent)
    assert "raport_rano" not in bot.run_due_jobs()
    assert len(api.sent) == sent


def test_blocked_user_is_marked_and_skipped(bot, api, repo):
    activate(bot, api)
    BotStore(repo).set_mode(MIETEK, "natychmiast")
    seed_leads(repo)
    api.blocked.add(MIETEK)
    bot.deliver_instant()
    assert BotStore(repo).get_user(MIETEK).status == "zablokowany"


def test_bot_commands_are_registered_on_setup(bot, api):
    bot.setup()
    assert ("filtry", "🔎 Ustaw, jakie inwestycje chcesz dostawać") in api.commands


def test_long_report_is_shortened_to_fit_telegram_limit(bot, api, repo):
    activate(bot, api)
    for n in range(40):
        repo.upsert(lead(f"DLUGI/{n}", nazwa_zamierzenia="Budowa zespołu budynków " + "bardzo długi opis " * 10,
                         adres_opisowy="ul. " + "Bardzo Długa Nazwa Ulicy " * 5 + f"{n}, 00-001 Warszawa"))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    report = api.last_to(MIETEK)
    assert len(report["text"]) <= 4096
    assert "więcej" in report["text"]



# --- Zgłoszenie z testu na żywo: „ustawiłem filtry, a raport pusty” ----------------------------------

def test_filters_set_after_unfiltered_report_still_show_matching_leads(bot, api, repo):
    """Odtworzenie testu na żywo: najpierw raport bez filtrów, potem filtry „Warszawa + bloki + 10 000 m³”."""
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))          # raport bez filtrów
    BotStore(repo).set_filters(MIETEK, UserFilters(miejsca=("Warszawa",), kategorie=("mieszkaniowa-wielorodzinna",),
                                                   min_kubatura=10000))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))

    text = api.last_to(MIETEK)["text"]
    assert "Budowa zespołu dwóch budynków wielorodzinnych" in text
    assert "Brak nowych" not in text


def test_only_leads_shown_in_report_count_as_seen(repo, api, clock):
    bot = make_bot(repo, api, clock, max_leads_in_report=2)
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    first = api.last_to(MIETEK)
    assert len(buttons(first["markup"])) == 2
    assert "więcej" in first["text"]

    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    second = api.last_to(MIETEK)
    assert "Nic nowego" not in second["text"]
    assert len(buttons(second["markup"])) == 1  # trzeci lead, którego nie było na pierwszej liście


def test_filters_screen_offers_show_matching_button(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "🔎 Filtry"))
    screen = api.last_to(MIETEK)
    bot.handle_update(click(MIETEK, callback_for(screen["markup"], "Pokaż pasujące")))
    assert "📊 <b>Raport" in api.last_to(MIETEK)["text"]


def test_nothing_matching_in_recent_days_says_so(bot, api, repo):
    activate(bot, api)
    seed_leads(repo)
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    BotStore(repo).set_filters(MIETEK, UserFilters(miejsca=("Gdańsk",)))
    bot.handle_update(message(MIETEK, "📊 Co nowego?"))
    text = api.last_to(MIETEK)["text"]
    assert "Nic nowego" in text
    assert "brak pasujących" in text
