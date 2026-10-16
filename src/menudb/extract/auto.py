"""Rule-based menu extraction: no LLM, no API key, no session tokens.

Splits a menu's text into dishes line by line, finds ingredients and preparations
with the multilingual lexicons in `data/lexicon/`, and returns the same
`ExtractedMenu` a session would write. It is rougher than a model: it only knows
the ingredients in the lexicon, and it leaves poetic titles and most Japanese,
Chinese and Korean menus thin. Comma-separated phrases it can't match never become
components (most are prose fragments); `extract-auto` collects them as lexicon
candidates in the bucket's review/lexicon_candidates.json instead.

Works on text only (pure functions); `menudb extract-auto` does the bucket I/O.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from menudb.extract.schema import ComponentOut, CourseOut, ExtractedMenu, PendingItem

EXTRACTOR = "lexicon-3"  # bump the number when the rules change enough to re-run

LEXICON_DIR = Path(__file__).resolve().parents[3] / "data" / "lexicon"

# --- text normalisation -------------------------------------------------------

_FOLD = str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "å": "a", "ð": "d", "þ": "th",
                       "ı": "i", "ł": "l", "’": "'", "‘": "'"})
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯豈-﫿]")
_CJK_LANGS = {"ja", "zh", "ko"}
_COMPOUND_LANGS = {"de", "nl", "da", "sv", "no"}


def fold(s: str) -> str:
    s = s.lower().translate(_FOLD)
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def stem(tok: str) -> str:
    """Crude plural/gender folding, applied the same way to lexicon terms and menu words,
    so it only has to be consistent, not linguistically right."""
    t = tok
    if len(t) > 4 and t.endswith("ies"):
        t = t[:-3] + "y"
    if len(t) > 3 and t[-1] in "sx":
        t = t[:-1]
    if len(t) >= 5 and t[-1] == "n" and t[-2] in "elr":
        t = t[:-1]
    if len(t) >= 5 and t[-1] in "aeio":
        t = t[:-1]
    return t


def tokens(s: str) -> list[tuple[str, int, int]]:
    """(stemmed folded token, start, end) for Latin-script words in `s`."""
    return [(stem(fold(m.group())), m.start(), m.end()) for m in _WORD.finditer(s)
            if not _CJK.search(m.group())]


# --- lexicons -----------------------------------------------------------------

@dataclass
class Entry:
    name: str
    category: str | None = None
    role: str | None = None  # preparations only: e.g. sauce


@dataclass
class Lexicon:
    words: dict[tuple[str, ...], list[tuple[str, Entry]]] = field(default_factory=dict)
    cjk: dict[str, list[tuple[str, Entry]]] = field(default_factory=dict)
    single: dict[str, list[tuple[str, Entry]]] = field(default_factory=dict)  # for compounds
    max_len: int = 1
    stop: dict[str, set[str]] = field(default_factory=dict)  # lang ("*" = any) -> words never matched

    def stopped(self, word: str, langs: list[str]) -> bool:
        w = word.lower()
        return any(w in self.stop.get(lg, ()) for lg in ("*", *langs))

    def add(self, lang: str, term: str, entry: Entry) -> None:
        term = term.strip()
        if not term:
            return
        if _CJK.search(term):
            self.cjk.setdefault(term, []).append((lang, entry))
            return
        key = tuple(t for t, _, _ in tokens(term))
        if not key:
            return
        self.words.setdefault(key, []).append((lang, entry))
        self.max_len = max(self.max_len, len(key))
        if len(key) == 1 and len(key[0]) >= 4:
            self.single.setdefault(key[0], []).append((lang, entry))


def _parse(path: Path, prep: bool) -> Lexicon:
    lex = Lexicon()
    category = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("## "):
            category = line[3:].strip()
            continue
        if not line or line.startswith("#"):
            continue
        head, _, body = line.partition("=")  # a bare "name > parent" has no terms
        role = None
        if prep and "@" in head:
            head, _, role = head.partition("@")
            role = role.strip()
        name = head.split(">")[0].strip()
        entry = Entry(name=name, category=None if prep else category, role=role)
        lex.add("en", name, entry)
        for part in body.split("|"):
            lang, _, terms = part.partition(":")
            for term in terms.split(","):
                lex.add(lang.strip(), term, entry)
    return lex


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


@lru_cache(maxsize=4)
def load(directory: Path = LEXICON_DIR) -> tuple[Lexicon, Lexicon]:
    ing = _parse(directory / "ingredients.txt", prep=False)
    for line in _lines(directory / "stopwords.txt"):
        lang, _, words = line.partition(":")
        ing.stop.setdefault(lang.strip(), set()).update(
            w.strip().lower() for w in words.split(",") if w.strip())
    return ing, _parse(directory / "preparations.txt", prep=True)


@lru_cache(maxsize=4)
def canonical_names(directory: Path = LEXICON_DIR) -> frozenset[str]:
    ing, _ = load(directory)
    return frozenset(e.name for es in ing.words.values() for _, e in es)


def canonical(name: str, directory: Path = LEXICON_DIR) -> str:
    """The lexicon's canonical name for an ingredient name from any extractor: Unicode,
    case and spacing normalized, and a variant the lexicon knows ("beef ribeye",
    "parmigiano reggiano", "wagyū") folded onto its ingredient (beef, parmesan, beef)."""
    name = " ".join(unicodedata.normalize("NFC", name).lower().split())
    if name in canonical_names(directory):
        return name
    hits = {h.entry.name for h in find(load(directory)[0], name, ["en"])}
    return hits.pop() if len(hits) == 1 else name


def tidy(ex: ExtractedMenu, directory: Path = LEXICON_DIR) -> ExtractedMenu:
    """Canonical names, and only courses naming at least two ingredients: a lone term
    ("Wine", "Cheese", "Tuna") is a heading, a drink line or a bare listing, and adds
    nothing to pairings. Applied to every extraction, lexicon or session, at import."""
    courses = []
    for c in ex.courses:
        comps = [comp.model_copy(update={"name": canonical(comp.name, directory)}) if comp.name else comp
                 for comp in c.components]
        # unnamed components (waiting for review) still count as ingredients
        if len({comp.name or fold(comp.raw_term) for comp in comps}) >= 2:
            courses.append(c.model_copy(update={"position": len(courses) + 1, "components": comps}))
    return ex.model_copy(update={"courses": courses})


def _pick(cands: list[tuple[str, Entry]], langs: list[str], fallback: bool = False) -> Entry | None:
    for lang in langs:
        for lg, entry in cands:
            if lg == lang:
                return entry
    # A word from another language ("Kartoffel" on an English page) still counts when
    # every language agrees on what it means.
    if fallback and cands and len({e.name for _, e in cands}) == 1:
        return cands[0][1]
    return None


@dataclass
class Hit:
    start: int
    end: int
    entry: Entry


def find(lex: Lexicon, text: str, langs: list[str]) -> list[Hit]:
    """Longest-first, non-overlapping lexicon hits in `text` for the active languages."""
    hits: list[Hit] = []
    toks = tokens(text)
    i = 0
    while i < len(toks):
        for n in range(min(lex.max_len, len(toks) - i), 0, -1):
            key = tuple(t for t, _, _ in toks[i:i + n])
            cands = lex.words.get(key)
            entry = _pick(cands, langs, fallback=len("".join(key)) >= 6) if cands else None
            if entry and n == 1 and lex.stopped(text[toks[i][1]:toks[i][2]], langs):
                entry = None
            if entry:
                hits.append(Hit(toks[i][1], toks[i + n - 1][2], entry))
                i += n
                break
        else:
            if set(langs) & _COMPOUND_LANGS and len(toks[i][0]) >= 7:
                hits.extend(_compound(lex, text, toks[i], langs))
            i += 1
    if lex.cjk and set(langs) & _CJK_LANGS and _CJK.search(text):
        hits.extend(_find_cjk(lex, text, langs, hits))
    return sorted(hits, key=lambda h: h.start)


def _compound(lex: Lexicon, text: str, tok: tuple[str, int, int], langs: list[str]) -> list[Hit]:
    """Split a German/Dutch/Scandinavian compound like 'Steinpilzrisotto' into known parts."""
    _, start, end = tok
    word = fold(text[start:end])
    if len(word) != end - start:  # folding changed the length; offsets would drift
        return []
    for k in range(3, len(word) - 3):
        tail = _pick(lex.single.get(stem(word[k:]), []), langs)
        if not tail or lex.stopped(text[start + k:end], langs):
            continue
        out = [Hit(start + k, end, tail)]
        head = word[:k]
        for link in ("", "s", "n", "en", "er", "es", "e"):
            if link and not head.endswith(link):
                continue
            stemmed = stem(head[: len(head) - len(link)] if link else head)
            first = _pick(lex.single.get(stemmed, []), langs)
            if first:
                out.insert(0, Hit(start, start + k, first))
                break
        return out
    return []


def _find_cjk(lex: Lexicon, text: str, langs: list[str], latin: list[Hit]) -> list[Hit]:
    taken = [False] * len(text)
    for h in latin:
        taken[h.start:h.end] = [True] * (h.end - h.start)
    out = []
    for term in sorted(lex.cjk, key=len, reverse=True):
        entry = _pick(lex.cjk[term], langs)
        if not entry:
            continue
        for m in re.finditer(re.escape(term), text):
            if not any(taken[m.start():m.end()]):
                taken[m.start():m.end()] = [True] * (m.end() - m.start())
                out.append(Hit(m.start(), m.end(), entry))
    return out


# --- language -----------------------------------------------------------------

_STOP = {
    "en": "the and with of our or served from menu tasting",
    "fr": "et de la le les aux du au avec des en sur menu",
    "es": "y de con la el del los las al en sobre",
    "it": "e di con la il al alla del dei delle allo in",
    "de": "und mit vom der die das an auf von im",
    "nl": "en met van het de op een",
    "pt": "e de com do da dos das ao",
    "da": "og med af fra på til",
    "sv": "och med av från på till",
}


def detect_languages(text: str) -> list[str]:
    """Most likely language first, then any close second (bilingual menus)."""
    if re.search(r"[぀-ヿ]", text):
        return ["ja", "zh", "en"]
    if re.search(r"[가-힯]", text):
        return ["ko", "en"]
    if len(re.findall(r"[一-鿿]", text)) > 30:
        return ["zh", "ja", "en"]
    words = [fold(w) for w in _WORD.findall(text)]
    scores = {lang: sum(words.count(w) for w in stop.split()) for lang, stop in _STOP.items()}
    ranked = sorted(scores, key=scores.get, reverse=True)
    top = scores[ranked[0]] or 1
    langs = [ranked[0]] + [lg for lg in ranked[1:3] if scores[lg] >= 0.4 * top]
    return langs + ([] if "en" in langs else ["en"])


# --- dish lines ---------------------------------------------------------------

_BOILER = re.compile(
    r"\breserv|\bbook(ing)? a table|newsletter|cookie|privacy|copyright|©|gift ?card|voucher|"
    r"opening hours|all rights|allerg|intoleran|dietary|vat\b|service charge|gratuity|"
    r"\bpairing|\baccords?\b|abbinament|weinbegleitung|maridaje|wine list|carte des vins|"
    r"foodborne|undercooked|tillæg|\bsupplement|\bsuppl[ée]ment|supplemento|aufpreis|per person|par personne|a persona|"
    r"wine cellar|cave à vin|follow us|instagram|facebook|\bcontact|\baddress|tel[.:]|phone|e-?mail|@|https?:|www\.|"
    r"\b(mon|tue|wed|thu|fri|sat|sun)(day|nesday|rsday|urday)?\b.*\d",
    re.I)
_PRICE = re.compile(
    r"(?:(€|£|\$|¥|₩|chf|eur|usd|gbp|jpy|dkk|sek|nok|hkd|hk\$|us\$|s\$|kr\.?|円)\s?(\d[\d.,'\s]*\d|\d))"
    r"|(?:(\d[\d.,'\s]*\d|\d)\s?(€|£|\$|¥|₩|chf|eur|euros?|usd|gbp|jpy|dkk|sek|nok|hkd|kr\.?|円|yen))",
    re.I)
_CURRENCY = {"€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "£": "GBP", "gbp": "GBP",
             "$": "USD", "usd": "USD", "us$": "USD", "¥": "JPY", "jpy": "JPY", "円": "JPY",
             "yen": "JPY", "₩": "KRW", "chf": "CHF", "dkk": "DKK", "sek": "SEK", "nok": "NOK",
             "hkd": "HKD", "hk$": "HKD", "s$": "SGD"}
_COUNTRY_KR = {"DK": "DKK", "SE": "SEK", "NO": "NOK", "IS": "ISK"}
_COUNTRY_DOLLAR = {"MX": "MXN", "CA": "CAD", "AU": "AUD", "NZ": "NZD", "HK": "HKD", "SG": "SGD",
                   "TW": "TWD", "BR": "BRL", "AR": "ARS", "CL": "CLP", "CO": "COP", "MO": "MOP"}
_COUNTRY_YEN = {"CN": "CNY"}
_CONTINUE_END = re.compile(r"([,;&+/–-]|\b(and|with|et|avec|y|con|e|mit|und|og|med|och|en|met|de|di|à|au|aux|al|alla))$", re.I)
_CONTINUE_START = re.compile(r"^([a-zà-ÿ(&+]|(?i:and|with|served|et|avec|con|mit|und|og|med|och|met)\b)")
_SPLIT = re.compile(r"[,;:&+/|·•()\[\]]|\s[–—-]\s|\b(?:and|with|et|avec|y|con|e|mit|und|og|med|och|en|met|sur|on|in|au|aux|à la|al|alla|del|della|de la|du|des|of)\b", re.I)

_HEADINGS = {
    "snack": "snack, snacks, amuse, amuses, amuse-bouche, amuse-bouches, amuse bouche, canapé, canapés, "
             "canapes, aperitivo, aperitif, apéritif, bocados, to start, starters, entrées, entradas, "
             "antipasti, vorspeisen, hors d'oeuvre, hors d'oeuvres",
    "dessert": "dessert, desserts, postre, postres, dolci, dolce, nachspeise, nachspeisen, süßes, sweets, "
               "pudding, puddings, douceurs, sucré, to finish",
    "mignardise": "mignardise, mignardises, petits fours, petit fours, friandises, gourmandises, "
                  "piccola pasticceria",
    "drink": "wine, wines, drinks, beverages, cocktails, wine pairing, non-alcoholic pairing, juice pairing, "
             "tea pairing, vins, boissons, bebidas, bevande, getränke, getranke, weinkarte, carta dei vini, "
             "carte des vins, wine list, sake, spirits, digestifs, aperitifs",
}
_HEADING_OF = {fold(w.strip()): kind for kind, ws in _HEADINGS.items() for w in ws.split(",")}
_SWEET = {"chocolate", "sugar", "honey", "vanilla", "caramel", "cocoa", "maple syrup", "tonka bean",
          "coffee", "meringue"}
_SWEET_PREPS = {"ice cream", "sorbet", "custard", "mousse", "candied"}
_SAVOURY_CATS = {"meat", "fish", "seafood"}
_SEPARATOR = re.compile(r"^[\W\d_]*$")  # ✳, ***, prices only, etc.


@dataclass
class Line:
    text: str
    hits: list[Hit]
    caps: bool


def _is_title(prev: Line, line: Line) -> bool:
    """'TUNA' or 'Cucumber' over 'Cucumber Medley; Seaweed Caviar, Green Gazpacho'."""
    if len(prev.text.split()) > 5 or re.search(r"[,;]", prev.text) or line.caps:
        return False
    if prev.caps:
        return True
    if len(prev.text.split()) <= 3 and re.search(r"[,;]", line.text) and len(line.text) > len(prev.text):
        return True  # "Scallop" over "shallot, green pepper, ..."
    names = {h.entry.name for h in prev.hits}
    return len(line.text) > len(prev.text) and names <= {h.entry.name for h in line.hits}


def _dish_lines(text: str, langs: list[str], ing: Lexicon) -> list[list[Line] | str]:
    """Blocks of candidate dish lines, with break markers ("|" for blank or skipped
    lines, "~" for explicit dividers) and headings ("#kind")."""
    out: list[list[Line] | str] = []
    block: list[Line] = []

    def flush(marker: str):
        nonlocal block
        if block:
            out.append(block)
            block = []
        out.append(marker)

    for raw in text.splitlines():
        s = " ".join(raw.split())
        if not s:
            flush("|")
            continue
        folded = fold(s).strip(" :.-")
        if folded in _HEADING_OF:
            flush("#" + _HEADING_OF[folded])
            continue
        if _SEPARATOR.match(s):
            flush("~")  # an explicit divider: ✳, ***, a price on its own line
            continue
        if len(s) > 220 or len(s.split()) > 30 or _BOILER.search(s):
            flush("|")
            continue
        stripped = _PRICE.sub("", s).strip()
        if stripped != s:
            stripped = stripped.rstrip(" .-–")
        hits = find(ing, stripped, langs)
        letters = [c for c in stripped if c.isalpha()]
        caps = bool(letters) and all(c.isupper() for c in letters if c.isascii()) and len(letters) > 2
        if not hits and not caps:
            if block and _CONTINUE_END.search(block[-1].text):
                block.append(Line(stripped, hits, caps))  # unmatched continuation of a dish
            else:
                flush("|")
            continue
        block.append(Line(stripped, hits, caps))
    flush("|")
    return out


def _split_block(block: list[Line], whole: bool) -> list[list[Line]]:
    if whole:
        return [block]
    dishes: list[list[Line]] = []
    for i, line in enumerate(block):
        prev = block[i - 1] if i else None
        joined = prev is not None and (
            _CONTINUE_END.search(prev.text) is not None
            or _CONTINUE_START.match(line.text) is not None
            or (len(dishes[-1]) == 1 and _is_title(prev, line)))
        if joined:
            dishes[-1].append(line)
        else:
            dishes.append([line])
    return [d for d in dishes if any(l.hits for l in d)]


# --- components ---------------------------------------------------------------

_FILLER = set(fold(w) for w in """
    and with et avec y con e mit und og med och en met of de di du des la le les il el the a an our
    our its their in on au aux al alla del della dello dei to from per for served variation selection
    choice homemade house seasonal season market garden farm local fresh young baby little petite
    mini warm cold hot chilled light classic traditional signature style chef chef's own day our
    fillet filet loin breast shoulder saddle belly leg rib ribs skin bone bones cheek wing thigh
    neck tail head heart side sides mains main dishes dish tasting first assortment combination
    topped made seasoned spiced spicy creamy aromatic natural white pink black sea isle bois mer
    ferme jardin saisons moment flamme france viande viandes fleisch voorgerechten hoofdgerechten
    nagerechten playing hero gallery paused verre suprême supreme fil bout coffre cotes têtes
    medley trio duo texture textures variation variations version cooked fond sauce eur menu
    menus course courses or main starter starters plat plats une un sur comme two one by op een
    experience carte cuisine maison pour every each is at your we including extra add this people
    gram grams g food entree appetizers classics bites nos choix par qui das wir vom d l c i n u sa
    son lunch dinner dessert desserts sweet red green spring grand nature french italian
    mediterranean glass origin delicate hommage instants palette viaggio che ttc apero essence
    condiment condiments prepared based mild hand meat meats fish fruit fruits vegetarian vegan
    """.split())
_ROLE_BY_CAT = {"herb": "seasoning", "spice": "seasoning", "grain": "base"}


def _components(dish: str, langs: list[str], ing: Lexicon, prep: Lexicon,
                candidates: list[str] | None = None) -> list[ComponentOut]:
    hits = find(ing, dish, langs)
    preps = find(prep, dish, langs)
    # segment boundaries: commas, "and"/"with", brackets, ...
    cuts = sorted({0, len(dish)} | {m.start() for m in _SPLIT.finditer(dish)})

    def segment(pos: int) -> int:
        return max(i for i, c in enumerate(cuts) if c <= pos)

    # preparations in the same segment, attached to the nearest ingredient
    attached: dict[int, list[Hit]] = {}
    for p in preps:
        inside = [j for j, h in enumerate(hits) if h.start <= p.start < h.end]
        if inside:  # "smoked eel" is one term; "sauce soja" is soy sauce, not a sauce of soy
            if not p.entry.role:
                attached.setdefault(inside[0], []).append(p)
            continue
        seg = segment(p.start)
        near = [j for j, h in enumerate(hits) if segment(h.start) == seg]
        if near:
            j = min(near, key=lambda j: min(abs(hits[j].start - p.end), abs(p.start - hits[j].end)))
            attached.setdefault(j, []).append(p)

    sauce_segs = {segment(p.start) for p in preps if p.entry.role == "sauce"
                  and not any(h.start <= p.start < h.end for h in hits)}
    first_seg = segment(hits[0].start) if hits else 0
    out: dict[str, ComponentOut] = {}
    for j, h in enumerate(hits):
        names = [p.entry.name for p in attached.get(j, [])]
        cat = h.entry.category
        if segment(h.start) in sauce_segs:
            role = "sauce"
        elif cat in _ROLE_BY_CAT:
            role = _ROLE_BY_CAT[cat]
        elif h.entry.name in {"stock", "jus"}:
            role = "sauce"
        elif segment(h.start) == first_seg:
            role = "main"
        else:
            role = "garnish"
        name = h.entry.name
        if name in out:  # one component per ingredient per dish
            out[name].preparations = sorted(set(out[name].preparations) | set(names))
            continue
        out[name] = ComponentOut(raw_term=dish[h.start:h.end][:200], name=name, role=role,
                                 preparations=sorted(set(names)))

    # Unmatched short phrases are lexicon candidates, never components: most are
    # fragments of prose ("around", "his image as gracious"), not ingredients.
    for a, b in zip(cuts, cuts[1:]):
        if any(h.start < b and h.end > a for h in hits):
            continue
        chars = list(dish[a:b])
        for p in preps:  # "Zorblax broth" -> "Zorblax"
            for i in range(max(p.start, a), min(p.end, b)):
                chars[i - a] = " "
        phrase = " ".join("".join(chars).split()).strip(" ,;:&+/|·•()[]–—-.\"'“”")
        words = [w for w in phrase.split() if fold(w).strip(".,'’") not in _FILLER]
        phrase = " ".join(words)
        if 1 <= len(words) <= 4 and len(phrase) >= 3 and not _PRICE.search(phrase) \
                and any(c.isalpha() for c in phrase) and fold(phrase) not in _HEADING_OF:
            if candidates is not None:
                candidates.append(phrase[:200])
    return list(out.values())


def _kind(comps: list[ComponentOut], heading: str | None, idx: int, n: int,
          categories: dict[str, str]) -> str:
    if heading:
        return heading
    names = {c.name for c in comps if c.name}
    preps = {p for c in comps for p in c.preparations}
    savoury = any(categories.get(nm) in _SAVOURY_CATS for nm in names)
    if not savoury and idx >= n * 0.6 and (names & _SWEET or preps & _SWEET_PREPS):
        return "dessert"
    return "course"


# --- menu level ---------------------------------------------------------------

_MENU_TYPES = [
    ("kaiseki", r"kaiseki|懐石|会席"),
    ("tasting", r"tasting|d[ée]gustation|degustaci[oó]n|degustazione|omakase|おまかせ|chef'?s menu|"
                r"carte blanche|menu d[ée]couverte|discovery menu|men[uü] (?:in|en) \d|\d[- ]course|\d plats|"
                r"\d gänge|\d gang|\d portate|\d tiempos|surprise"),
    ("a_la_carte", r"[àa] la carte"),
]


def _menu_type(text: str, n_prices: int, n_dishes: int) -> str | None:
    head = " ".join(text.splitlines()[:25])
    if re.search(r"\b(vegetarian|v[ée]g[ée]tarien|vegetariano|vegetarisch|plant[- ]based|vegan)\b.{0,15}\bmen[uü]|"
                 r"\bmen[uü]\b.{0,15}\b(vegetarian|v[ée]g[ée]tarien|vegetariano|vegetarisch|vegan)", head, re.I):
        return "vegetarian"
    if re.search(r"prix[- ]fixe|menu du jour|men[uú] del d[ií]a", text, re.I):
        return "prix_fixe_lunch" if re.search(r"\b(lunch|d[ée]jeuner|almuerzo|pranzo|mittag)", head, re.I) \
            else "prix_fixe_dinner"
    for kind, pat in _MENU_TYPES:
        if re.search(pat, text, re.I):
            if kind == "tasting" and n_prices >= max(4, n_dishes // 2):
                return "a_la_carte"
            return kind
    if re.search(r"\b(lunch|d[ée]jeuner|almuerzo|pranzo|mittag)", head, re.I):
        return "lunch"
    if n_prices >= max(4, n_dishes // 2):
        return "a_la_carte"
    return None


def _amount(s: str) -> float | None:
    s = s.replace(" ", "").replace("'", "")
    if re.fullmatch(r"\d{1,3}([.,]\d{3})+", s):
        s = re.sub(r"[.,]", "", s)
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _prices(text: str, country: str | None) -> list[tuple[float, str, bool]]:
    """(amount, currency, is_set_price_line) for each price that isn't a pairing or supplement."""
    out = []
    for line in text.splitlines():
        if re.search(r"pairing|accord|abbinament|weinbegleitung|maridaje|wine|vin\b|supplement|"
                     r"suppl[ée]ment|supplemento|aufpreis|gift|voucher|deposit|cancel", line, re.I):
            continue
        for m in _PRICE.finditer(line):
            sym = (m.group(1) or m.group(4) or "").lower().rstrip(".")
            amount = _amount(m.group(2) or m.group(3) or "")
            cur = _CURRENCY.get(sym) or (_COUNTRY_KR.get(country or "") if sym == "kr" else None)
            if sym == "$":
                cur = _COUNTRY_DOLLAR.get(country or "", cur)
            elif sym == "¥":
                cur = _COUNTRY_YEN.get(country or "", cur)
            if amount and cur and amount > 0:
                setline = bool(re.search(r"men[uü]|tasting|d[ée]gustation|degusta|course|plats|"
                                         r"gänge|gang|portate|prix|omakase|kaiseki|per person|pp\b|"
                                         r"par personne|a persona|pro person", line, re.I))
                out.append((amount, cur, setline))
    return out


def extract(item: PendingItem, country: str | None = None, lexicon_dir: Path = LEXICON_DIR,
            candidates: list[str] | None = None) -> ExtractedMenu:
    """`candidates`, if given, collects unmatched phrases from dish lines (for the lexicon)."""
    ing, prep = load(lexicon_dir)
    text = item.text or ""
    base = dict(slug=item.slug, normalized_hash=item.normalized_hash, menu_id=item.menu_id,
                extractor=EXTRACTOR)
    if not text.strip():
        return ExtractedMenu(**base, is_menu=False)
    langs = detect_languages(text)
    blocks = _dish_lines(text, langs, ing)
    # Menus that put a divider (✳, a price) between most dishes keep each block as one dish
    idx = [i for i, b in enumerate(blocks) if isinstance(b, list)]
    sizes = sorted(len(blocks[i]) for i in idx)
    divided = sum(1 for i in idx if "~" in (blocks[i - 1] if i else None, blocks[i + 1] if i + 1 < len(blocks) else None))
    whole = len(idx) >= 3 and divided >= 0.6 * len(idx) and sizes[len(sizes) // 2] <= 3

    dishes: list[tuple[str, str | None]] = []
    heading = None
    for b in blocks:
        if isinstance(b, str):
            if b.startswith("#"):
                heading = b[1:]
            continue
        if heading == "drink":
            continue  # wine list or pairing: not dishes
        for d in _split_block(b, whole):
            dishes.append(("\n".join(l.text for l in d), heading))

    categories = {e.name: e.category for es in ing.words.values() for _, e in es}
    courses = []
    seen = set()
    for raw, head in dishes:
        if fold(raw) in seen:
            continue  # the same dish on two menus of one page counts once
        seen.add(fold(raw))
        found: list[str] = []
        comps = _components(raw.replace("\n", " , "), langs, ing, prep, found)
        named = {c.name for c in comps if c.name}
        if named and candidates is not None:
            candidates.extend(found)  # a lone match next to unknown words is a lexicon gap
        if len(named) < 2:
            continue  # a lone term ("Wine", "Cheese", "Tuna") is not a dish; see tidy()
        courses.append((raw, head, comps))
    out = []
    for i, (raw, head, comps) in enumerate(courses):
        out.append(CourseOut(position=i + 1, raw_text=raw,
                             kind=_kind(comps, head, i, len(courses), categories),
                             components=comps))

    prices = _prices(text, country)
    menu_type = _menu_type(text, len(prices), len(out))
    set_prices = [p for p in prices if p[2]] or (prices if len(prices) <= 3 else [])
    price = currency = None
    if prices:
        currency = max({p[1] for p in prices}, key=lambda c: sum(p[1] == c for p in prices))
    if set_prices and menu_type != "a_la_carte":
        best = max((p for p in set_prices if p[1] == currency), key=lambda p: p[0], default=None)
        price = best[0] if best else None
    # A menu has several dishes of more than one ingredient; a harvest list or a
    # landing page that names a few ingredients in prose does not.
    is_menu = sum(1 for c in out if sum(1 for x in c.components if x.name) >= 2) >= 4
    if menu_type is None and is_menu and price is not None:
        menu_type = "tasting"  # one set price and no other clue
    return ExtractedMenu(**base, is_menu=is_menu, menu_type=menu_type if is_menu else None,
                         language=langs[0], price=price, currency=currency,
                         courses=out if is_menu else [])
