from datetime import date

from menudb.seasons import aligned_month, season

PARIS, SYDNEY, SINGAPORE = 48.86, -33.87, 1.35


def test_northern():
    assert season(PARIS, date(2026, 10, 14)) == "autumn"
    assert season(PARIS, date(2026, 1, 5)) == "winter"


def test_southern_is_flipped():
    assert season(SYDNEY, date(2026, 10, 14)) == "spring"
    assert season(SYDNEY, date(2026, 1, 5)) == "summer"
    assert aligned_month(SYDNEY, date(2026, 10, 1)) == 4  # Sydney October ~ Paris April
    assert aligned_month(SYDNEY, date(2026, 6, 1)) == 12


def test_tropics():
    assert season(SINGAPORE, date(2026, 7, 1)) == "tropical"
