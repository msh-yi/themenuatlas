"""Edits to data/lexicon/ingredients.txt from review decisions (`menudb review decide`).

Lines keep the lexicon's format (see the header of ingredients.txt):

    canonical [> parent] = lang: term, term | lang: term

A new term joins its canonical's line, in that language's segment; a new ingredient
goes at the end of its `## category` section. Nothing here reads or writes menu text:
only the reviewed term is written, and only when a person approved or merged it.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from menudb.extract import auto

CATEGORIES = ["vegetable", "fungus", "fruit", "nut", "grain", "herb", "spice", "other",
              "dairy", "seafood", "fish", "meat"]
_BAD = re.compile(r"[=|,>@#\n]")


@dataclass
class LexiconFile:
    path: Path
    lines: list[str] = field(default_factory=list)
    _cache: tuple | None = field(default=None, repr=False)

    @classmethod
    def read(cls, path: Path) -> "LexiconFile":
        return cls(path, path.read_text(encoding="utf-8").splitlines())

    def write(self) -> None:
        self.path.write_text("\n".join(self.lines) + "\n", encoding="utf-8")

    # --- lookups ---------------------------------------------------------------

    def _entries(self):
        """(line index, canonical, category) for every entry line."""
        category = None
        for i, raw in enumerate(self.lines):
            line = raw.strip()
            if line.startswith("## "):
                category = line[3:].strip()
            elif line and not line.startswith("#"):
                yield i, line.partition("=")[0].split(">")[0].strip().lower(), category

    def canonicals(self) -> dict[str, str | None]:
        """canonical -> category."""
        return {name: cat for _, name, cat in self._entries()}

    def parsed(self) -> auto.Lexicon:
        """The lexicon as the extractor reads it, from the current (edited) lines."""
        text = "\n".join(self.lines)
        if self._cache is None or self._cache[0] != text:
            self._cache = (text, auto._parse(SimpleNamespace(read_text=lambda encoding=None: text),
                                             prep=False))
        return self._cache[1]

    def lookup(self, term: str, language: str) -> set[str]:
        """Canonicals that `term` already resolves to in `language` (or English)."""
        lex = self.parsed()
        if auto._CJK.search(term):
            cands = lex.cjk.get(term.strip(), [])
        else:
            cands = lex.words.get(tuple(t for t, _, _ in auto.tokens(term)), [])
        return {e.name for lg, e in cands if lg in (language, "en")}

    # --- edits -----------------------------------------------------------------

    def add_term(self, canonical: str, language: str, term: str) -> bool:
        """Add `term` to `canonical`'s line. False if it is already there."""
        idx = next(i for i, name, _ in self._entries() if name == canonical)
        line = self.lines[idx]
        head, eq, body = line.partition("=")
        folded = auto.fold(term.strip())
        known = {auto.fold(t.strip()) for part in body.split("|")
                 if part.partition(":")[0].strip() == language
                 for t in part.partition(":")[2].split(",")}
        if folded in known or (language == "en" and folded == auto.fold(head.split(">")[0].strip())):
            return False
        parts = [p.strip() for p in body.split("|") if p.strip()] if eq else []
        for j, part in enumerate(parts):
            if part.partition(":")[0].strip() == language:
                parts[j] = f"{part}, {term.strip()}"
                break
        else:
            parts.append(f"{language}: {term.strip()}")
        self.lines[idx] = f"{head.rstrip()} = " + " | ".join(parts)
        return True

    def add_entry(self, canonical: str, category: str, language: str, term: str,
                  parent: str | None = None) -> None:
        """A new ingredient at the end of its `## category` section."""
        head = canonical + (f" > {parent}" if parent else "")
        term = term.strip()
        line = head if auto.fold(term) == auto.fold(canonical) else f"{head} = {language}: {term}"
        sections = [i for i, raw in enumerate(self.lines) if raw.strip().startswith("## ")]
        start = next((i for i in sections if self.lines[i].strip()[3:].strip() == category), None)
        if start is None:
            while self.lines and not self.lines[-1].strip():
                self.lines.pop()
            self.lines += ["", f"## {category}", line]
            return
        end = next((i for i in sections if i > start), len(self.lines))
        while end > start + 1 and not self.lines[end - 1].strip():
            end -= 1
        self.lines.insert(end, line)


def valid_name(s: str | None) -> bool:
    return bool(s and s.strip() and len(s) <= 120 and not _BAD.search(s))
