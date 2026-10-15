import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from find_websites import official, pick  # noqa: E402


def test_official_accepts_own_domains():
    assert official("Alinea", "Chicago", "https://www.alinearestaurant.com/")
    assert official("Le Bernardin", "New York", "https://www.le-bernardin.com/")
    assert official("Sushi Saito", "Tokyo", "https://sushisaito.com/")
    assert official("Osteria Francescana", "Modena", "https://osteriafrancescana.it/")


def test_official_rejects_articles_directories_and_partial_matches():
    assert not official("Plaza Athénée", "Paris", "https://elitetraveler.com/")
    assert not official("Korai Kitchen", "Houston", "https://usarestaurants.info/")
    assert not official("Asperges", "Paris", "https://rolandlim.wordpress.com/")
    assert not official("CTC Urban Gastronomy", "Athens", "https://tinyurbankitchen.com/")
    assert not official("Tacos Wuey Detroit", "Detroit", "https://www.clickondetroit.com/")


def test_pick_skips_deep_pages():
    results = [{"url": "https://alinearestaurant.com/a/b/c", "title": ""},
               {"url": "https://www.alinearestaurant.com/menu", "title": ""}]
    assert pick("Alinea", "Chicago", results) == "https://www.alinearestaurant.com/"
