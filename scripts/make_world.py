"""Write site/world.js: Natural Earth 1:110m country outlines (public domain) as lon/lat
rings for the Coverage map. Input is world-atlas's countries-110m.json TopoJSON
(https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json).

Run: uv run python -I scripts/make_world.py <path to countries-110m.json>
"""

import json
import sys
from pathlib import Path

topo = json.loads(Path(sys.argv[1]).read_text())
(sx, sy), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]


def decode(arc):
    x = y = 0
    out = []
    for dx, dy in arc:
        x, y = x + dx, y + dy
        out.append((x * sx + tx, y * sy + ty))
    return out


arcs = [decode(a) for a in topo["arcs"]]


def ring(indexes):
    pts = []
    for i in indexes:
        a = arcs[i] if i >= 0 else arcs[~i][::-1]
        pts += a[1:] if pts else a
    return pts


def unwrap(pts):
    """Make a ring that crosses the antimeridian continuous in longitude."""
    out = [pts[0]]
    for lon, lat in pts[1:]:
        prev = out[-1][0]
        while lon - prev > 180:
            lon -= 360
        while prev - lon > 180:
            lon += 360
        out.append((lon, lat))
    return out


def pieces(pts):
    """A crossing ring becomes two rings clamped to +-180: a cut at the antimeridian."""
    pts = unwrap(pts)
    lons = [p[0] for p in pts]
    if min(lons) >= -180 and max(lons) <= 180:
        return [pts]
    shift = 360 if min(lons) < -180 else -360
    clamp = lambda q: [(max(-180, min(180, lon)), lat) for lon, lat in q]
    return [clamp(pts), clamp([(lon + shift, lat) for lon, lat in pts])]


SKIP = {"Antarctica", "Fr. S. Antarctic Lands"}  # no restaurants; rings run along the pole
countries = []
for geom in topo["objects"]["countries"]["geometries"]:
    name = geom["properties"]["name"]
    if name in SKIP or geom["type"] not in ("Polygon", "MultiPolygon"):
        continue
    polys = geom["arcs"] if geom["type"] == "MultiPolygon" else [geom["arcs"]]
    rings = []
    for poly in polys:
        for r in poly:
            pts = ring(r)
            if len(pts) >= 4:
                rings += [[round(v, 1) for p in piece for v in p] for piece in pieces(pts)]
    countries.append({"n": name, "r": rings})

out = Path(__file__).parents[1] / "site/world.js"
out.write_text("// Natural Earth 1:110m countries (public domain), from scripts/make_world.py.\n"
               "// [{n: name, r: [[lon, lat, lon, lat, ...], ...]}]; holes are rings too (even-odd fill).\n"
               f"window.MENU_ATLAS_WORLD = {json.dumps(countries, separators=(',', ':'), ensure_ascii=False)};\n")
print(f"wrote {out.name}: {len(countries)} countries, {out.stat().st_size} bytes")
