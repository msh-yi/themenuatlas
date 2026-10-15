"""Row counts for the Actions step summary. Counts only, never menu text."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from menudb.db.models import (
    Component, Course, Ingredient, IngredientAlias, Menu, Restaurant, ReviewItem, Snapshot, Source,
)


def row_counts(session: Session) -> dict[str, int]:
    def n(model, *where):
        return session.scalar(select(func.count()).select_from(model).where(*where))

    return {
        "restaurants": n(Restaurant),
        "restaurants_with_menus": session.scalar(
            select(func.count(func.distinct(Menu.restaurant_id))).where(Menu.excluded_reason.is_(None))),
        "sources_check": n(Source, Source.mode == "check"),
        "sources_bucket": n(Source, Source.mode == "bucket"),
        "snapshots": n(Snapshot),
        "menus": n(Menu),
        "menus_extracted": n(Menu, Menu.extraction_version.is_not(None)),
        "menus_not_a_menu": n(Menu, Menu.menu_type == "not_a_menu"),
        "menus_excluded": n(Menu, Menu.excluded_reason.is_not(None)),
        "courses": n(Course),
        "components": n(Component),
        "components_unmatched": n(Component, Component.ingredient_id.is_(None)),
        "ingredients": n(Ingredient),
        "aliases": n(IngredientAlias),
        "review_open": n(ReviewItem, ReviewItem.status == "open"),
        "review_auto": n(ReviewItem, ReviewItem.status == "auto"),
    }
