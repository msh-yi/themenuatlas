from datetime import datetime, timedelta

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Interval,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Restaurant(Base):
    __tablename__ = "restaurant"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(120))
    country: Mapped[str | None] = mapped_column(String(2))  # ISO 3166-1 alpha-2
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    website: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | closed
    publishes_menu: Mapped[str] = mapped_column(String(10), default="unknown")  # yes | no | unknown
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    memberships: Mapped[list["ListMembership"]] = relationship(back_populates="restaurant")
    sources: Mapped[list["Source"]] = relationship(back_populates="restaurant")
    menus: Mapped[list["Menu"]] = relationship(back_populates="restaurant")


class ListMembership(Base):
    __tablename__ = "list_membership"
    __table_args__ = (UniqueConstraint("restaurant_id", "list_name", "year"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurant.id", ondelete="CASCADE"))
    list_name: Mapped[str] = mapped_column(String(40))  # michelin | w50b | ...
    year: Mapped[int] = mapped_column(Integer)
    value: Mapped[str] = mapped_column(String(20))  # stars ("3") or rank ("12")

    restaurant: Mapped[Restaurant] = relationship(back_populates="memberships")


class Source(Base):
    """One place a restaurant's menu can be found, with the recipe to reach it."""

    __tablename__ = "source"

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurant.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(60), default="main")  # e.g. tasting, vegetarian
    recipe: Mapped[dict] = mapped_column(JSONB)
    last_verified: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check_interval: Mapped[timedelta] = mapped_column(Interval, default=timedelta(days=7))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    # conditional-GET validators for the final menu URL(s), keyed by URL
    validators: Mapped[dict] = mapped_column(JSONB, default=dict)
    robots_ok: Mapped[bool | None]
    tdm_reserved: Mapped[bool | None]
    # "check": `menudb check` fetches it on its cadence (curated recipes in data/recipes/).
    # "bucket": recheck.yml fetches it with no database and import records what it found
    # (auto-discovered recipes in data/recipes_auto/); `check` skips these.
    mode: Mapped[str] = mapped_column(String(10), default="check", server_default="check")

    restaurant: Mapped[Restaurant] = relationship(back_populates="sources")
    snapshots: Mapped[list["Snapshot"]] = relationship(back_populates="source")


class Snapshot(Base):
    """An immutable fetch. Raw bytes live in object storage under content_hash."""

    __tablename__ = "snapshot"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id", ondelete="CASCADE"))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    origin: Mapped[str] = mapped_column(String(10))  # live | wayback
    wayback_ts: Mapped[str | None] = mapped_column(String(14))
    url: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(20))  # html | pdf | image
    content_hash: Mapped[str] = mapped_column(String(64))
    normalized_hash: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str] = mapped_column(Text)

    source: Mapped[Source] = relationship(back_populates="snapshots")


class Menu(Base):
    """A distinct menu: one normalized_hash per restaurant, seen over a time window."""

    __tablename__ = "menu"
    __table_args__ = (UniqueConstraint("restaurant_id", "normalized_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurant.id", ondelete="CASCADE"))
    normalized_hash: Mapped[str] = mapped_column(String(64))
    first_snapshot_id: Mapped[int] = mapped_column(ForeignKey("snapshot.id"))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    menu_type: Mapped[str | None] = mapped_column(String(30))  # tasting | a_la_carte | vegetarian | ...
    language: Mapped[str | None] = mapped_column(String(10))
    price: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    extraction_version: Mapped[int | None] = mapped_column(Integer)
    extraction_model: Mapped[str | None] = mapped_column(String(60))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Set by `menudb exclude` (e.g. "search_rejected": the menu came from a website wrongly
    # matched to this restaurant). Excluded menus stay in the database but are left out of
    # analysis, the site, review and the extraction queue; clearing it undoes the exclusion.
    excluded_reason: Mapped[str | None] = mapped_column(String(40))

    restaurant: Mapped[Restaurant] = relationship(back_populates="menus")
    courses: Mapped[list["Course"]] = relationship(
        back_populates="menu", cascade="all, delete-orphan", order_by="Course.position"
    )


class Course(Base):
    __tablename__ = "course"

    id: Mapped[int] = mapped_column(primary_key=True)
    menu_id: Mapped[int] = mapped_column(ForeignKey("menu.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(Text)  # private: never displayed verbatim
    kind: Mapped[str | None] = mapped_column(String(20))  # snack | course | dessert | mignardise

    menu: Mapped[Menu] = relationship(back_populates="courses")
    components: Mapped[list["Component"]] = relationship(
        back_populates="course", cascade="all, delete-orphan"
    )


class Ingredient(Base):
    __tablename__ = "ingredient"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_name: Mapped[str] = mapped_column(String(120), unique=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("ingredient.id"))
    category: Mapped[str | None] = mapped_column(String(40))
    is_composite: Mapped[bool] = mapped_column(default=False)  # dashi, XO sauce, garum


class IngredientAlias(Base):
    __tablename__ = "ingredient_alias"
    __table_args__ = (UniqueConstraint("alias", "language"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    alias: Mapped[str] = mapped_column(String(160))  # lowercased
    language: Mapped[str] = mapped_column(String(10), default="und")
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("ingredient.id", ondelete="CASCADE"))


class Component(Base):
    __tablename__ = "component"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("course.id", ondelete="CASCADE"))
    ingredient_id: Mapped[int | None] = mapped_column(ForeignKey("ingredient.id"))  # null until reviewed
    raw_term: Mapped[str] = mapped_column(String(200))
    suggested_name: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[str | None] = mapped_column(String(20))  # main | sauce | garnish | ...
    preparations: Mapped[list[str]] = mapped_column(ARRAY(String(60)), default=list)

    course: Mapped[Course] = relationship(back_populates="components")


class ReviewItem(Base):
    """Extracted terms with no alias match, waiting for a human to map them."""

    __tablename__ = "review_queue"
    __table_args__ = (UniqueConstraint("raw_term", "language"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    raw_term: Mapped[str] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(10), default="und")
    suggested_name: Mapped[str | None] = mapped_column(String(120))
    example_context: Mapped[str | None] = mapped_column(Text)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | auto | mapped | ignored
