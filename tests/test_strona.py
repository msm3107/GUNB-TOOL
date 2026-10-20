"""Strona sprzedażowa – lekka, dostępna, bez atrap i wymyślonych danych."""

import re
from html.parser import HTMLParser
from pathlib import Path
import pytest

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
    assert not page.all("script") and not page.all("form") and not page.all("input")
    assert all(img.get("alt") for img in page.all("img"))
    styles = [link["href"] for link in page.all("link") if link.get("rel") == "stylesheet"]
    assert styles == ["styl.css"]
    assert (SITE / "styl.css").stat().st_size < 12_000


def test_landing_page_has_every_required_section_and_no_invented_data():
    text = " ".join(parse("index.html").text)
    for required in ("Dla kogo", "województwo warmińsko-mazurskie", "PRZYKŁAD – dane fikcyjne", "Jak to działa",
                     "Co dostajesz", "Uczciwie o danych", "Oferta", "Pytania", "Kto za tym stoi",
                     "nie gotowe zamówienia", "Nie gwarantujemy zleceń",
                     "kontakt pokazujemy tylko, gdy ktoś wpisał go w rejestr"):
        assert required in text, required
    assert text.count("DO UZUPEŁNIENIA") >= 3
    lowered = text.lower()
    for invented in ("opinie klientów", "zadowolonych klientów", "cała polska", "telefony do inwestorów:",
                     "pierwszeństwo gwarantowane"):
        assert invented not in lowered, invented
    assert re.findall(r"(\S+) gwarant", lowered) == ["nie"]


@pytest.mark.parametrize("name", ("zasady.html", "prywatnosc.html"))
def test_rules_and_privacy_are_marked_as_drafts(name):
    text = " ".join(parse(name).text)
    assert "SZKIC" in text and "nie jest dokumentem" in text and "DO UZUPEŁNIENIA" in text
