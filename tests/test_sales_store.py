"""Migracja v12 i operacje w bazie: zamówienia z jednym otwartym na osobę i jednorazowym potwierdzeniem,
wynik pracy na parę osoba–inwestycja, źródło wejścia, prośba o test, przedłużenie testu z powodem."""

import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from gunb_tool import storage
from gunb_tool.bot_store import BotStore, Outcome
from gunb_tool.config import OfferConfig
from gunb_tool.storage import LeadRepository
from tests.bot_helpers import MIETEK, OBCY, lead
from tests.test_storage import legacy_database

NOW = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
OFFER = OfferConfig(price=Decimal("99"), tax="netto_vat", vat_rate=23, payment="przelew", seller_name="Jan",
                    seller_contact="@jan", period_days=30, name="Żółta Tablica")


@pytest.fixture
def store(repo):
    store = BotStore(repo)
    for chat_id in (MIETEK, OBCY):
        store.register(chat_id, "Ktoś", None, status="aktywny", backlog_days=7)
    repo.upsert(lead("A/1"))
    repo.upsert(lead("A/2"))
    return store


# --- Migracja ---------------------------------------------------------------------------------------------

def test_version_11_database_gets_the_sales_tables_and_keeps_its_data(tmp_path, clock):
    path = tmp_path / "v11.sqlite"
    legacy = legacy_database(path, 11)
    legacy.execute("INSERT INTO bot_users (chat_id, status, nowe_od, utworzono, zmieniono, filtry, konfiguracja)"
                   " VALUES (?, 'aktywny', 'x', 'x', 'x', '{\"miejsca\": [\"Dywity\"]}', 'gotowe')", (MIETEK,))
    legacy.execute("INSERT INTO zdarzenia (chat_id, rodzaj, kiedy) VALUES (?, 'test_start', 'x')", (MIETEK,))
    legacy.commit()
    legacy.close()

    with LeadRepository(path, now=clock.now_utc) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        user = BotStore(repo).get_user(MIETEK)
        assert user.filtry.miejsca == ("Dywity",) and user.setup_done
        assert user.zrodlo is None and user.firma is None and user.tips_enabled and user.porady == ()
        assert repo.connection.execute("SELECT szczegoly FROM zdarzenia").fetchone()[0] is None
    assert list((tmp_path / "backups").glob(f"v11-przed-v{storage.SCHEMA_VERSION}-*.sqlite")), "kopia przed migracją"


def test_deleting_a_person_removes_their_orders_and_outcomes(store, repo):
    store.set_outcome(MIETEK, "A/1", wynik="rozmowa")
    store.create_order(MIETEK, OFFER)
    repo.connection.execute("PRAGMA foreign_keys = ON")
    repo.connection.execute("DELETE FROM bot_users WHERE chat_id = ?", (MIETEK,))
    assert repo.connection.execute("SELECT COUNT(*) FROM wyniki").fetchone()[0] == 0
    assert repo.connection.execute("SELECT COUNT(*) FROM zamowienia").fetchone()[0] == 0


# --- Zamówienia ---------------------------------------------------------------------------------------------

def test_order_keeps_a_snapshot_of_the_offer(store):
    order, created = store.create_order(MIETEK, OFFER)
    assert created and order.number == f"Z-{order.id}" and order.stan == "zgloszone"
    assert (order.cena, order.do_zaplaty, order.dni) == ("99", "121.77", 30)
    assert order.opis_ceny == "99 zł netto + 23% VAT = 121,77 zł do zapłaty"


def test_only_one_open_order_per_person(store):
    first, created = store.create_order(MIETEK, OFFER)
    again, created_again = store.create_order(MIETEK, OFFER)
    assert created and not created_again and again.id == first.id
    with pytest.raises(sqlite3.IntegrityError):  # także przy wyścigu dwóch procesów
        store._conn.execute("INSERT INTO zamowienia (chat_id, oferta, cena, waluta, podatek, do_zaplaty, opis_ceny,"
                            " dni, utworzono, zmieniono) VALUES (?, 'x', '1', 'PLN', 'brutto', '1', 'x', 30, 'x', 'x')",
                            (MIETEK,))
    other, _ = store.create_order(OBCY, OFFER)
    assert other.id != first.id


def test_payment_is_confirmed_exactly_once(store):
    order, _ = store.create_order(MIETEK, OFFER)
    confirmed = store.confirm_order(order.id, admin=1001, note="przelew 02.10")
    assert confirmed is not None and confirmed.stan == "oplacone" and confirmed.potwierdzil == 1001
    assert confirmed.uwagi == "przelew 02.10" and confirmed.oplacono is not None
    assert store.confirm_order(order.id, admin=1001) is None  # drugie kliknięcie niczego nie zmienia
    assert store.open_order(MIETEK) is None
    new_order, created = store.create_order(MIETEK, OFFER)  # odnowienie – nowe zamówienie
    assert created and new_order.id != order.id


def test_cancelled_order_cannot_be_paid(store):
    order, _ = store.create_order(MIETEK, OFFER)
    assert store.cancel_order(order.id) and not store.cancel_order(order.id)
    assert store.confirm_order(order.id, admin=1001) is None
    assert store.get_order(order.id).stan == "anulowane"


# --- Wynik pracy ----------------------------------------------------------------------------------------------

def test_outcome_belongs_to_the_pair_and_repeated_clicks_change_nothing(store, repo):
    assert store.set_outcome(MIETEK, "A/1", wynik="rozmowa")
    assert not store.set_outcome(MIETEK, "A/1", wynik="rozmowa")  # ponowione kliknięcie
    assert store.set_outcome(MIETEK, "A/1", ocena=1)
    assert not store.set_outcome(MIETEK, "A/1", ocena=1)
    assert store.outcome(MIETEK, "A/1") == Outcome(wynik="rozmowa", ocena=1)
    assert store.outcome(OBCY, "A/1") == Outcome()  # druga osoba nic nie widzi
    assert repo.connection.execute("SELECT COUNT(*) FROM wyniki").fetchone()[0] == 1


def test_reason_is_kept_only_for_a_not_matching_investment(store):
    store.set_outcome(MIETEK, "A/1", wynik="niepasujaca", powod="obszar")
    assert store.outcome(MIETEK, "A/1").powod == "obszar"
    store.set_outcome(MIETEK, "A/1", wynik="sprawdzona")
    assert store.outcome(MIETEK, "A/1") == Outcome(wynik="sprawdzona")
    with pytest.raises(ValueError):
        store.set_outcome(MIETEK, "A/1", wynik="kupione")


def test_own_work_covers_saved_noted_rated_and_reminded_investments(store):
    assert not store.owns_work(MIETEK, "A/1")
    store.set_note(MIETEK, "A/1", "zadzwonić w poniedziałek")
    store.set_outcome(MIETEK, "A/2", wynik="oferta")
    assert store.owns_work(MIETEK, "A/1") and store.owns_work(MIETEK, "A/2")
    assert not store.owns_work(OBCY, "A/1")


# --- Wejście, test, podpowiedzi ---------------------------------------------------------------------------------

def test_source_is_saved_once_at_registration(repo):
    store = BotStore(repo)
    store.register(MIETEK, "Mietek", None, status="aktywny", backlog_days=7, zrodlo="ulotka")
    store.register(MIETEK, "Mietek", None, status="aktywny", backlog_days=7, zrodlo="strona")
    assert store.get_user(MIETEK).zrodlo == "ulotka"


def test_trial_request_is_recorded_once(store):
    assert store.request_trial(MIETEK) and not store.request_trial(MIETEK)
    assert store.get_user(MIETEK).prosba_o_test is not None


def test_trial_can_be_extended_once_with_a_reason(store, clock):
    start = clock.now_utc()
    store.allow_trial(MIETEK)
    store.start_trial(MIETEK, start, start + timedelta(days=7))
    assert store.extend_trial(MIETEK, start + timedelta(days=10), "urlop klienta")
    assert not store.extend_trial(MIETEK, start + timedelta(days=12), "jeszcze raz")
    user = store.get_user(MIETEK)
    assert user.subscription_ends == user.test_koniec == (start + timedelta(days=10)).isoformat(timespec="seconds")
    assert user.test_przedluzenie_powod == "urlop klienta"
    assert not store.extend_trial(OBCY, start + timedelta(days=10), "bez testu")  # tylko trwający/odbyty test


def test_tips_are_remembered_per_person(store):
    store.mark_tip(MIETEK, "otworz")
    store.mark_tip(MIETEK, "otworz")
    assert store.get_user(MIETEK).porady == ("otworz",)
    store.set_tips_enabled(MIETEK, False)
    assert not store.get_user(MIETEK).tips_enabled and store.get_user(OBCY).tips_enabled
