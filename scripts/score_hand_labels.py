"""Score the rule-based extractor against hand labels (the 5-menu check in docs/PLAN.md).

    uv run python scripts/score_hand_labels.py <pick.json> <labels dir>

<pick.json> is the list of extracted menus shown on the labelling page (ExtractedMenu
JSON, script output). <labels dir> holds one <slug>.json per menu as the page saves it:
{"wrong": {"<course>:<item>": true}, "missed": {"<course>": "a, b"}, "nodish": {"<course>": true},
"done": bool}. Both stay outside the repo (they name menu terms). Prints counts and rates
only, never terms.

Precision is extracted ingredients not marked wrong over all extracted. Recall is those
right ingredients over right plus missed. Courses marked "doesn't match a dish" are left
out of both and counted separately.
"""

import json
import sys
from pathlib import Path


def score(pick: list[dict], labels: dict[str, dict]) -> list[dict]:
    rows = []
    for menu in pick:
        lab = labels.get(menu["slug"])
        if not lab:
            continue
        wrong, missed, nodish = lab.get("wrong", {}), lab.get("missed", {}), lab.get("nodish", {})
        found = right = miss = skipped = 0
        for ci, course in enumerate(menu["courses"]):
            if nodish.get(str(ci)):
                skipped += 1
                continue
            n = len(course["components"])
            bad = sum(1 for ii in range(n) if wrong.get(f"{ci}:{ii}"))
            found += n
            right += n - bad
            miss += len([x for x in (missed.get(str(ci)) or "").split(",") if x.strip()])
        rows.append({"slug": menu["slug"], "done": bool(lab.get("done")), "courses": len(menu["courses"]),
                     "not_a_dish": skipped, "found": found, "right": right, "missed": miss})
    return rows


def main() -> None:
    pick = json.loads(Path(sys.argv[1]).read_text())
    labels = {p.stem: json.loads(p.read_text()) for p in Path(sys.argv[2]).glob("*.json")}
    rows = score(pick, labels)
    tot = {k: sum(r[k] for r in rows) for k in ("found", "right", "missed", "courses", "not_a_dish")}
    for r in rows:
        p = r["right"] / r["found"] if r["found"] else 0
        rec = r["right"] / (r["right"] + r["missed"]) if r["right"] + r["missed"] else 0
        print(f"{r['slug']:<28} {'done' if r['done'] else 'open':<5} precision {p:.2f}  recall {rec:.2f}  "
              f"not a dish {r['not_a_dish']}/{r['courses']}")
    if rows:
        p = tot["right"] / tot["found"] if tot["found"] else 0
        rec = tot["right"] / (tot["right"] + tot["missed"]) if tot["right"] + tot["missed"] else 0
        print(f"{'all':<34} precision {p:.2f}  recall {rec:.2f}  (target recall 0.85)  "
              f"not a dish {tot['not_a_dish']}/{tot['courses']}")
    else:
        print("no labels yet")


if __name__ == "__main__":
    main()
