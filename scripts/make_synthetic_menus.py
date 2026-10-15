"""Write tests/fixtures/synthetic_menus.json: invented restaurants and menus for the
analysis tests and for running the notebook before real data exists.

Planted patterns: beetroot+horseradish and scallop+brown butter co-occur across
restaurants; uni is frequent in Japan; asparagus shows up in spring (north) and in
October-November (south). Run: uv run python scripts/make_synthetic_menus.py
"""

import json
import random
from pathlib import Path

rng = random.Random(7)
RESTAURANTS = [
    ("syn-nordic-a", "DK", 55.7), ("syn-nordic-b", "SE", 59.3), ("syn-france", "FR", 48.9),
    ("syn-uk", "GB", 51.5), ("syn-japan-a", "JP", 35.7), ("syn-japan-b", "JP", 35.0),
    ("syn-australia", "AU", -37.8), ("syn-peru", "PE", -12.1),
]
# Display details for the static site; the analysis ignores them.
DETAILS = {
    "syn-nordic-a": ("Synthetic Nordic A", "Copenhagen", 12.6),
    "syn-nordic-b": ("Synthetic Nordic B", "Stockholm", 18.1),
    "syn-france": ("Synthetic France", "Paris", 2.35), "syn-uk": ("Synthetic UK", "London", -0.13),
    "syn-japan-a": ("Synthetic Japan A", "Tokyo", 139.7),
    "syn-japan-b": ("Synthetic Japan B", "Kyoto", 135.8),
    "syn-australia": ("Synthetic Australia", "Melbourne", 145.0),
    "syn-peru": ("Synthetic Peru", "Lima", -77.0), "syn-no-menu": ("Synthetic No Menu", "New York", -74.0),
}
# Fixed preparations (no rng calls, so the planted ingredient draws stay the same).
PREPARATIONS = {"beetroot": ["roasted", "pickled"], "horseradish": ["grated"],
                "scallop": ["seared"], "brown butter": ["emulsified"], "rice": ["steamed"],
                "duck": ["dry-aged", "roasted"], "celeriac": ["salt-baked"], "kelp": ["dashi"],
                "mushroom": ["fermented"], "plum": ["pickled"], "rye": ["toasted"],
                "asparagus": ["grilled"], "lamb": ["smoked"], "potato": ["confit"]}
FILLER = ["apple", "barley", "celeriac", "chicken", "cucumber", "dill", "duck", "egg yolk",
          "fennel", "hazelnut", "kelp", "lamb", "leek", "mushroom", "oyster", "pear", "plum",
          "potato", "rhubarb", "rye", "sea buckthorn", "shallot", "sorrel", "turbot", "venison"]


def course(pos, extra=()):
    names = set(rng.sample(FILLER, 2)) | set(extra)
    return {"position": pos, "raw_text": "synthetic", "kind": "course",
            "components": [{"raw_term": n, "name": n, "role": "main",
                            "preparations": PREPARATIONS.get(n, [])}
                           for n in sorted(names)]}


menus = []
for slug, country, lat in RESTAURANTS:
    for k, month in enumerate([4, 10]):
        planted = []
        if slug != "syn-peru":
            planted.append(("beetroot", "horseradish"))
        if slug in ("syn-france", "syn-uk", "syn-nordic-a", "syn-australia"):
            planted.append(("scallop", "brown butter"))
        if country == "JP":
            planted.append(("uni", "rice"))
        if (lat > 0 and month == 4) or (lat < 0 and month == 10):
            planted.append(("asparagus",))
        courses = [course(i + 1, extra) for i, extra in enumerate(planted)]
        courses += [course(len(courses) + j + 1) for j in range(4)]
        menus.append({"slug": slug, "normalized_hash": f"{slug}-{k}", "extractor": "synthetic",
                      "first_seen": f"2026-{month:02d}-15", "language": "en", "courses": courses})

restaurants = [(s, c, lat, "yes") for s, c, lat in RESTAURANTS] + [("syn-no-menu", "US", 40.7, "no")]
out = {"restaurants": [{"slug": s, "name": DETAILS[s][0], "city": DETAILS[s][1], "country": c,
                        "lat": lat, "lon": DETAILS[s][2], "publishes_menu": pub}
                       for s, c, lat, pub in restaurants],
       "menus": menus}
path = Path(__file__).parents[1] / "tests/fixtures/synthetic_menus.json"
path.write_text(json.dumps(out, indent=1) + "\n")
print(f"wrote {path.name}: {len(menus)} menus")
