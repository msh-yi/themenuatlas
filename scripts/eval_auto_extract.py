"""Score the rule-based extractor against session extractions (the reference set).

    uv run python scripts/eval_auto_extract.py build <dir>   # from the bucket: imported/ + extracted/ + raw/
    uv run python scripts/eval_auto_extract.py score <dir>   # prints aggregate scores only

`build` pairs each session extraction with the raw document it came from (same
normalized hash) and writes <dir>/<slug>--<hash>.json holding the text and the
reference. Keep <dir> outside the repo: it holds menu text. `score` prints
counts and rates, never menu text or terms.
"""

import json
import sys
from collections import Counter
from pathlib import Path

from menudb.extract.auto import EXTRACTOR, extract, load
from menudb.extract.schema import PendingItem


def build(out: Path) -> None:
    from menudb import storage
    from menudb.extract.text import document_text
    from menudb.normalize import content_hash, normalized_hash

    gold = {}
    for prefix in ("imported/", "extracted/"):
        for k in storage.list_keys(prefix):
            g = json.loads(storage.get_raw(k))
            if not g.get("extractor", "").startswith("lexicon"):
                gold[g["normalized_hash"]] = g
    out.mkdir(parents=True, exist_ok=True)
    for k in storage.list_keys("raw/"):
        data = storage.get_raw(k)
        kind = "pdf" if data[:5] == b"%PDF-" else "html"
        try:
            text = document_text(kind, data)
        except Exception:
            continue
        h = normalized_hash(text) if text else content_hash(data)
        if h in gold:
            g = gold.pop(h)
            (out / f"{g['slug']}--{h[:12]}.json").write_text(json.dumps({"text": text, "gold": g}))
    print(f"built {len(list(out.glob('*.json')))}; unmatched {len(gold)}")


def _head(name: str) -> str:
    return name.split()[-1] if name else name


def score(ref: Path) -> None:
    ing, _ = load()
    known = {e.name for es in ing.words.values() for _, e in es}
    parent = {}
    for line in (Path(__file__).resolve().parents[1] / "data/lexicon/ingredients.txt").read_text().splitlines():
        head = line.split("=")[0]
        if ">" in head:
            a, b = head.split(">")
            parent[a.strip()] = b.strip()
    import csv
    countries = {r["slug"]: r["country"] for r in csv.DictReader(
        (Path(__file__).resolve().parents[1] / "data/seed/restaurants_full.csv").open())}
    c = Counter()
    rows = []
    for f in sorted(ref.glob("*.json")):
        d = json.loads(f.read_text())
        g = d["gold"]
        item = PendingItem(slug=g["slug"], normalized_hash=g["normalized_hash"], kind="html",
                           url="", storage_key="", text=d["text"])
        ex = extract(item, country=countries.get(g["slug"]))
        c["menus"] += 1
        c["is_menu_ok"] += ex.is_menu == g["is_menu"]
        c["lang_ok"] += ex.language == g["language"]
        if not g["is_menu"]:
            continue
        gnames = {x["name"] for co in g["courses"] for x in co["components"] if x["name"]}
        mine = {x.name for co in ex.courses for x in co.components if x.name}
        up = lambda n: {n, parent.get(n, n), _head(n)}
        mine_up = set().union(*(up(n) for n in mine)) if mine else set()
        strict = len(gnames & mine)
        lenient = sum(1 for n in gnames if up(n) & mine_up)
        prec = sum(1 for n in mine if any(up(n) & up(gn) for gn in gnames))
        c["gold_names"] += len(gnames)
        c["gold_in_lexicon"] += sum(1 for n in gnames if up(n) & known)
        c["strict"] += strict
        c["lenient"] += lenient
        c["mine"] += len(mine)
        c["prec"] += prec
        c["gold_courses"] += len(g["courses"])
        c["courses"] += len(ex.courses)
        c["price_ok"] += (g.get("price") == ex.price)
        c["currency_ok"] += (g.get("currency") == ex.currency)
        c["type_ok"] += (g.get("menu_type") == ex.menu_type)
        c["dish_menus"] += 1
        rows.append((g["slug"], len(g["courses"]), len(ex.courses), lenient, len(gnames), prec, len(mine)))
    print(f"extractor {EXTRACTOR}: {c['menus']} reference menus")
    print(f"is_menu agrees {c['is_menu_ok']}/{c['menus']}, language {c['lang_ok']}/{c['menus']}")
    n = c["dish_menus"] or 1
    print(f"menus with dishes {c['dish_menus']}: courses {c['courses']} vs {c['gold_courses']} reference; "
          f"menu_type {c['type_ok']}/{n}, price {c['price_ok']}/{n}, currency {c['currency_ok']}/{n}")
    g = c["gold_names"] or 1
    print(f"ingredient recall per menu (distinct names): strict {c['strict'] / g:.2f}, "
          f"lenient {c['lenient'] / g:.2f} (parent or head noun); "
          f"precision {c['prec'] / max(c['mine'], 1):.2f}; reference names the lexicon knows "
          f"{c['gold_in_lexicon'] / g:.2f}")
    for r in rows:
        print("  %-22s courses %3d/%3d  recall %3d/%3d  precision %3d/%3d" % (r[0], r[2], r[1], r[3], r[4], r[5], r[6]))


if __name__ == "__main__":
    {"build": build, "score": score}[sys.argv[1]](Path(sys.argv[2]))
