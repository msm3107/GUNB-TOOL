"""Cała droga lokalnie (bez sieci): nowa osoba → przykład → konfiguracja → test → pierwsza wartość → zamówienie
→ potwierdzenie płatności → aktywny dostęp; osobno osoba, która nie kupuje. Każda wiadomość mieści się w limicie
Telegrama i ma poprawny HTML; nic nie przychodzi podwójnie; dane osób są rozdzielone."""

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gunb_tool.bot_store import BotStore, Outcome
from gunb_tool.clock import WARSAW
from gunb_tool.config import OfferConfig
from gunb_tool.exporter import TELEGRAM_LIMIT
from tests.bot_helpers import ADMIN, MIETEK, OBCY, buttons, click, lead, make_bot, message

OFFER = OfferConfig(area="Olsztyn i powiat olsztyński", price=Decimal("99"), tax="netto_vat", vat_rate=23,
                    period_days=30, payment="przelew – dane prześlę w wiadomości", seller_name="Jan & <Syn>",
                    seller_contact="@jan_przyklad", response_time="w dni robocze 16:00–20:00")
DYWITY = dict(adres_opisowy="Dywity, ul. Polna 5", miejscowosc="Dywity", gmina="Dywity", powiat="olsztyński",
              powiat_teryt="1465", gmina_teryt="1465032", lat=53.8285, lon=20.4867, precyzja_geo="dzialka",
              google_maps_url="https://www.google.com/maps?q=53.8285,20.4867")


def decided(days_ago: int) -> dict:
    day = (date(2026, 9, 29) - timedelta(days=days_ago)).isoformat()
    return dict(data_decyzji=day, data_aktualizacji=day)


def at(clock, month: int, day: int, hour: int) -> None:
    clock.utc = datetime(2026, month, day, hour, 0, tzinfo=WARSAW).astimezone(timezone.utc)


@pytest.fixture
def bot(repo, api, clock):
    bot = make_bot(repo, api, clock, offer=OFFER)
    for n in range(7):
        repo.upsert(lead(f"J/{n}", nazwa_zamierzenia=f"Budowa domu numer {n} <z garażem> & tarasem",
                         **DYWITY, **decided(n * 2 + 1)))
    repo.upsert(lead("J/OLD", nazwa_zamierzenia="Starsza budowa w oknie dachu", **DYWITY, **decided(150)))
    BotStore(repo).set_job_time("import_udany", clock.now_utc())
    return bot


def texts(api, chat_id):
    return [m["text"] for m in api.to(chat_id)] + [e["text"] for e in api.edits if e["chat_id"] == chat_id and e["text"]]


def assert_telegram_safe(messages):
    for text in messages:
        assert len(text) <= TELEGRAM_LIMIT, text[:80]
        for tag in ("b", "code", "a"):
            assert len(re.findall(f"<{tag}[ >]", text)) == text.count(f"</{tag}>"), (tag, text[:120])
        stripped = re.sub(r"</?(b|code)>|<a href=\"[^\"]*\">|</a>", "", text)
        assert "<" not in stripped and ">" not in stripped, stripped[:120]  # reszta ucieczkowana
        assert not re.search(r"&(?!amp;|lt;|gt;|quot;)", stripped), stripped[:120]


def new_person_until_trial(bot, api, chat_id, *, trade, area_text):
    bot.handle_update(message(chat_id, "/start strona"))
    bot.handle_update(click(chat_id, "i:demo"))
    bot.handle_update(click(chat_id, "i:oferta"))
    bot.handle_update(click(chat_id, "i:test"))
    request = next(m for m in api.to(ADMIN)
                   if m["text"].startswith("🙋 Prośba o test") and f"<code>{chat_id}</code>" in m["text"])
    bot.handle_update(click(ADMIN, dict((t, d) for t, d in buttons(request["markup"]))["🎁 Test 7 dni"]))
    bot.handle_update(click(chat_id, f"ob:{trade}"))
    bot.handle_update(click(chat_id, "oa:txt"))
    bot.handle_update(message(chat_id, area_text))
    summary = api.last_to(chat_id)
    assert "🔎 Teraz pasuje:" in summary["text"] and ("▶️ Zacznij 7-dniowy test", "ts") in buttons(summary["markup"])
    bot.handle_update(click(chat_id, "ts"))


def test_buyer_from_first_contact_to_paid_access(bot, api, repo, clock):
    store = BotStore(repo)
    new_person_until_trial(bot, api, MIETEK, trade="dach", area_text="Dywity")

    first = api.last_to(MIETEK)
    assert first["text"].startswith("⭐ <b>Na początek")
    assert first["text"].index("Starsza budowa w oknie dachu") < first["text"].index("Budowa domu numer 0")
    numbers = [d for _, d in buttons(first["markup"]) if d.startswith("o:")]
    assert len(numbers) == 5

    bot.handle_update(click(MIETEK, numbers[0]))  # pierwsza wartość: karta, zapis, notatka, wynik
    nr = numbers[0].split(":")[1]
    bot.handle_update(click(MIETEK, f"s1:{nr}"))
    bot.handle_update(click(MIETEK, f"nt:{nr}"))
    bot.handle_update(message(MIETEK, "Kierownik: pan Adam, dzwonić po 15"))
    bot.handle_update(click(MIETEK, f"w:{nr}:r"))
    for extra in numbers[1:3]:
        bot.handle_update(click(MIETEK, extra))

    bot.handle_update(message(MIETEK, "/konto"))
    bot.handle_update(click(MIETEK, "zm:new"))
    assert len(store.orders()) == 1
    card = next(m for m in api.to(ADMIN) if m["text"].startswith("🛒 <b>Zamówienie"))
    bot.handle_update(click(ADMIN, dict(buttons(card["markup"]))["✅ Płatność otrzymana"]))

    person = store.get_user(MIETEK)
    assert person.rodzaj_dostepu == "platny" and bot._has_access(person)
    assert person.subscription_ends == "2026-11-05T05:00:00+00:00"  # koniec testu 06.10 + 30 dni
    assert any("Płatność za zamówienie" in t for t in texts(api, MIETEK))
    assert store.outcome(MIETEK, repo.get_by_nr(int(nr)).id_sprawy) == Outcome(wynik="rozmowa")
    assert "Jan &amp; &lt;Syn&gt;" in next(t for t in texts(api, MIETEK) if "Zamówienie" in t)

    clock.advance(days=15)
    bot.handle_update(message(ADMIN, "/start"))
    bot.handle_update(message(ADMIN, "/raport 30"))
    report = api.last_to(ADMIN)["text"]
    assert "strona 1" in report and "💳 płatności potwierdzone: 1 osoba" in report
    assert "aktywacja 1/1 · zamówienie 1/1 · płatność 1/1" in report

    assert_telegram_safe(texts(api, MIETEK) + texts(api, ADMIN))


def test_person_who_does_not_buy(bot, api, repo, clock):
    store = BotStore(repo)
    new_person_until_trial(bot, api, OBCY, trade="materialy", area_text="Dywity")
    first = api.last_to(OBCY)
    saved_nr = [d for _, d in buttons(first["markup"]) if d.startswith("o:")][0].split(":")[1]
    bot.handle_update(click(OBCY, f"s1:{saved_nr}"))
    other_nr = repo.get("J/6").nr

    at(clock, 9, 30, 9)
    bot.run_due_jobs()
    for day in range(1, 9):
        at(clock, 10, day, 9)
        bot.run_due_jobs()
        at(clock, 10, day, 20)
        bot.run_due_jobs()

    sent = texts(api, OBCY)
    assert sum(t.startswith("💡 <b>Jak idzie test?</b>") for t in sent) == 1
    assert sum("kończy się" in t for t in sent) == 1
    assert sum(t.startswith("⛔ Twój darmowy test skończył się") for t in sent) == 1
    reports_after_end = [m for m in api.to(OBCY)
                         if m["text"].startswith("📊 <b>Raport") and m["message_id"] > max(
                             x["message_id"] for x in api.to(OBCY) if x["text"].startswith("⛔"))]
    assert reports_after_end == []

    api.answers.clear()
    bot.handle_update(click(OBCY, f"o:{saved_nr}"))
    assert api.last_to(OBCY)["text"].startswith("🗄️ <b>Twoja zapisana praca</b>")
    bot.handle_update(click(OBCY, f"o:{other_nr}"))
    assert api.answers[-1].startswith("⛔")
    assert store.orders(chat_id=OBCY) == [] and not bot._has_access(store.get_user(OBCY))

    assert_telegram_safe(sent)


def test_people_never_see_each_others_work(bot, api, repo):
    for chat_id, area in ((MIETEK, "Dywity"), (OBCY, "Dywity")):
        new_person_until_trial(bot, api, chat_id, trade="materialy", area_text=area)
    nr = repo.get("J/0").nr
    store = BotStore(repo)
    bot.handle_update(click(MIETEK, f"nt:{nr}"))
    bot.handle_update(message(MIETEK, "tajne ustalenia Mietka"))
    bot.handle_update(click(MIETEK, f"w:{nr}:o"))
    bot.handle_update(click(MIETEK, f"s1:{nr}"))

    bot.handle_update(click(OBCY, f"o:{nr}"))
    card = api.last_to(OBCY)
    bot.handle_update(message(OBCY, "⭐ Zapisane"))

    assert "tajne" not in card["text"] and "📋 Wynik" in [t for t, _ in buttons(card["markup"])]
    assert "Nie masz jeszcze zapisanych" in api.last_to(OBCY)["text"]
    assert store.outcome(OBCY, "J/0") == Outcome() and store.note(OBCY, "J/0") is None
