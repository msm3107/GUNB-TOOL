"""P1: strona sprzedażowa i teksty profilu – lekkie, dostępne, bez atrap i wymyślonych danych."""

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from gunb_tool.bot import campaign_source

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "strona"
PAGES = ("index.html", "zasady.html", "prywatnosc.html")


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict]] = []
        self.ids: set[str] = set()
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append((tag, attrs))
        if "id" in attrs:
            self.ids.add(attrs["id"])

    def handle_data(self, data):
        self.text.append(data)

    def all(self, tag):
        return [attrs for name, attrs in self.tags if name == tag]


def parse(name: str) -> Page:
    page = Page()
    page.feed((SITE / name).read_text(encoding="utf-8"))
    return page


@pytest.mark.parametrize("name", PAGES)
def test_pages_are_light_accessible_and_have_basic_seo(name):
    page = parse(name)
    html = page.tags[0]
    assert html == ("html", {"lang": "pl"})
    assert {"name": "viewport", "content": "width=device-width, initial-scale=1"} in page.all("meta")
    description = next(m["content"] for m in page.all("meta") if m.get("name") == "description")
    assert 50 <= len(description) <= 170
    assert len(page.all("h1")) == 1 and page.all("main")
    assert not page.all("script") and not page.all("form") and not page.all("input")  # bez atrap i formularzy
    assert all(img.get("alt") for img in page.all("img"))
    styles = [link["href"] for link in page.all("link") if link.get("rel") == "stylesheet"]
    assert styles == ["styl.css"]  # bez zewnętrznych czcionek i bibliotek
    assert (SITE / "styl.css").stat().st_size < 12_000


@pytest.mark.parametrize("name", PAGES)
def test_every_link_works_locally_or_opens_the_bot_with_a_valid_source(name):
    page = parse(name)
    for link in page.all("a"):
        href = link["href"]
        if href.startswith("#"):
            assert href[1:] in page.ids, href
        elif href.startswith("https://"):
            url = urlparse(href)
            assert url.netloc == "t.me" and url.path == "/ZoltaTablicaBot", href
            source = parse_qs(url.query)["start"][0]
            assert campaign_source(source) == source, href  # kod przejdzie walidację bota
        else:
            assert (SITE / href).is_file(), href


def test_landing_page_has_every_required_section_and_no_invented_data():
    text = " ".join(parse("index.html").text)
    for required in ("Dla kogo", "województwo warmińsko-mazurskie", "PRZYKŁAD – dane fikcyjne", "Jak to działa",
                     "Co dostajesz", "Uczciwie o danych", "Oferta", "Pytania", "Kto za tym stoi",
                     "nie gotowe zamówienia", "Nie gwarantujemy zleceń",
                     "kontakt pokazujemy tylko, gdy ktoś wpisał go w rejestr"):  # jak karta w bocie
        assert required in text, required
    assert text.count("DO UZUPEŁNIENIA") >= 3  # cena, płatność, sprzedawca – do wpisania przez operatora
    lowered = text.lower()
    for invented in ("opinie klientów", "zadowolonych klientów", "cała polska", "telefony do inwestorów:",
                     "pierwszeństwo gwarantowane"):
        assert invented not in lowered, invented
    assert re.findall(r"(\S+) gwarant", lowered) == ["nie"]  # „gwarantujemy” tylko w zaprzeczeniu


@pytest.mark.parametrize("name", ("zasady.html", "prywatnosc.html"))
def test_rules_and_privacy_are_marked_as_drafts(name):
    text = " ".join(parse(name).text)
    assert "SZKIC" in text and "nie jest dokumentem" in text and "DO UZUPEŁNIENIA" in text


def test_privacy_draft_matches_what_the_bot_really_stores():
    text = " ".join(" ".join(parse("prywatnosc.html").text).split()).lower()
    for fact in ("id czatu", "notatki", "źródło wejścia", "13 miesięcy", "nie przyjmuje płatności", "nie wysyła reklam",
                 "nazwa firmy", "kopie zapasowe bazy (na serwerze",
                 "tylko wtedy, gdy ktoś wpisał go w ten rejestr", "nie szuka danych kontaktowych w innych źródłach"):
        assert fact in text, fact
    # bot nie przekazuje zwykłych wiadomości operatorowi („🤔 Nie rozumiem”) – prośba o usunięcie idzie do operatora
    assert "przez bota" not in text


def section(markdown: str, name: str) -> str:
    return re.search(rf"<!-- {name} -->\n(.*?)\n<!-- /{name} -->", markdown, re.S).group(1)


def test_telegram_profile_texts_fit_the_limits_and_promise_nothing_false():
    doc = (ROOT / "docs" / "TELEGRAM_PROFIL.md").read_text(encoding="utf-8")
    name, about, description = (section(doc, key) for key in ("nazwa", "about", "description"))
    assert name == "Żółta Tablica"
    assert len(about) <= 120 and len(description) <= 512
    assert "nie zamówienia" in description and "nie kontakty do inwestorów" in description
    for link in re.findall(r"https://t\.me/ZoltaTablicaBot\?start=([\w-]+)", doc):
        assert campaign_source(link) == link
