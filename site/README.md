# The Menu Atlas (static site)

A static page that loads `data/data.json` and does search, filtering and charts in the browser. `export-site` splits the data so the first load stays light: `data.json` (lists, map, charts; compacted), `pairs.json` (loaded by the pairing and ingredient pages) and, with `--menus`, `menus/NN.json` (64 shards, loaded when a restaurant page opens; `NN` is FNV-1a of the slug, mod 64, the same in `site.py` and `app.js`). No build step and no dependencies.

```bash
uv run menudb export-site                  # synthetic fixture -> site/data/data.json
cd site && python3 -m http.server 8000     # open http://localhost:8000
```

- `menudb export-site --source bucket|db` writes real aggregated data. `site/data/` is gitignored; don't commit or publish it (see "Website" in `docs/PLAN.md`).
- The data holds derived facts only: ingredient names, preparations, shares, pair scores and coverage, plus restaurant names and website links. No course text, and no per-course grouping that would rebuild a dish.
- **Coverage** is a world map (Equal Earth projection, drawn on a canvas) of every restaurant in the seed list, coloured by menu status. With `--source bucket|db` it also places every restaurant in `data/seed/restaurants_full.csv` (the full list; those without menus yet show as such). Restaurants with city-level coordinates (`coord_precision=city`) are drawn faded. Scroll or pinch to zoom, drag to pan, click a country to zoom to it; countries are shaded by how many listed restaurants they have. Country outlines are Natural Earth 1:110m (public domain) in `world.js`, regenerated with `scripts/make_world.py`.
- **Accolades** come from the `lists` column of the seed and full lists: Michelin stars from the most recent guide year listed, World's 50 Best rank, NYT America 50, and NYT NYC 100 rank. Restaurants and the coverage map filter and group by them. Ingredient shares and pairings by accolade are computed per group (`by_accolade`, `pairs_by_accolade`, at most 3,000 pairs each), only for groups with at least 10 restaurants with menus (`site.MIN_GROUP`).
- **Menus** (`export-site --menus` only): each restaurant page lists its menus course by course, with ingredient links and preparations, never the menu's own wording. Real builds with menus stay private.
- Views use hash routes (`#/` home, `#/ingredients`, `#/ingredient/<name>`, `#/pairings`, `#/restaurants`, `#/restaurant/<slug>`, `#/coverage` (the Map), `#/about`), and filters live in the URL so a view can be linked.
- **Home** (`#/`) is a landing page: one search box over ingredients (and their other names) and restaurants with menus, then cards for Ingredients, Restaurants, Pairings and the Map.
- **Restaurants without menus are hidden by default** everywhere: the restaurant list defaults to "With menus" (`menus=all` shows the whole list) and the map shows only "Menus collected" until other statuses are turned on in the legend (`hide=none` shows all).
- **Look:** the "Lush forest" palette (`#2E6F40`, `#CFFFDC`, `#68BA7F`, `#253D2C`) as tokens in `style.css`, with a dark mode. Headings use Cormorant Garamond and all other text (body, tables, forms) Newsreader, both from Google Fonts. Newsreader is a text serif drawn for on-screen reading, with optical sizes so small table text stays sturdy. Michelin stars are drawn with the Michelin symbol (`#mstar` in `index.html`, traced from Wikimedia Commons' `Etoile_Michelin-1.svg`).
- `.github/workflows/site.yml` builds it from the synthetic fixture and uploads it as an artifact. Nothing deploys to GitHub Pages yet.
