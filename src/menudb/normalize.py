"""Turn fetched pages into stable text so that unchanged menus hash identically.

Two outputs per document:
- `display text`: the readable menu text, kept for extraction.
- `normalized hash`: sha256 of an aggressively canonicalized form of that text,
  with volatile noise (dates, years, prices, whitespace, case) removed. Only a
  change in actual menu wording should change this hash.
"""

import hashlib
import re
import unicodedata

from selectolax.lexbor import LexborHTMLParser as HTMLParser

# Elements that never contain menu content.
_DROP_TAGS = ["script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "template"]
# Class/id fragments of cookie banners, popups and similar overlays.
_NOISE_ATTR = re.compile(
    r"cookie|consent|gdpr|onetrust|newsletter|popup|modal|banner|subscribe|chat-widget", re.I
)

_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_VOLATILE = [
    re.compile(r"\b\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b"),  # 14/10/2026, 2026-10-14
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{_MONTHS})\b", re.I),  # 14 october
    re.compile(rf"\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.I),  # october 14
    re.compile(r"\b(?:19|20)\d{2}\b"),  # bare years: "autumn 2026", "© 2026"
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm)?\b", re.I),  # times
    # prices: price-only changes are not a new menu for ingredient analysis
    re.compile(r"[$€£¥₩]\s?\d[\d.,\s]*|\d[\d.,]*\s?(?:€|eur|usd|gbp|dkk|sek|nok|chf|jpy|kr|yen|円)\b", re.I),
    re.compile(r"©.*$", re.M),
]


def html_to_text(html: str) -> str:
    """Readable text of a page with boilerplate removed, one block per line."""
    tree = HTMLParser(html)
    tree.strip_tags(_DROP_TAGS)
    for node in tree.css("[class], [id]"):
        attrs = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}"
        if _NOISE_ATTR.search(attrs) and node.tag not in ("html", "body"):
            node.decompose()
    root = tree.body or tree.root
    if root is None:
        return ""
    text = root.text(separator="\n")
    return tidy(text)


def tidy(text: str) -> str:
    """Collapse whitespace while keeping line structure; drop empty lines."""
    text = unicodedata.normalize("NFKC", text)
    lines = (re.sub(r"\s+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def canonical(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    for pattern in _VOLATILE:
        text = pattern.sub(" ", text)
    text = re.sub(r"[^\w\s]", " ", text)  # punctuation is too often cosmetic
    return " ".join(text.split())


def normalized_hash(text: str) -> str:
    return hashlib.sha256(canonical(text).encode()).hexdigest()


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
