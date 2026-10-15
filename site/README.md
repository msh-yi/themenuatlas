# Menu Atlas (static site)

A static page that loads `data/data.json` and does search, filtering and charts in the browser. No build step and no dependencies.

```bash
uv run menudb export-site                  # synthetic fixture -> site/data/data.json
cd site && python3 -m http.server 8000     # open http://localhost:8000
```

- `menudb export-site --source bucket|db` writes real aggregated data. `site/data/` is gitignored; don't commit or publish it (see "Website" in `docs/PLAN.md`).
- The data holds derived facts only: ingredient names, preparations, shares, pair scores and coverage, plus restaurant names and website links. No course text, and no per-course grouping that would rebuild a dish.
- Views use hash routes (`#/`, `#/ingredient/<name>`, `#/pairings`, `#/restaurants`, `#/restaurant/<slug>`, `#/coverage`, `#/about`), and filters live in the URL so a view can be linked.
- `.github/workflows/site.yml` builds it from the synthetic fixture and uploads it as an artifact. Nothing deploys to GitHub Pages yet.
