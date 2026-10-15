"""Hemisphere-relative seasons, so October in Sydney compares with April in Paris."""

from datetime import date

TROPIC_LAT = 23.44

_NORTH = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
          6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}


def aligned_month(lat: float, d: date) -> int:
    """Month on a Northern-Hemisphere calendar: Southern dates shift by six months."""
    if lat >= 0:
        return d.month
    return (d.month + 5) % 12 + 1


def season(lat: float, d: date) -> str:
    """Meteorological season. Inside the tropics returns 'tropical' instead:
    temperate seasons are a poor model there (wet/dry cycles are regional)."""
    if abs(lat) < TROPIC_LAT:
        return "tropical"
    return _NORTH[aligned_month(lat, d)]
