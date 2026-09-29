"""Telefony i e-maile z surowych pól inwestora/projektanta – dane testowe są fikcyjne."""

import pytest

from gunb_tool.contacts import extract_contact, find_email, find_phone


@pytest.mark.parametrize(
    "text, phone",
    [
        ("tel. 600 123 456", "+48600123456"),
        ("Tel.: 600-123-456", "+48600123456"),
        ("kom. 600123456", "+48600123456"),
        ("+48 600 123 456", "+48600123456"),
        ("0048 600 123 456", "+48600123456"),
        ("Pracownia Test, telefon (89) 527 00 00", "+48895270000"),
        ("89 527 00 00", "+48895270000"),
        ("733-110-133", "+48733110133"),  # telefon wpisany w pole numeru uprawnień
    ],
)
def test_polish_phone_numbers_are_found_and_normalized(text, phone):
    assert find_phone(text) == phone


@pytest.mark.parametrize(
    "text",
    [
        "516042210",  # 9 cyfr bez „tel.” – równie dobrze REGON albo numer uprawnień
        "REGON 123456789",
        "NIP 123-456-78-90",
        "upr. bud. 123/94/Op",
        "WAM/0123/POOK/05",
        "UID: EPUAP_1829_187936833",
        "10-123 Olsztyn, ul. Długa 5",
        "decyzja z 2026-09-25",
        "tel. 012 345 678",  # numery w Polsce nie zaczynają się od 0
        "tel. 123 456 789",  # typowa wartość zastępcza
        "Budowa stacji bazowej telefonii komórkowej",
        "Firma Test Sp. z o.o. sp. kom.",
        "Remont obiektu mostowego O JNI 010113558",
        None,
        "",
    ],
)
def test_numbers_that_are_not_phones_are_ignored(text):
    assert find_phone(text) is None


def test_email_is_found_and_lowercased():
    assert find_email("e-mail: Biuro@Pracownia-Test.PL, tel. 600 123 456") == "biuro@pracownia-test.pl"
    assert find_email("kontakt: jan.test+gunb@poczta.test.") == "jan.test+gunb@poczta.test"
    assert find_email("Budowa domu") is None
    assert find_email(None) is None


def test_contact_comes_from_the_first_field_that_has_it():
    contact = extract_contact("Firma Testowa Sp. z o.o.", "JAN TESTOWY tel. 600 123 456", "biuro@test.pl")
    assert contact == ("+48600123456", "biuro@test.pl")
    assert extract_contact(None, "", "Budowa domu") == (None, None)
