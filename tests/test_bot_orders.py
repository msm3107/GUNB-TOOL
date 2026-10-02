"""P0: oferta i ręczny zakup – zamówienie, informacja dla admina, potwierdzenie rzeczywiście otrzymanej
płatności (dokładnie raz), przedłużenie dostępu, potwierdzenie dla klienta. Dostęp nadany ręcznie to nie płatność."""

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from gunb_tool.bot_store import BotStore
from gunb_tool.config import OfferConfig
from tests.bot_helpers import ADMIN, MIETEK, OBCY, buttons, click, configured, make_bot, message

OFFER = OfferConfig(area="Olsztyn i powiat olsztyński", price=Decimal("99"), tax="netto_vat", vat_rate=23,
                    period_days=30, payment="przelew – numer konta prześlę w wiadomości",
                    seller_name="Jan Przykładowy", seller_contact="@jan_przyklad",
                    renewal="Dostęp nie odnawia się sam.")


@pytest.fixture
def bot(repo, api, clock):
    return make_bot(repo, api, clock, offer=OFFER)


def store(repo):
    return BotStore(repo)


def on_trial(bot, api, chat_id=MIETEK):
    bot.handle_update(message(chat_id, "/start"))
    configured(bot, chat_id)
    bot.handle_update(click(ADMIN, f"adm:trial:{chat_id}"))
    bot.handle_update(click(chat_id, "ts"))
    api.sent.clear()
    api.edits.clear()
    api.answers.clear()


def labels(markup):
    return [text for text, _ in buttons(markup)]


def order_card(api):
    return next(m for m in reversed(api.to(ADMIN)) if m["text"].startswith("🛒 <b>Zamówienie"))


def test_order_is_taken_once_and_the_admin_is_told(bot, api, repo):
    on_trial(bot, api)

    bot.handle_update(click(MIETEK, "zm:new"))
    bot.handle_update(click(MIETEK, "zm:new"))  # drugie kliknięcie – to samo zamówienie

    orders = store(repo).orders()
    assert len(orders) == 1 and orders[0].stan == "zgloszone"
    confirmation = api.last_to(MIETEK)["text"]
    assert f"Zamówienie {orders[0].number}" in confirmation and "121,77 zł" in confirmation
    assert "przelew – numer konta prześlę w wiadomości" in confirmation
    assert "To nie jest faktura ani potwierdzenie płatności" in confirmation
    cards = [m for m in api.to(ADMIN) if m["text"].startswith("🛒 <b>Zamówienie")]
    assert len(cards) == 1
    assert {("✅ Płatność otrzymana", f"adm:pay:{orders[0].id}"), ("✖️ Anuluj", f"adm:cancel:{orders[0].id}")} \
        <= set(buttons(cards[0]["markup"]))
    assert [e.rodzaj for e in store(repo).events(kinds=("zamowienie",))] == ["zamowienie"]


def test_confirmed_payment_extends_access_exactly_once(bot, api, repo, clock):
    on_trial(bot, api)
    trial_end = store(repo).get_user(MIETEK).subscription_ends
    bot.handle_update(click(MIETEK, "zm:new"))
    order = store(repo).orders()[0]

    bot.handle_update(click(ADMIN, f"adm:pay:{order.id}"))
    bot.handle_update(click(ADMIN, f"adm:pay:{order.id}"))  # podwójne kliknięcie admina

    person = store(repo).get_user(MIETEK)
    expected = (datetime.fromisoformat(trial_end) + timedelta(days=30)).isoformat(timespec="seconds")
    assert person.subscription_ends == expected and person.rodzaj_dostepu == "platny"  # dni testu nie przepadają
    assert store(repo).get_order(order.id).stan == "oplacone"
    assert len(store(repo).events(kinds=("platnosc",))) == 1
    assert sum("Płatność za zamówienie" in m["text"] for m in api.to(MIETEK)) == 1
    assert api.answers[-1].startswith("ℹ️ Zamówienie") and "już opłacone" in api.answers[-1]


def test_payment_can_be_confirmed_by_command_with_a_note(bot, api, repo):
    on_trial(bot, api)
    bot.handle_update(click(MIETEK, "zm:new"))
    order = store(repo).orders()[0]

    bot.handle_update(message(ADMIN, f"/zaplacone {order.number} przelew 02.10"))
    bot.handle_update(message(ADMIN, f"/zaplacone {order.id}"))

    assert store(repo).get_order(order.id).uwagi == "przelew 02.10"
    assert "już opłacone" in api.last_to(ADMIN)["text"]
    assert len(store(repo).events(kinds=("platnosc",))) == 1


def test_cancelled_order_is_never_paid(bot, api, repo):
    on_trial(bot, api)
    bot.handle_update(click(MIETEK, "zm:new"))
    order = store(repo).orders()[0]
    bot.handle_update(click(ADMIN, f"adm:cancel:{order.id}"))
    bot.handle_update(click(ADMIN, f"adm:pay:{order.id}"))
    assert store(repo).get_order(order.id).stan == "anulowane"
    assert store(repo).get_user(MIETEK).rodzaj_dostepu == "test"
    assert "anulowane" in api.last_to(MIETEK)["text"]


def test_second_paid_order_is_a_renewal(bot, api, repo):
    on_trial(bot, api)
    for _ in range(2):
        bot.handle_update(click(MIETEK, "zm:new"))
        bot.handle_update(click(ADMIN, f"adm:pay:{store(repo).orders()[0].id}"))
    assert [e.rodzaj for e in store(repo).events(kinds=("platnosc", "odnowienie"))] == \
        ["platnosc", "platnosc", "odnowienie"]


def test_manual_access_is_never_presented_as_a_payment(bot, api, repo):
    bot.handle_update(message(OBCY, "/start"))
    configured(bot, OBCY)
    bot.handle_update(message(ADMIN, f"/aktywuj {OBCY} 30"))
    granted = api.last_to(OBCY)["text"]
    bot.handle_update(message(OBCY, "/konto"))
    account = api.last_to(OBCY)["text"]
    bot.handle_update(message(ADMIN, "/start"))  # lista użytkowników to ekran dla zarejestrowanego admina
    bot.handle_update(message(ADMIN, "/uzytkownicy"))

    assert store(repo).get_user(OBCY).rodzaj_dostepu == "reczny"
    for text in (granted, account):
        assert not any(word in text.lower() for word in ("płatn", "opłac", "abonament")), text
    assert "🔑 ręcznie do" in api.last_to(ADMIN)["text"]
    assert store(repo).events(kinds=("platnosc",)) == []


def test_account_shows_the_offer_and_the_last_confirmed_payment(bot, api, repo):
    on_trial(bot, api)
    bot.handle_update(message(MIETEK, "/konto"))
    account = api.last_to(MIETEK)
    assert "99 zł netto" in account["text"] and "🛒 Zamawiam" in labels(account["markup"])

    bot.handle_update(click(MIETEK, "zm:new"))
    order = store(repo).orders()[0]
    bot.handle_update(click(ADMIN, f"adm:pay:{order.id}"))
    bot.handle_update(message(MIETEK, "/konto"))
    assert f"💳 Ostatnia potwierdzona płatność: zamówienie {order.number}" in api.last_to(MIETEK)["text"]


def test_offer_comes_before_the_end_and_after_it(bot, api, repo, clock):
    on_trial(bot, api)
    clock.advance(days=6, hours=1)  # dzień przed końcem testu
    bot.run_due_jobs()
    reminder = next(m for m in api.to(MIETEK) if "kończy się" in m["text"])
    clock.advance(days=1)
    bot.run_due_jobs()
    ended = next(m for m in api.to(MIETEK) if m["text"].startswith("⛔"))

    assert "kończy się" in reminder["text"] and "99 zł netto" in reminder["text"]
    assert "🛒 Zamawiam" in labels(reminder["markup"])
    assert ended["text"].startswith("⛔ Twój darmowy test skończył się")
    assert "⭐ zapisane i notatki zostają" in ended["text"] and "🛒 Zamawiam" in labels(ended["markup"])


def test_inquiry_reaches_the_admin_at_most_once_a_day(bot, api, repo, clock):
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(click(MIETEK, "zm:q"))
    bot.handle_update(click(MIETEK, "zm:q"))
    assert sum(m["text"].startswith("💬 Pytanie o ofertę") for m in api.to(ADMIN)) == 1
    clock.advance(days=1, minutes=1)
    bot.handle_update(click(MIETEK, "zm:q"))
    assert sum(m["text"].startswith("💬 Pytanie o ofertę") for m in api.to(ADMIN)) == 2


def test_admin_can_answer_through_the_bot_and_list_orders(bot, api, repo):
    on_trial(bot, api)
    bot.handle_update(click(MIETEK, "zm:new"))
    bot.handle_update(message(ADMIN, f"/napisz {MIETEK} Numer konta: 00 1111 <b>test</b>"))
    assert api.last_to(MIETEK)["text"].endswith("Numer konta: 00 1111 &lt;b&gt;test&lt;/b&gt;")

    bot.handle_update(message(ADMIN, "/zamowienia"))
    listing = api.last_to(ADMIN)["text"]
    assert store(repo).orders()[0].number in listing and "121,77 zł" in listing


def test_admin_message_keeps_its_lines(bot, api, repo):
    """Dane do przelewu w kilku liniach dochodzą w kilku liniach (wcześniej wszystko sklejało się w jedną)."""
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/napisz {MIETEK} Dane do przelewu:\nJan Przykładowy\nPL 12 3456 7890\n"
                                     "Tytuł: Z-1"))
    assert api.last_to(MIETEK)["text"].endswith("Dane do przelewu:\nJan Przykładowy\nPL 12 3456 7890\nTytuł: Z-1")


def test_too_long_admin_message_goes_back_to_the_admin_instead_of_being_cut(bot, api, repo):
    bot.handle_update(message(MIETEK, "/start"))
    api.sent.clear()
    bot.handle_update(message(ADMIN, f"/napisz {MIETEK} " + "A & B " * 700))  # po ucieczce HTML ponad 4096 znaków
    assert api.to(MIETEK) == []
    assert "za długa" in api.last_to(ADMIN)["text"]


def test_long_orders_list_comes_in_several_messages_telegram_accepts(bot, api, repo):
    bot.handle_update(message(ADMIN, "/start"))
    for n in range(30):
        chat_id = 6_000_000_000 + n
        bot.handle_update(message(chat_id, "/start", first_name=f"Hurtownia Materiałów Budowlanych {n:02d} " + "x" * 25))
        bot.handle_update(click(chat_id, "zm:new"))
    api.sent.clear()
    bot.handle_update(message(ADMIN, "/zamowienia"))
    parts = [m["text"] for m in api.to(ADMIN)]
    assert len(parts) > 1
    assert all(f"• {order.number} · " in "\n".join(parts) for order in store(repo).orders(limit=50))


def test_payment_received_outside_the_order_button_is_recorded_as_a_payment(bot, api, repo):
    """Klient zapłacił po rozmowie, bez „🛒 Zamawiam”: /wplata zakłada zamówienie z bieżącej oferty i od razu je
    potwierdza – płatność nie ląduje w „dostępie ręcznym” (/aktywuj), więc /raport liczy ją jako płatność."""
    on_trial(bot, api)
    bot.handle_update(message(ADMIN, f"/wplata {MIETEK} przelew po rozmowie"))
    orders = store(repo).orders()
    assert len(orders) == 1 and orders[0].stan == "oplacone" and orders[0].uwagi == "przelew po rozmowie"
    assert orders[0].opis_ceny == "99 zł netto + 23% VAT = 121,77 zł do zapłaty"
    assert store(repo).get_user(MIETEK).rodzaj_dostepu == "platny"
    assert f"Płatność za zamówienie {orders[0].number} potwierdzona" in api.last_to(MIETEK)["text"]
    assert api.last_to(ADMIN)["text"].startswith(f"✅ {orders[0].number} opłacone")
    kinds = [e.rodzaj for e in store(repo).events(kinds=("zamowienie", "platnosc"))]
    assert kinds == ["zamowienie", "platnosc"]


def test_payment_by_person_confirms_the_open_order_instead_of_making_a_second_one(bot, api, repo):
    on_trial(bot, api)
    bot.handle_update(click(MIETEK, "zm:new"))
    bot.handle_update(message(ADMIN, f"/wplata {MIETEK}"))
    bot.handle_update(message(ADMIN, f"/wplata {MIETEK}"))  # drugi raz – nowe zamówienie i kolejne 30 dni
    orders = store(repo).orders()
    assert [o.stan for o in orders] == ["oplacone", "oplacone"]
    assert len([e for e in store(repo).events(kinds=("zamowienie",))]) == 2


def test_payment_by_person_needs_a_complete_offer(repo, api, clock):
    bot = make_bot(repo, api, clock)  # oferta niepełna – nie wiadomo, za ile i na ile dni
    bot.handle_update(message(MIETEK, "/start"))
    bot.handle_update(message(ADMIN, f"/wplata {MIETEK}"))
    assert store(repo).orders() == []
    reply = api.last_to(ADMIN)["text"]
    assert "OFERTA_" in reply and f"/aktywuj {MIETEK}" in reply


def test_orders_and_admin_commands_are_only_for_the_admin(bot, api, repo):
    on_trial(bot, api)
    bot.handle_update(click(MIETEK, "zm:new"))
    order = store(repo).orders()[0]
    bot.handle_update(click(MIETEK, f"adm:pay:{order.id}"))
    bot.handle_update(message(MIETEK, f"/zaplacone {order.id}"))
    bot.handle_update(message(MIETEK, f"/wplata {MIETEK}"))
    assert store(repo).get_order(order.id).stan == "zgloszone" and len(store(repo).orders()) == 1
    assert api.answers[-1].startswith("⛔ Tylko administrator")
