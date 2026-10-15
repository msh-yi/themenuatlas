"""Rule-based extractor on invented menus (no real menu text in the repo)."""

import json

from menudb import handoff
from menudb.extract import auto
from menudb.extract.schema import ExtractedMenu, PendingItem
from test_handoff import bucket  # noqa: F401  (fixture)

TASTING_EN = """Book a table
Autumn Tasting Menu
Smoked eel, beetroot and horseradish,
apple and dill
Hand-dived scallop, pumpkin purée
Roast pigeon
cep, blackberry and red wine sauce
DESSERT
Dark chocolate, hazelnut ice cream
£180 per person
Wine pairing £120
"""

DIVIDED_FR = """Menu dégustation
✳
Betteraves fumées & estragon
Crème de raifort
✳
Homard bleu rôti, beurre noisette
Girolles et échalotes
✳
Pigeonneau, cèpes grillés
Jus au thym
✳
Poire pochée, glace vanille
✳
220 €
"""


def item(text: str, slug: str = "r") -> PendingItem:
    return PendingItem(slug=slug, normalized_hash="h" * 64, kind="html", url="https://r.example/",
                       storage_key="raw/x", text=text)


def names(course) -> set[str]:
    return {c.name for c in course.components if c.name}


def test_english_tasting_menu():
    ex = auto.extract(item(TASTING_EN), country="GB")
    assert ex.is_menu and ex.extractor == auto.EXTRACTOR
    assert (ex.language, ex.menu_type, ex.price, ex.currency) == ("en", "tasting", 180.0, "GBP")
    assert len(ex.courses) == 4  # continuation lines join their dish; the pairing is ignored
    eel, scallop, pigeon, dessert = ex.courses
    assert {"eel", "beetroot", "horseradish", "apple", "dill"} <= names(eel)
    smoked = next(c for c in eel.components if c.name == "eel")
    assert smoked.preparations == ["smoked"] and smoked.role == "main"
    assert {"scallop", "pumpkin"} <= names(scallop)
    assert {"pigeon", "porcini", "blackberry", "wine"} <= names(pigeon)
    assert dessert.kind == "dessert" and {"chocolate", "hazelnut"} <= names(dessert)


def test_french_menu_with_dividers_keeps_each_block_as_one_dish():
    ex = auto.extract(item(DIVIDED_FR))
    assert ex.language == "fr" and ex.currency == "EUR" and ex.price == 220.0
    assert ex.menu_type == "tasting"
    assert len(ex.courses) == 4
    assert {"beetroot", "tarragon", "horseradish", "cream"} <= names(ex.courses[0])
    lobster = next(c for c in ex.courses[1].components if c.name == "lobster")
    assert lobster.preparations == ["roasted"]
    assert {"chanterelle", "shallot", "butter"} <= names(ex.courses[1])
    assert "porcini" in names(ex.courses[2])


def test_plurals_compounds_and_unmatched_phrases():
    ing, _ = auto.load()
    found = lambda s, langs: [h.entry.name for h in auto.find(ing, s, langs)]  # noqa: E731
    assert found("Heritage tomatoes, radishes and cherries", ["en"]) == ["tomato", "radish", "cherry"]
    assert found("Steinpilzrisotto mit Kalbsbries", ["de", "en"]) == ["porcini", "risotto", "sweetbread"]
    ex = auto.extract(item("Turbot, Zorblax foam\nLamb, onion\nDuck, plum\nPork, apple\nHake, leek\n"))
    unnamed = [c.raw_term for c in ex.courses[0].components if not c.name]
    assert unnamed == ["Zorblax"]  # goes to the review queue on import


def test_not_a_menu():
    landing = "Welcome to our restaurant\nReservations\nWe cook with local lamb and seasonal vegetables.\n"
    assert not auto.extract(item(landing)).is_menu
    harvest = "\n".join(["Carrot", "Beetroot", "Kale", "Leek", "Radish", "Turnip"])
    assert not auto.extract(item(harvest)).is_menu
    assert not auto.extract(item("")).is_menu


def test_extract_auto_writes_extracted_and_keeps_session_work(bucket):  # noqa: F811
    a, b = item(TASTING_EN, "a"), item(DIVIDED_FR, "b")
    for it in (a, b):
        handoff.put_json(handoff.key(handoff.PENDING, it.slug, it.normalized_hash), it)
    counts = handoff.extract_auto(slugs={"a"}, countries={"a": "GB"})
    assert counts["extracted"] == 1 and counts["menus"] == 1
    k = handoff.key(handoff.EXTRACTED, "a", a.normalized_hash)
    assert ExtractedMenu.model_validate_json(bucket[k]).extractor == auto.EXTRACTOR
    assert handoff.key(handoff.PROCESSED, "a", a.normalized_hash) in bucket
    assert handoff.key(handoff.PENDING, "a", a.normalized_hash) not in bucket
    assert handoff.key(handoff.PENDING, "b", b.normalized_hash) in bucket  # filtered out

    # a session later replaces it; a rerun must not overwrite the session's work
    session = json.loads(bucket[k]) | {"extractor": "claude-code-session"}
    bucket[k] = json.dumps(session).encode()
    counts = handoff.extract_auto(slugs={"a"}, rerun=True)
    assert counts["kept_session_extraction"] == 1
    assert json.loads(bucket[k])["extractor"] == "claude-code-session"

    # processed items are not queued again
    assert handoff.queue_items([a])["already_queued"] == 1
