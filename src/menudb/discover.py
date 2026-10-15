"""Find a restaurant's menu without a hand-written recipe: generic, deterministic heuristics.

From the website's home page, score links whose text or URL says "menu" in one of the
languages on the lists, visit the best few, and judge each candidate document by how many
distinct food words its text contains. The best candidate becomes a recipe (start at the
menu page, or follow a menu-named PDF link from it), which `check` and `probe` can then run.

Only scores, URLs and statuses leave this module: never document text.
"""

import re
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from menudb.extract.text import document_kind, document_text
from menudb.fetch.http import _WALLS, FetchError, Fetcher, RobotsDisallowed, absolute
from menudb.normalize import html_to_text

# Link text or URL that points at a menu, in the languages of the seed lists.
MENU_WORDS = (
    r"menus?|menù|menú|carte|la carte|cartes|tasting|degustation|dégustation|degustazione|"
    r"degustación|degustação|food|dining|our cuisine|speisekarte|menükarte|meny|menyer|menukaart|"
    r"menukort|ruokalista|cardápio|carta|kaart|lunch|dinner|omakase|kaiseki|"
    r"お品書き|献立|メニュー|料理|コース|菜单|菜單|套餐|메뉴|코스|เมนู"
)
_MENU_RE = re.compile(rf"(?i)(?<![a-z])({MENU_WORDS})(?![a-z])")
_MENU_HREF_RE = re.compile(r"(?i)(menu|carte|tasting|degust|speisekarte|meny|food|cuisine|kaart|course|omakase|kaiseki)")
# Links that look like menus but aren't the food menu.
_AVOID_RE = re.compile(
    r"(?i)(gift|voucher|gutschein|cadeau|regalo|bon-cadeau|reserv|book|booking|privacy|cookie|"
    r"career|jobs|job|press|presse|shop|boutique|newsletter|instagram|facebook|login|account|"
    r"cart|basket|warenkorb|event|private-dining|private dining|wine|vin|vino|wein|drinks|bar menu|"
    r"cocktail|allergen|terms|impressum|imprint|accessibility|sitemap|"
    r"opentable|sevenrooms|resy|tock|tablecheck|thefork|quandoo)"
)
_FILE_RE = re.compile(r"\.(jpe?g|png|gif|webp|svg|avif|mp4|mov|zip|docx?|xlsx?|ics)$")
_SOCIAL_HOSTS = re.compile(r"(?i)(instagram|facebook|twitter|x\.com|youtube|tiktok|linkedin|google\.|apple\.com|"
                           r"opentable|sevenrooms|resy\.com|exploretock|tablecheck|thefork|quandoo|michelin)")

# Distinct food words a menu tends to contain (English, French, Spanish, Italian, German,
# Nordic, Portuguese, plus romanised Japanese). Counted on whole words in lower-cased text.
FOOD_WORDS = frozenset("""
apple asparagus aubergine eggplant bass beef beetroot beet berries blackberry blueberry bread butter
buttermilk cabbage caviar celeriac celery chanterelle cheese cherry chestnut chicken chocolate clam
cod corn crab cream cucumber cumin dashi dill duck dumpling eel egg fennel fig fish garlic ginger goat
grape halibut hazelnut herring honey horseradish jus kohlrabi koji lamb langoustine leek lemon
lettuce lime lobster mackerel mango miso morel mushroom mussel mustard nori octopus oyster pea peach
pear pepper pigeon plum pork potato prawn pumpkin quail quince radish rhubarb rice saffron sake salmon
scallop seaweed sesame shallot shrimp sorrel soy squab squash squid strawberry sturgeon sweetbread
tomato trout truffle tuna turbot turnip uni urchin veal venison vinegar walnut wasabi yuzu yogurt
yoghurt almond anchovy artichoke bean beans brioche broth carrot cauliflower chervil chicory
chive citrus clementine coconut coriander courgette zucchini cress currant elderflower endive
gooseberry grouse guinea hake hare hen jerusalem juniper kale kombu lardo leek lentil liver lovage
mackerel mandarin marrow melon mint monkfish mullet nettle noodle nutmeg oat olive onion orange
parsley parsnip partridge pasta pastry pheasant pine pistachio polenta pomegranate praline
raspberry ravioli risotto romanesco rosemary rye sage salsify sardine sausage shiso snail sole
spinach sweetcorn tarragon thyme toffee tofu tuile vanilla venison verjus watercress wagyu whey
pomme poire agneau boeuf bœuf veau canard pigeonneau homard langouste crevette saumon turbot
cabillaud huître huitre oursin coquille saint-jacques champignon cèpe cepe girolle truffe beurre
crème creme fromage chou poireau navet betterave céleri celeri fenouil citron orange pêche peche
fraise framboise cerise noisette amande chocolat miel lait oeuf œuf ail oignon échalote echalote
cordero ternera cerdo pato bogavante langosta gamba gambas salmón salmon atún atun bacalao ostra
erizo vieira setas trufa mantequilla queso puerro remolacha apio hinojo limón limon naranja
melocotón fresa frambuesa cereza avellana almendra miel leche huevo ajo cebolla calabaza tomate
maíz maiz arroz agnello manzo vitello anatra astice aragosta gambero gamberi salmone tonno
merluzzo ostrica riccio capesante funghi porcini tartufo burro formaggio cavolo porro barbabietola
sedano finocchio limone arancia pesca fragola lampone ciliegia nocciola mandorla cioccolato
latte uovo aglio cipolla zucca pomodoro mais riso lamm rind kalb ente hummer garnele lachs
thunfisch kabeljau auster jakobsmuschel pilz pilze trüffel butter käse kohl lauch bete
sellerie fenchel zitrone orange pfirsich erdbeere himbeere kirsche haselnuss mandel schokolade
honig milch knoblauch zwiebel kürbis tomate reis lam okse kalv hummer reje laks torsk østers
kammusling svampe trøffel smør ost kål porre rødbede selleri fennikel citron jordbær hindbær
kirsebær hasselnød mandel chokolade honning mælk æg hvidløg løg græskar tomat ris rabarber
borrego vitela pato lagosta camarão salmão atum bacalhau ostra cogumelo trufa manteiga queijo
toro otoro chutoro ikura kani ebi hotate awabi tai hirame kinmedai nodoguro saba aji katsuo
anago unagi wagyu matsutake sansho yuba kombu shiitake
""".split())

# Scripts without spaces between words are matched as substrings instead.
CJK_FOOD = (
    "鮪 鯛 海老 伊勢海老 蟹 雲丹 うに 松茸 鮑 あわび 牛 鴨 豆腐 柚子 胡麻 椀 八寸 向付 焼物 煮物 炊合 御飯 "
    "水菓子 先付 造り 鱧 鮎 河豚 ふぐ 鰻 穴子 筍 蕪 茄子 栗 銀杏 帆立 牡蠣 甘鯛 のどぐろ 鰆 鰤 蛤 "
    "鮑魚 龍蝦 豬 雞 魚翅 燕窩 豆腐 叉燒 蝦 帶子 "
    "한우 전복 성게 김치 된장 고추장 새우 대게 송이 굴 장어 도미 "
    "กุ้ง ปู หมู ไก่ เนื้อ ปลา มะพร้าว ตะไคร้ พริก"
).split()
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af\u0e00-\u0e7f]")

_WORD_RE = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)?", re.UNICODE)

# How many distinct food words make a document count as a menu.
MENU_MIN = 12
MAYBE_MIN = 5


def food_score(text: str | None) -> int:
    if not text:
        return 0
    words = {w.lower() for w in _WORD_RE.findall(text)}
    words |= {w.rstrip("s") for w in words}
    score = len(words & FOOD_WORDS)
    if _CJK_RE.search(text):
        score += sum(1 for term in CJK_FOOD if term in text)
    return score


@dataclass
class Link:
    url: str
    score: float
    is_pdf: bool


@dataclass
class Discovery:
    status: str  # menu_found | menu_maybe | no_menu_found | needs_js | robots | bot_blocked | failed | opted_out
    recipe: dict | None = None
    menu_url: str = ""
    kind: str = ""
    score: int = 0
    note: str = ""


def _site(url: str) -> str:
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    return ".".join(host.split(".")[-2:])


def menu_links(base: str, html: str) -> list[Link]:
    """Links on a page that look like they lead to a menu, best first."""
    found: dict[str, Link] = {}
    for a in HTMLParser(html).css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "whatsapp:")):
            continue
        url = absolute(base, href).split("#")[0]
        if not url.startswith("http") or _SOCIAL_HOSTS.search(urlsplit(url).netloc):
            continue
        text = a.text(strip=True)[:80] or (a.attributes.get("title") or "") or (a.attributes.get("aria-label") or "")
        path = urlsplit(url).path.lower()
        if _FILE_RE.search(path):
            continue
        is_pdf = path.endswith(".pdf")
        if not is_pdf and _site(url) != _site(base):
            continue
        score = 0.0
        if _MENU_RE.search(text):
            score += 3
        if _MENU_HREF_RE.search(urlsplit(url).path):
            score += 2
        if is_pdf and score:
            score += 1
        if _AVOID_RE.search(text) or _AVOID_RE.search(urlsplit(url).path):
            score -= 4
        if score <= 0 or url.rstrip("/") == base.rstrip("/"):
            continue
        if url not in found or found[url].score < score:
            found[url] = Link(url, score, is_pdf)
    return sorted(found.values(), key=lambda link: -link.score)


def challenge_page(html: str) -> str | None:
    """Vendor name if the page is a bot-protection challenge rather than the site."""
    head = html[:20000]
    for name, rx in _WALLS:
        if name != "captcha" and rx.search(head):
            return name
    return None


def _visible_chars(html: str) -> int:
    return len(html_to_text(html))


def discover(
    website: str,
    fetcher: Fetcher,
    get_html: Callable[[str], tuple[str, str]] | None = None,
    max_pages: int = 4,
) -> Discovery:
    """Find the menu on one site. `get_html` defaults to plain HTTP; pass a renderer's for JS sites."""
    get_html = get_html or fetcher.get_html
    try:
        home_url, home = get_html(website)
    except RobotsDisallowed:
        return Discovery("robots", note="robots.txt disallows the home page")
    except FetchError as e:
        if e.bot_wall:
            return Discovery("bot_blocked", note=f"{e.bot_wall}: {e}"[:200])
        return Discovery("failed", note=f"{type(e).__name__}: {e}"[:200])
    except Exception as e:
        return Discovery("failed", note=f"{type(e).__name__}: {e}"[:200])

    # A challenge page served with 200 (or rendered by the browser) is still a bot wall.
    if _visible_chars(home) < 2000 and (wall := challenge_page(home)):
        return Discovery("bot_blocked", note=f"{wall} challenge page")

    links = menu_links(home_url, home)
    if not links and _visible_chars(home) < 300:
        return Discovery("needs_js", note="home page has almost no text without JavaScript")

    best: Discovery | None = None
    # (url, parent page, is_pdf): the home page itself is a candidate (one-page sites).
    queue: list[tuple[str, str, bool]] = [(home_url, "", False)]
    queue += [(link.url, home_url, link.is_pdf) for link in links[:max_pages]]
    seen: set[str] = set()
    while queue:
        url, parent, is_pdf = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            if url == home_url:
                res = None
                data, ctype, final = home.encode(), "text/html", home_url
            elif is_pdf:
                res = fetcher.get(url)
                data, ctype, final = res.content, res.content_type, res.final_url
            else:
                final, html = get_html(url)
                res, data, ctype = None, html.encode(), "text/html"
        except RobotsDisallowed:
            continue
        except Exception:
            continue
        if res is not None and res.tdm_reserved:
            return Discovery("opted_out", note="tdm-reservation")

        kind = document_kind(ctype, final, data)
        text = document_text(kind, data) if kind != "image" else None
        score = food_score(text)
        if kind == "html" and url != home_url and len(seen) <= max_pages * 2:
            # one level deeper: menu PDFs and sub-menus linked from a "Menus" page
            for link in menu_links(final, data.decode(errors="replace"))[:3]:
                queue.append((link.url, final, link.is_pdf))
        candidate = _candidate(url, parent, kind, score)
        if best is None or candidate.score > best.score or (
            candidate.score == best.score and url != home_url and best.menu_url == home_url):
            best = candidate

    if best is None or best.score < MAYBE_MIN:
        status = "needs_js" if _visible_chars(home) < 300 else "no_menu_found"
        return Discovery(status, score=best.score if best else 0, menu_url=best.menu_url if best else "",
                         note=f"{len(links)} menu-like links")
    best.status = "menu_found" if best.score >= MENU_MIN else "menu_maybe"
    return best


def _candidate(url: str, parent: str, kind: str, score: int) -> Discovery:
    if kind == "pdf" and parent:
        # Start from the page that links the PDF and follow the link by its file name, so a
        # renamed or replaced PDF on the same page is still found.
        name = re.escape(urlsplit(url).path.rsplit("/", 1)[-1])
        recipe = {"start": parent, "steps": [{"follow": {"href_matches": name}}], "content": "pdf"}
    else:
        recipe = {"start": url, "content": "pdf" if kind == "pdf" else "html_main"}
    return Discovery("", recipe=recipe, menu_url=url, kind=kind, score=score)
