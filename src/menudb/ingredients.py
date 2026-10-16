"""Match extracted terms to canonical ingredients.

A term matches an alias (raw term, in the menu's language or "und"), or a canonical
name equal to the extractor's suggested English name. With no match, a suggested
name becomes a new ingredient (auto-accepted) so the analysis can use it at once;
every such term also goes to review_queue, status "auto", for a human to audit.
Terms with neither a match nor a suggestion wait in review_queue as "open".
"""

import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from menudb.db.models import Ingredient, IngredientAlias, ReviewItem


def _norm(term: str) -> str:
    return " ".join(unicodedata.normalize("NFC", term).lower().split())[:160]


class Matcher:
    """Caches aliases, ingredients and review items for one import run, so matching a
    term costs no database round trip (Postgres is across the network in Actions).
    Build a new one after a rollback: the cache may hold rows that were undone."""

    def __init__(self, session: Session):
        self.session = session
        self.aliases = {(a, lang): i for a, lang, i in session.execute(
            select(IngredientAlias.alias, IngredientAlias.language, IngredientAlias.ingredient_id))}
        self.ingredients = dict(session.execute(select(Ingredient.canonical_name, Ingredient.id)).all())
        self.reviews = {(r.raw_term, r.language): r for r in session.scalars(select(ReviewItem))}

    def match(self, raw_term: str, suggested: str | None, language: str | None,
              context: str | None = None, auto_accept: bool = True) -> int | None:
        lang = language or "und"
        alias = _norm(raw_term)
        found = self.aliases.get((alias, lang)) or self.aliases.get((alias, "und"))
        if found:
            return found

        name = _norm(suggested)[:120] if suggested else None
        ingredient_id = self.ingredients.get(name) if name else None
        if name and ingredient_id is None and auto_accept:
            ingredient = Ingredient(canonical_name=name)
            self.session.add(ingredient)
            self.session.flush()  # for its id
            ingredient_id = self.ingredients[name] = ingredient.id
            self._review(raw_term, lang, name, context, "auto")
        if ingredient_id is None:
            self._review(raw_term, lang, name, context, "open")
            return None

        if alias != name:
            self.session.add(IngredientAlias(alias=alias, language=lang, ingredient_id=ingredient_id))
            self.aliases[(alias, lang)] = ingredient_id
        return ingredient_id

    def _review(self, raw_term, lang, suggested, context, status):
        term = raw_term[:200]
        item = self.reviews.get((term, lang))
        if item:
            item.occurrences += 1
            return
        item = self.reviews[(term, lang)] = ReviewItem(
            raw_term=term, language=lang, suggested_name=suggested, example_context=context,
            occurrences=1, status=status)
        self.session.add(item)


def match(session: Session, raw_term: str, suggested: str | None, language: str | None,
          context: str | None = None, auto_accept: bool = True) -> int | None:
    """One-off match; loads the caches, so use a Matcher for many terms."""
    return Matcher(session).match(raw_term, suggested, language, context, auto_accept)


def consolidate(session: Session) -> int:
    """Merge ingredients whose name the lexicon folds onto another one ("beef ribeye",
    "wagyū" -> beef; "parmigiano  reggiano" -> parmesan) into that ingredient: its
    components, aliases and children move over and the variant row goes. Earlier imports
    left such rows behind, and their aliases would keep catching new menus. Ingredients a
    human chose in review (an alias of a mapped or accepted term) are left alone.
    Returns the number of ingredients merged away."""
    from sqlalchemy import update

    from menudb.db.models import Component
    from menudb.extract.auto import canonical, canonical_names

    lexicon = canonical_names()
    decided = {(r.raw_term.lower(), r.language) for r in session.scalars(
        select(ReviewItem).where(ReviewItem.status.in_(("mapped", "accepted"))))}
    protected = {i for a, lang, i in session.execute(
        select(IngredientAlias.alias, IngredientAlias.language, IngredientAlias.ingredient_id))
        if (a, lang) in decided}
    by_name = {i.canonical_name: i for i in session.scalars(select(Ingredient))}
    merged = 0
    for name, ing in list(by_name.items()):
        target_name = canonical(name)
        if target_name == name or ing.id in protected:
            continue
        if target_name not in lexicon and target_name not in by_name:
            ing.canonical_name = target_name  # only the spelling changes
            by_name[target_name] = by_name.pop(name)
            continue
        target = by_name.get(target_name)
        if target is None:
            target = by_name[target_name] = Ingredient(canonical_name=target_name)
            session.add(target)
            session.flush()
        session.execute(update(Component).where(Component.ingredient_id == ing.id)
                        .values(ingredient_id=target.id))
        session.execute(update(IngredientAlias).where(IngredientAlias.ingredient_id == ing.id)
                        .values(ingredient_id=target.id))
        session.execute(update(Ingredient).where(Ingredient.parent_id == ing.id)
                        .values(parent_id=target.id))
        session.flush()
        session.delete(ing)
        del by_name[name]
        merged += 1
    session.flush()
    return merged
