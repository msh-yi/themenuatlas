# Plan: first slice (pipeline + analysis)

Approved 2026-10-14. Updated the same day: Neon replaces Cloudflare R2, and extraction runs in Claude Code sessions instead of through the Anthropic API. Updated 2026-10-15: cloud sessions can't reach Postgres, so they hand work to and from Actions through the bucket (see "Where work runs").

## Goal

A public web app that collects published menus from top restaurants and breaks them into ingredients, preparations and combinations. Users can then search by ingredient and see how pairings vary by season and geography. Fine-dining menus rarely describe dishes plainly, so the value is in structured data at the ingredient level, not in the menus themselves.

This first slice proves the hard parts on about 30 restaurants before any web app is built:

- finding menus cheaply
- spotting when a menu changes
- extracting ingredients
- building history from the Wayback Machine
- answering one real analysis question in a notebook

## Decisions

| Topic | Decision |
|---|---|
| Product | Public. Only derived data is displayed. |
| Sources | Restaurant websites only. No Instagram, booking platforms or blogs. |
| Stack | Python 3.12 (uv), httpx, Playwright for JS-rendered sites, pymupdf, SQLAlchemy and Alembic |
| Database | Neon Postgres. Pooled `DATABASE_URL` for app traffic, `DATABASE_URL_UNPOOLED` for migrations. |
| Raw storage | Neon Object Storage, private bucket `snapshots` (S3 API, path-style) |
| Auth | Neon Auth is provisioned but unused until the web app exists |
| Scheduling | GitHub Actions cron, running `check` only, with no LLM |
| Extraction | In Claude Code sessions (subscription), not the API. No `ANTHROPIC_API_KEY`. |
| Where work runs | Cloud sessions have no Postgres access; they exchange work with Actions through the `snapshots` bucket (`pending/`, `extracted/`). |
| Repo | Code will be public. Data stays private (Neon) and never goes in the repo. |
| Scope now | Pipeline and analysis notebook, plus a static site (revised 2026-10-15, see "Website"). |

## Legal / content stance (not legal advice; get a review before public launch)

- **Facts vs. expression.** Facts such as "this menu uses uni, yuzu, brown butter" are generally not copyrightable. The menu wording, PDF layout and photos are.
- **Store** raw snapshots privately, only for re-extraction. They are never served.
- **Display** only derived data: ingredients, preparations, menu type, date window, restaurant and a link to the source. No verbatim course text in bulk, and no PDFs or images.
- **Respect site signals.**
  - robots.txt
  - TDM opt-outs (the `tdm-reservation` header or meta tag, and `/.well-known/tdmrep.json`)
  - low request rates
  - an honest User-Agent with a contact URL

  Opted-out sites are recorded and skipped.
- **Opt-out path.** Plan a takedown/opt-out process for restaurants before launch.
- **Public logs.** Actions logs are public, so log IDs, hashes and statuses only. Never log menu text or secrets.

## Where work runs

Cloud Claude Code sessions only allow HTTPS out, through a proxy. Object storage and Neon Auth work from there, but Postgres on port 5432 does not (checked 2026-10-15). Don't tunnel or relay Postgres through HTTPS: Claude Code's permission system blocks that as a containment escape. Instead, the private `snapshots` bucket is the handoff point:

| Who | Does what | Postgres? |
|---|---|---|
| **GitHub Actions** (scheduled) | `check`, then `pending --to-bucket` writes unextracted menus to `pending/`. `import --from-bucket` loads `extracted/` into Postgres. | Yes, direct |
| **Cloud session** | Reads `pending/`, extracts, writes JSON to `extracted/`. Also writes recipes and code, and runs the offline tests. | No |
| **Local machine** | Occasional manual work: migrations, ad-hoc queries, `smoke`. | Yes |

Extracted data reaches Postgres on the next Actions run (scheduled, or a manual `workflow_dispatch`). Raw menus and extraction output stay in the private bucket and never enter the repo. Neon's SQL-over-HTTPS API is the fallback if this round trip becomes a problem, at the cost of a second database code path.

## Website (started 2026-10-15)

Marcus revised "no web app yet" on 2026-10-15: start a front-end now, as a GitHub Pages site.

- **Static, client-side.** `site/` is plain HTML, CSS and JS with no build step. It loads one `data/data.json` and does search, filters (region, hemisphere-adjusted season, minimum restaurants) and charts in the browser. The Coverage tab is a world map of the seed list by menu status (Marcus asked for a map over a list, 2026-10-15). See `site/README.md`.
- **`menudb export-site --source synthetic|bucket|db`** (`src/menudb/site.py`) writes that JSON from the same `analysis.py` statistics as the notebook: ingredient shares overall, by region and by season; preparations per ingredient; pairs with lift/PMI seen at 2+ restaurants; coverage; restaurant name, city, region and website link. No course text and no per-course grouping, which would rebuild dishes.
- **`.github/workflows/site.yml`** builds the site from the synthetic fixture and uploads it as a workflow artifact. It does not deploy to Pages and never reads real data. `site/data/` is gitignored.
- **Not yet decided: publishing real data.** A Pages site is public and can't be taken back once crawled or archived. Real data would expose which seed restaurants publish menus, the full ingredient and preparation list for each one, and region/season shares that, with one or two restaurants per region, are close to that restaurant's own menu. The legal stance above asks for a review before public launch. Turning on Pages with real data waits on Marcus.

## Data model (see `src/menudb/db/models.py`)

```
restaurant ── list_membership (list, year, stars/rank)
    └── source (recipe JSON, check cadence, validators, robots/TDM status)
          └── snapshot (immutable fetch; raw bytes in the bucket under sha256)
menu (one per distinct normalized_hash per restaurant; first_seen/last_seen)
    └── course (position, raw_text [private], kind)
          └── component (ingredient_id, raw_term, role, preparations[])
ingredient (canonical, parent hierarchy, is_composite) + ingredient_alias
review_queue (unmatched terms awaiting mapping)
```

Each distinct menu counts once in the analysis. Seeing it again only moves `last_seen` forward.

## Recipes (stored navigation steps per restaurant)

```yaml
# data/recipes/<slug>.yaml  (a single recipe, or {label: recipe} for several menus)
start: https://restaurant.com/
steps:
  - follow: {text_matches: "(?i)menu|carte|tasting|meny"}
  - select: {css: "a[href$='.pdf']", pick: first}   # first | last | all
  - regex: {pattern: '"file":"([^"]+\.pdf)"', template: "/files/{1}"}  # URLs in JSON/JS bodies
render: static        # or js
content: html_main    # or pdf | image
```

For the first 30 restaurants, recipes are written by hand in sessions. An automated agent that repairs broken recipes comes later.

## Pipeline

**`menudb check`** (done). Runs on a schedule with no LLM.

1. Pick the sources that are due: `last_checked + check_interval <= now`.
2. **Tier 0:** walk the recipe and send a conditional GET for each final URL. A 304, or an unchanged normalized hash, means stop. Normalization strips scripts, nav, footer, cookie banners, dates, years, times and prices.
3. **Tier 1:** for a new hash, store the raw bytes in the bucket, then insert a `snapshot` and a `menu` row. This runs in a savepoint, so a failure records nothing.
4. **Failures** (recipe drift, HTTP errors, robots.txt) increment `consecutive_failures`, and the run summary lists the failing sources.
5. **Cadence adapts:** unchanged → interval ×1.5 (max 30 days); changed → interval ÷2 (min 3 days).

**Extraction** (built 2026-10-15; conventions in `docs/EXTRACTION.md`). Runs in a cloud session, not through the API, with the bucket as the handoff (see "Where work runs"). Objects are keyed `<prefix>/<slug>/<normalized_hash>.json`, the same identity as a menu row, so a document can be queued and extracted before `check` has recorded it.

- `menudb pending --to-bucket` (in Actions) writes one `PendingItem` per unextracted menu to `pending/`: slug, menu id, kind, URL, storage key, and the text (or none for image-only documents). `--from-recipes` walks the recipes instead and needs no database, for use before `check` has run.
- The session runs `menudb extract-io list/get/put`. Claude writes JSON matching the pydantic `ExtractedMenu` schema (`src/menudb/extract/schema.py`): courses, then components with raw term, canonical English name, role and preparations, plus menu type, language, price and stated period. `put` validates it, writes `extracted/`, and deletes the `pending/` object. No database needed.
- `menudb import --from-bucket` (in Actions, after `check`) validates each `extracted/` object, replaces the menu's courses and components, stamps `extraction_version` and the extractor name, then moves the object to `imported/`. An extraction whose menu row doesn't exist yet stays in `extracted/` until it does. `menudb import <json>` stays available for local use.
- `ingredients.py` matches terms against aliases, then canonical names. A new suggested name becomes an ingredient at once (with an alias for the raw term) and is logged in `review_queue` as `auto` for audit; terms with no suggestion wait as `open`. `menudb review` maps, accepts or ignores terms through the bucket (`review/queue.json` out, `review/decisions/` in), run from `check.yml`.
- **Automating later:** a scheduled Claude Code routine (`/schedule`), or `claude-code-action` with a subscription token.

**`menudb backfill [slug...]`** (built; runs in Actions `backfill.yml`)

- Uses the Wayback CDX API with `collapse=digest` and replays the recipe against `web.archive.org/web/<ts>id_/<url>`.
- Stores the results as `origin=wayback` snapshots.
- Rate-limited to about 1 request per second.

**GitHub Actions**: `.github/workflows/check.yml`

- Triggers: daily cron plus `workflow_dispatch`.
- Steps: uv, Playwright Chromium (cached), `menudb seed`, `menudb check`, `menudb review apply --from-bucket`, `menudb import --from-bucket`, `menudb pending --to-bucket`, `menudb review export --to-bucket`. Import runs after check so extractions made before their menu row existed can match.
- Secrets: `DATABASE_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`. Bulk-set them with `gh secret set -f <file>`.
- Scheduled workflows on public repos are disabled after 60 days without repo activity.

## Seed set (~30, in `data/seed/restaurants.csv`)

**Sources.** Draft from the official Michelin 3-star list and World's 50 Best. Verify each list status against the official source. One-off, low-volume fetches of the 50 Best list pages are fine (their robots.txt allows it). guide.michelin.com serves a bot challenge, so don't try to get past it: use Wikipedia's Michelin lists, then check by hand.

**Chosen for spread, not prestige:**

- **Regions:** Nordic, France, Spain, Italy, UK, US, Mexico/Peru, Japan, rest of East Asia, Southeast Asia, and Australia (for Southern Hemisphere coverage).
- **Formats:** HTML, PDF, JS-rendered, image-only and non-English menus.
- **Gaps:** a few restaurants known not to publish menus, so the coverage gap is measured.

The user reviews the list before recipes are written.

CSV columns: `slug,name,city,country,lat,lon,website,publishes_menu,lists`. `lists` takes the form `michelin:2026:3;w50b:2026:12`.

## Full list (built 2026-10-15)

`data/seed/restaurants_full.csv` (the 34-row `restaurants.csv` stays the test set). Built by `scripts/build_full_list.py` from:

- **Michelin:** Wikipedia's "List of Michelin-starred restaurants in ..." pages (80 pages; each row keeps the page revision and the guide year that page shows, since some pages lag the guide).
- **World's 50 Best** 1-100 (2025), from theworlds50best.com list pages.
- **NYT** "50 Best Restaurants in America" 2026: names from NYT's own shared Google Maps list (`data/seed/lists/nyt_america_2026.txt`). **NYC 100**: names and yearly ranks from a reader-kept shared Maps list (`data/seed/lists/nyt_nyc_100.txt`, 137 places, 91 ranked in 2026; the rest of the list didn't load without signing in). Unranked places are left out; lists entries look like `nytnyc:2026:2`. nytimes.com is not scraped (its robots.txt prohibits automated extraction).

Websites and coordinates come from the seed CSV, Wikidata (by article, then one query matching name and country), and OpenStreetMap Nominatim (1 request per second, per its usage policy). `coord_precision` is `exact`, `city` (city center, no exact match) or `country` (last resort). Extra columns `list_sources`, `website_source` and `coord_precision` are ignored by `seed`.

**Menu discovery** (`menudb discover`, `src/menudb/discover.py`) finds menus without hand recipes: generic link scoring plus a food-word count, with a headless Chromium retry for sites that need JavaScript. It writes recipes to `data/recipes_auto/`, statuses to `data/seed/discovery.csv`, and with `--to-bucket` stores the documents and queues them in `pending/`. `seed --auto-dir` (default `data/recipes_auto`) uses a generated recipe where no hand recipe exists. `check.yml` still seeds only the 34-restaurant test set.

First run (2026-10-15, cloud session): of 1,711 restaurants with a website, 493 `menu_found` and 214 `menu_maybe` (707 recipes; 706 documents stored and queued in `pending/`), 731 no menu found, 137 robots.txt disallow, 123 failed (HTTP errors, timeouts), 1 TDM opt-out. Robots refusals and failures include sites the cloud proxy can't reach; rerun those from Actions before treating them as opt-outs. 2,742 restaurants have no website yet.

## Analysis notebook (`notebooks/01_pairings.ipynb`)

- Ingredient frequency overall, by region, and by hemisphere-relative season (`seasons.py`).
- Course-level co-occurrence of ingredient pairs, ranked by lift/PMI with minimum-support thresholds. Each distinct menu counts once, and results are weighted per restaurant.
- A coverage table (published vs. not, by region) next to every chart, so the bias stays visible.

## Status / build order

1. ✅ Scaffold: uv project, config, Alembic schema (applied to Neon), storage, CLI (`seed`, `check`, `smoke`), tests.
2. ✅ Fetcher (robots/TDM/rate limit/conditional GET), normalization, recipe runner, `check` pipeline. 19 tests pass.
3. ✅ Seed CSV committed (34 restaurants). Recipes for 15 restaurants (20 sources) in `data/recipes/`, with coverage notes in `data/recipes/README.md`. `menudb probe` walks recipes without a database; on 2026-10-15 all sources resolved and their raw documents were stored in the bucket from a cloud session.
   ⏭ **Next:** add the Actions secrets and run `check.yml` once by hand. That records the menus, imports the 20 extractions already in `extracted/`, and queues anything new.
4. 🟡 Extraction via the bucket handoff: `pending` / `extract-io` / `import` built and tested offline. On 2026-10-15 a cloud session queued the 20 documents the recipes fetch (`pending --from-recipes`) and extracted all 20 into `extracted/`: 14 menus with dishes (213 courses, 766 components), 6 marked not-a-menu (landing pages without dishes, Brae's harvest list). A trial import into embedded Postgres matched every component. `menudb review` built. Still to do: the 5-menu hand-labeled evaluation (target ≥0.85 ingredient recall).
5. ✅ `wayback.py` / `menudb backfill` (Actions `backfill.yml`, manual). Monthly distinct captures of each recipe's start page, replayed through `id_` URLs at about 1 request per second; the restaurant's current robots.txt and TDM still apply; `render: js` sources are skipped. web.archive.org resets connections from cloud sessions, so it has only run in tests so far.
6. GitHub Actions: `.github/workflows/check.yml` exists (daily cron and `workflow_dispatch`: `seed`, `check`, `review apply`, `import --from-bucket`, `pending --to-bucket`, `review export`). Still to do: the repo secrets.
7. ✅ Notebook `notebooks/01_pairings.ipynb` with the statistics in `src/menudb/analysis.py`: coverage, restaurant-weighted ingredient frequency (overall, by region, by season), and course-level pair lift/PMI with a minimum-restaurants threshold. Reads Postgres, the bucket's `extracted/` (no database), or a synthetic fixture (`scripts/make_synthetic_menus.py`); it has run on the synthetic data and, with `MENUDB_ANALYSIS_SOURCE=bucket`, on the first 20 extractions (too few restaurants yet for pairings to mean much).
8. 🟡 Static site in `site/` (see "Website"): `menudb export-site` plus `site.yml`, which builds from synthetic data and doesn't deploy. Next: Marcus decides whether and when real data goes public on Pages.

## Verification

- **Tests:** `uv run pytest`. These run offline, and the DB tests use an embedded Postgres via pgserver.
- **Services:** `uv run menudb smoke` checks Postgres, object storage and Neon Auth end to end. In a cloud session the Postgres check fails after about 30 seconds (connect timeouts), and that is expected.
- **Pipeline:**
  - `menudb check --limit 3` creates snapshots and menus, and an immediate rerun reports 0 changed.
  - Backfill on a restaurant with PDF menus yields several distinct menus over time.
  - In Actions, a manual `workflow_dispatch` succeeds with no menu text in the logs.
