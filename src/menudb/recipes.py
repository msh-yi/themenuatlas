"""Per-restaurant navigation recipes: how to get from a start URL to the menu document(s).

    start: https://restaurant.com/
    steps:
      - follow: {text_matches: "(?i)menu|carte|tasting"}
      - select: {css: "a[href$='.pdf']", pick: first}
    render: static        # or js
    content: html_main    # or pdf | image

A third step type, `regex: {pattern, template, pick}`, pulls URLs out of JSON or
script bodies. Each step maps the current set of page URLs to a new set. Whatever URLs remain
after the last step are the menu documents to fetch and hash.
"""

import re
from pathlib import Path
from typing import Callable, Literal

import yaml

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


class RegexSpec(BaseModel):
    """For URLs that only appear in JSON or script bodies. `template` is formatted
    with the match: {0} is the whole match, {1}... the groups."""
    pattern: str
    template: str = "{0}"
    pick: Literal["first", "last", "all"] = "all"


class FollowStep(BaseModel):
    follow: FollowSpec


class SelectStep(BaseModel):
    select: SelectSpec


class RegexStep(BaseModel):
    regex: RegexSpec


class Recipe(BaseModel):
    start: str
    steps: list[FollowStep | SelectStep | RegexStep] = Field(default_factory=list)
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


def _pick(values: list[str], pick: str) -> list[str]:
    values = list(dict.fromkeys(values))  # de-duplicate, keep order
    if pick == "first":
        return values[:1]
    if pick == "last":
        return values[-1:]
    return values


def _select(base: str, html: str, spec: SelectSpec) -> list[str]:
    values = [
        absolute(base, v)
        for node in HTMLParser(html).css(spec.css)
        if (v := node.attributes.get(spec.attr))
    ]
    return _pick(values, spec.pick)


def _regex(base: str, body: str, spec: RegexSpec) -> list[str]:
    values = [
        absolute(base, spec.template.format(m.group(0), *m.groups()))
        for m in re.finditer(spec.pattern, body)
    ]
    return _pick(values, spec.pick)


def resolve(recipe: Recipe, get_html: GetHtml) -> list[str]:
    """Walk the recipe's navigation steps; returns the final document URLs."""
    urls = [recipe.start]
    for i, step in enumerate(recipe.steps):
        next_urls: list[str] = []
        for url in urls:
            final_url, html = get_html(url)
            if isinstance(step, FollowStep):
                next_urls += _follow(final_url, html, step.follow)
            elif isinstance(step, SelectStep):
                next_urls += _select(final_url, html, step.select)
            else:
                next_urls += _regex(final_url, html, step.regex)
        if not next_urls:
            raise RecipeError(f"step {i} matched nothing")
        urls = list(dict.fromkeys(next_urls))
    return urls


def load_recipes(path: Path) -> dict[str, Recipe]:
    """Read data/recipes/<slug>.yaml: a single recipe, or {label: recipe} for several menus."""
    docs = yaml.safe_load(path.read_text())
    if "start" in docs:
        docs = {"main": docs}
    return {label: Recipe.model_validate(body) for label, body in docs.items()}
