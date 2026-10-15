"""Per-restaurant navigation recipes: how to get from a start URL to the menu document(s).

    start: https://restaurant.com/
    steps:
      - follow: {text_matches: "(?i)menu|carte|tasting"}
      - select: {css: "a[href$='.pdf']", pick: first}
    render: static        # or js
    content: html_main    # or pdf | image

Each step maps the current set of page URLs to a new set. Whatever URLs remain
after the last step are the menu documents to fetch and hash.
"""

import re
from typing import Callable, Literal

from pydantic import BaseModel, Field
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from menudb.fetch.http import absolute


class RecipeError(Exception):
    pass


class FollowSpec(BaseModel):
    text_matches: str | None = None  # regex on link text
    href_matches: str | None = None  # regex on href


class SelectSpec(BaseModel):
    css: str
    attr: str = "href"
    pick: Literal["first", "last", "all"] = "first"


class FollowStep(BaseModel):
    follow: FollowSpec


class SelectStep(BaseModel):
    select: SelectSpec


class Recipe(BaseModel):
    start: str
    steps: list[FollowStep | SelectStep] = Field(default_factory=list)
    render: Literal["static", "js"] = "static"
    content: Literal["html_main", "pdf", "image"] = "html_main"


# (url) -> (final_url, html)
GetHtml = Callable[[str], tuple[str, str]]


def _follow(base: str, html: str, spec: FollowSpec) -> list[str]:
    text_re = re.compile(spec.text_matches) if spec.text_matches else None
    href_re = re.compile(spec.href_matches) if spec.href_matches else None
    for a in HTMLParser(html).css("a[href]"):
        href = a.attributes.get("href") or ""
        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if text_re and not text_re.search(a.text(strip=True)):
            continue
        if href_re and not href_re.search(href):
            continue
        return [absolute(base, href)]
    return []


def _select(base: str, html: str, spec: SelectSpec) -> list[str]:
    values = [
        absolute(base, v)
        for node in HTMLParser(html).css(spec.css)
        if (v := node.attributes.get(spec.attr))
    ]
    values = list(dict.fromkeys(values))  # de-duplicate, keep order
    if not values:
        return []
    if spec.pick == "first":
        return values[:1]
    if spec.pick == "last":
        return values[-1:]
    return values


def resolve(recipe: Recipe, get_html: GetHtml) -> list[str]:
    """Walk the recipe's navigation steps; returns the final document URLs."""
    urls = [recipe.start]
    for i, step in enumerate(recipe.steps):
        next_urls: list[str] = []
        for url in urls:
            final_url, html = get_html(url)
            if isinstance(step, FollowStep):
                next_urls += _follow(final_url, html, step.follow)
            else:
                next_urls += _select(final_url, html, step.select)
        if not next_urls:
            raise RecipeError(f"step {i} matched nothing")
        urls = list(dict.fromkeys(next_urls))
    return urls
