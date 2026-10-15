"""menudb review build / decide: lexicon edits from the review page (no database)."""

from menudb import review
from menudb.extract import auto
from menudb.lexicon_edit import LexiconFile

LEX = """# test lexicon

## vegetable
beetroot = en: beet | fr: betterave
carrot

## herb
dill = da: dild
"""


def lexicon(tmp_path):
    p = tmp_path / "ingredients.txt"
    p.write_text(LEX)
    return p


def key(term):
    return tuple(t for t, _, _ in auto.tokens(term))


def choice(**kw):
    return review.Choice.model_validate({"kind": "candidate", "language": "en", **kw})


def test_add_term_and_entry_keep_the_format(tmp_path):
    lex = LexiconFile.read(lexicon(tmp_path))
    assert lex.add_term("beetroot", "fr", "betteraves rouges")
    assert lex.add_term("beetroot", "de", "rote bete")
    assert lex.add_term("carrot", "fr", "carotte")
    assert not lex.add_term("beetroot", "en", "Beet")  # already there
    lex.add_entry("celtuce", "vegetable", "en", "celtuce")
    lex.add_entry("lovage", "herb", "fr", "livèche")
    lex.add_entry("nori", "seaweed", "ja", "海苔")
    lex.write()
    text = lex.path.read_text()
    assert "beetroot = en: beet | fr: betterave, betteraves rouges | de: rote bete" in text
    assert "carrot = fr: carotte" in text
    assert "carrot = fr: carotte\nceltuce\n\n## herb\ndill = da: dild\nlovage = fr: livèche\n" in text
    assert text.endswith("## seaweed\nnori = ja: 海苔\n")
    parsed = auto._parse(lex.path, prep=False)
    assert parsed.words[key("betteraves rouges")][0][1].name == "beetroot"
    assert parsed.cjk["海苔"][0][1].name == "nori"


def test_build_skips_known_terms_rejected_and_rare(tmp_path):
    queue = [
        {"raw_term": "Beet", "language": "en", "status": "auto", "current": "beetroot", "occurrences": 3},
        {"raw_term": "Celtuce", "language": "en", "status": "auto", "current": "celtuce", "occurrences": 2},
        {"raw_term": "Carrots", "language": "en", "status": "mapped", "current": "x", "occurrences": 9},
    ]
    cands = [
        {"phrase": "betterave", "language": "fr", "restaurants": 5},  # in the lexicon
        {"phrase": "livèche", "language": "fr", "restaurants": 4},
        {"phrase": "carrots", "language": "en", "restaurants": 3},  # plural of a known term
        {"phrase": "fine dining", "language": "en", "restaurants": 9},  # rejected before
        {"phrase": "Ari Kolender Cooks", "language": "en", "restaurants": 3},
        {"phrase": "dills", "language": "fr", "restaurants": 1},  # too rare
    ]
    items = review.build_items(queue, cands, lexicon(tmp_path),
                               rejected=[{"phrase": "Fine dining", "language": "en"}])
    assert [(i["kind"], i["term"]) for i in items] == [
        ("ingredient", "Celtuce"), ("candidate", "livèche"), ("candidate", "Ari Kolender Cooks")]
    assert items[0]["suggest"] == {"action": "approve", "canonical": "celtuce"}
    assert items[2]["suggest"] == {"action": "reject"}


def test_decide_edits_lexicon_and_returns_alias_decisions(tmp_path):
    path = lexicon(tmp_path)
    counts, decisions, rejected = review.decide([
        choice(term="livèche", language="fr", action="approve", canonical="lovage", category="herb"),
        choice(term="rote Bete", language="de", action="merge", canonical="beetroot"),
        choice(term="Celtuce", kind="ingredient", action="approve", category="vegetable"),
        choice(term="Hiramasa", kind="ingredient", action="merge", canonical="yellowtail kingfish",
               category="fish"),
        choice(term="Bay leaf jus", kind="ingredient", action="reject"),
        choice(term="fine dining", action="reject"),
        choice(term="dild", language="da", action="merge", canonical="beetroot"),  # means dill
        choice(term="a | b", action="approve"),
        choice(term="Mu", kind="ingredient", action="merge", canonical="beetroot"),  # too short for the lexicon
    ], path)
    assert counts["approved"] == 2 and counts["merged"] == 4 and counts["rejected"] == 2
    assert counts["lexicon_conflict"] == 1 and counts["skipped_bad_name"] == 1
    assert counts["lexicon_too_short"] == 1
    text = path.read_text()
    assert "lovage = fr: livèche" in text and "de: rote bete" in text
    assert "celtuce = en: Celtuce" not in text and "\nceltuce\n" in text
    assert "## fish\nyellowtail kingfish = en: hiramasa" in text
    assert "mu" not in text.split()
    assert [(d.raw_term, d.action, d.canonical) for d in decisions] == [
        ("Celtuce", "map", "celtuce"), ("Hiramasa", "map", "yellowtail kingfish"),
        ("Bay leaf jus", "ignore", None), ("Mu", "map", "beetroot")]
    assert rejected == [{"phrase": "fine dining", "language": "en"}]
    # the extractor now finds the new terms
    ing = auto._parse(path, prep=False)
    assert ing.words[key("livèche")][0][1].name == "lovage"
