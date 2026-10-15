"""Match extracted terms to canonical ingredients.

A term matches an alias (raw term, in the menu's language or "und"), or a canonical
name equal to the extractor's suggested English name. With no match, a suggested
name becomes a new ingredient (auto-accepted) so the analysis can use it at once;
every such term also goes to review_queue, status "auto", for a human to audit.
Terms with neither a match nor a suggestion wait in review_queue as "open".
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from menudb.db.models import Ingredient, IngredientAlias, ReviewItem


def _norm(term: str) -> str:
    return " ".join(term.lower().split())[:160]


def match(session: Session, raw_term: str, suggested: str | None, language: str | None,
          context: str | None = None, auto_accept: bool = True) -> int | None:
    lang = language or "und"
    alias = _norm(raw_term)
    found = session.scalar(
        select(IngredientAlias.ingredient_id)
        .where(IngredientAlias.alias == alias, IngredientAlias.language.in_([lang, "und"]))
        .limit(1)
    )
    if found:
        return found

    name = _norm(suggested)[:120] if suggested else None
    ingredient = None
    if name:
        ingredient = session.scalar(select(Ingredient).where(Ingredient.canonical_name == name))
        if ingredient is None and auto_accept:
            ingredient = Ingredient(canonical_name=name)
            session.add(ingredient)
            session.flush()
            _review(session, raw_term, lang, name, context, "auto")
    if ingredient is None:
        _review(session, raw_term, lang, name, context, "open")
        return None

    if alias != name:
        session.add(IngredientAlias(alias=alias, language=lang, ingredient_id=ingredient.id))
        session.flush()
    return ingredient.id


def _review(session, raw_term, lang, suggested, context, status):
    term = raw_term[:200]
    item = session.scalar(
        select(ReviewItem).where(ReviewItem.raw_term == term, ReviewItem.language == lang))
    if item:
        item.occurrences += 1
        return
    session.add(ReviewItem(raw_term=term, language=lang, suggested_name=suggested,
                           example_context=context, occurrences=1, status=status))
    session.flush()
