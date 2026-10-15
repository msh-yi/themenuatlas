"""Seed the real CSV and recipes into a local Postgres."""

import csv
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from menudb.db import make_engine
from menudb.db.models import ListMembership, Restaurant, Source
from menudb.seed import country_code, seed

ROOT = Path(__file__).parents[1]


def test_country_code():
    assert country_code("United Kingdom") == "GB"
    assert country_code("JP") == "JP"
    with pytest.raises(ValueError):
        country_code("Atlantis")


@pytest.mark.db
def test_seed_repo_data_twice(pg_url):
    engine = make_engine(pg_url)
    with sessionmaker(engine)() as s:
        try:
            for _ in range(2):  # idempotent
                counts = seed(s, ROOT / "data/seed/restaurants.csv", ROOT / "data/recipes")
                s.commit()
            n_csv = sum(1 for _ in (ROOT / "data/seed/restaurants.csv").open()) - 1
            assert counts["restaurants"] == n_csv
            assert s.scalar(select(func.count()).select_from(Restaurant)) == n_csv
            assert s.scalar(select(func.count()).select_from(Source)) == counts["sources"] > 0
            r = s.scalar(select(Restaurant).where(Restaurant.slug == "geranium"))
            assert r.country == "DK" and r.publishes_menu == "yes"
            assert s.scalar(select(func.count()).select_from(ListMembership)) > 0
        finally:
            s.rollback()
            for m in (Source, ListMembership, Restaurant):
                s.query(m).delete()
            s.commit()
    engine.dispose()


@pytest.mark.db
def test_seed_full_list_then_curated(pg_url):
    """`menudb seed` loads the full list (restaurants only), then the curated list with recipes."""
    engine = make_engine(pg_url)
    with sessionmaker(engine)() as s:
        try:
            full = seed(s, ROOT / "data/seed/restaurants_full.csv", None)
            curated = seed(s, ROOT / "data/seed/restaurants.csv", ROOT / "data/recipes")
            s.commit()
            assert full.get("sources", 0) == 0 and curated["sources"] > 0
            slugs = {r["slug"] for r in csv.DictReader((ROOT / "data/seed/restaurants_full.csv").open())}
            slugs |= {r["slug"] for r in csv.DictReader((ROOT / "data/seed/restaurants.csv").open())}
            assert s.scalar(select(func.count()).select_from(Restaurant)) == len(slugs)
        finally:
            s.rollback()
            for m in (Source, ListMembership, Restaurant):
                s.query(m).delete()
            s.commit()
    engine.dispose()
