from menudb.normalize import canonical, html_to_text, normalized_hash


def test_boilerplate_removed(fixture_text):
    text = html_to_text(fixture_text("menu_v1.html"))
    assert "Langoustine, sea buckthorn, brown butter" in text
    assert "cookies" not in text.lower()
    assert "Date.now" not in text
    assert "Home" not in text  # nav
    assert "rights reserved" not in text  # footer


def test_volatile_noise_does_not_change_hash(fixture_text):
    a = normalized_hash(html_to_text(fixture_text("menu_v1.html")))
    b = normalized_hash(html_to_text(fixture_text("menu_v1_noise.html")))
    assert a == b


def test_dish_change_changes_hash(fixture_text):
    a = normalized_hash(html_to_text(fixture_text("menu_v1.html")))
    b = normalized_hash(html_to_text(fixture_text("menu_v2.html")))
    assert a != b


def test_canonical_strips_dates_prices_years():
    assert canonical("Menu 12/03/2026 — €185") == canonical("Menu 01.04.2027 — € 195")
    assert canonical("Served 7:30 pm, March 3rd") == canonical("Served 8:00 pm, April 9th")
    assert canonical("Turbot, beurre blanc") != canonical("Brill, beurre blanc")
