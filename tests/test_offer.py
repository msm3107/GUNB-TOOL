"""Oferta (P0): jedna, walidowana konfiguracja – niepełna nie udaje zamówienia ani płatności."""

from decimal import Decimal

import pytest

from gunb_tool.config import ConfigError, OfferConfig, load_config
from tests.test_config import write_config

FULL = dict(price="99", tax="netto_vat", vat_rate="23", payment="przelew – dane w potwierdzeniu zamówienia",
            seller_name="Jan Przykładowy", seller_contact="@jan_przyklad")


def offer_yaml(**values) -> str:
    lines = "".join(f"    {key}: \"{value}\"\n" for key, value in values.items())
    return "gunb:\n  voivodeships: ['28']\nbot:\n  offer:\n" + lines


def test_offer_is_incomplete_by_default_and_says_what_is_missing(tmp_path):
    offer = load_config(write_config(tmp_path, "gunb:\n  voivodeships: ['28']\n"), env={}).bot.offer
    assert not offer.complete
    assert offer.missing() == ["cena", "sposób podatku", "sposób płatności", "sprzedawca", "kontakt sprzedawcy"]
    assert offer.period_days == 30 and offer.accounts == 1 and offer.currency == "PLN"


def test_complete_offer_from_the_config_file(tmp_path):
    offer = load_config(write_config(tmp_path, offer_yaml(**FULL, period_days="30")), env={}).bot.offer
    assert offer.complete and offer.missing() == []
    assert offer.price == Decimal("99")
    assert offer.amount_due == Decimal("121.77")
    assert offer.price_line() == "99 zł netto + 23% VAT = 121,77 zł do zapłaty"


@pytest.mark.parametrize("tax, line", [
    ("brutto", "99 zł brutto (kwota do zapłaty)"),
    ("bez_vat", "99 zł – kwota do zapłaty (sprzedawca nie dolicza VAT)"),
])
def test_tax_presentation_is_chosen_by_the_operator_not_assumed(tax, line):
    offer = OfferConfig(**{**FULL, "price": Decimal("99"), "tax": tax, "vat_rate": 23})
    assert offer.price_line() == line and offer.amount_due == Decimal("99")


def test_values_come_from_env_so_seller_data_stays_out_of_the_public_repository(tmp_path):
    path = write_config(tmp_path, "gunb:\n  voivodeships: ['28']\nbot:\n  offer:\n"
                                  "    price: ${OFERTA_CENA:-}\n    seller_name: ${OFERTA_SPRZEDAWCA:-}\n")
    offer = load_config(path, env={"OFERTA_CENA": "99,50", "OFERTA_SPRZEDAWCA": "Jan"}).bot.offer
    assert offer.price == Decimal("99.50") and offer.seller_name == "Jan"


@pytest.mark.parametrize("key, value, message", [
    ("price", "dużo", "bot.offer.price"),
    ("price", "-5", "bot.offer.price"),
    ("tax", "vat", "bot.offer.tax"),
    ("vat_rate", "230", "bot.offer.vat_rate"),
    ("period_days", "0", "bot.offer.period_days"),
    ("accounts", "0", "bot.offer.accounts"),
    ("currency", "złotówki", "bot.offer.currency"),
    ("terms_url", "http://niebezpieczny.example", "bot.offer.terms_url"),
])
def test_invalid_offer_values_are_rejected_at_start(tmp_path, key, value, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, offer_yaml(**{key: value})), env={})


def test_repository_config_keeps_the_offer_in_env_placeholders():
    """Publiczne repozytorium: cena i dane sprzedawcy tylko z .env – w pliku żadnych wymyślonych wartości."""
    from tests.test_config import REPO_ROOT

    offer = load_config(REPO_ROOT / "config.yaml", env={}).bot.offer
    assert not offer.complete and offer.price is None and offer.seller_name == ""
    assert offer.area == "Olsztyn i powiat olsztyński"
