"""JSON shapes for the bucket handoff: `pending/` items and `extracted/` menus.

Both live only in the private bucket. They carry menu text, so never print them.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# Bump when the extraction instructions or schema change in a way that should
# trigger re-extraction of menus extracted under an older version.
EXTRACTION_VERSION = 1


class PendingItem(BaseModel):
    """One menu document waiting for extraction."""

    slug: str
    source_label: str = "main"
    normalized_hash: str
    menu_id: int | None = None  # None when queued before `check` created the menu row
    kind: Literal["html", "pdf", "image"]
    url: str
    storage_key: str  # raw bytes in the bucket
    text: str | None = None  # None for image-only documents: read storage_key instead


Role = Literal["main", "sauce", "garnish", "seasoning", "base", "drink", "other"]
CourseKind = Literal["snack", "course", "dessert", "mignardise", "drink", "other", "section"]

# A course naming more ingredients than this is a whole section or card the extractor
# could not split into dishes. It is kept, as kind "section", but left out of pairings
# (n ingredients make n(n-1)/2 pairs) and of the site's course lists.
MAX_DISH_INGREDIENTS = 15


class ComponentOut(BaseModel):
    raw_term: str = Field(min_length=1, max_length=200)  # as written on the menu
    name: str | None = Field(None, max_length=120)  # canonical English ingredient name
    role: Role | None = None
    preparations: list[str] = Field(default_factory=list)  # e.g. grilled, fermented, cured

    @field_validator("name")
    @classmethod
    def _lower(cls, v: str | None) -> str | None:
        return v.strip().lower() or None if v else None

    @field_validator("preparations")
    @classmethod
    def _preps(cls, v: list[str]) -> list[str]:
        return [p.strip().lower()[:60] for p in v if p.strip()]


class CourseOut(BaseModel):
    position: int = Field(ge=1)
    raw_text: str  # private: never displayed or logged
    kind: CourseKind | None = None
    components: list[ComponentOut] = Field(default_factory=list)


class ExtractedMenu(BaseModel):
    """What a session writes to extracted/<slug>/<normalized_hash>.json."""

    slug: str
    normalized_hash: str
    menu_id: int | None = None
    extractor: str = Field(max_length=60)  # e.g. "claude-code-session"
    extraction_version: int = EXTRACTION_VERSION
    is_menu: bool = True  # False when the document turns out not to be a menu
    menu_type: str | None = Field(None, max_length=30)  # tasting | a_la_carte | vegetarian | ...
    language: str | None = Field(None, max_length=10)  # ISO 639-1
    price: float | None = None
    currency: str | None = Field(None, min_length=3, max_length=3)
    stated_period: str | None = None  # e.g. "autumn 2026", as stated; not stored yet
    courses: list[CourseOut] = Field(default_factory=list)
