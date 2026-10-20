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
| Extraction | Rule-based script (`extract-auto`) for bulk work; Claude Code sessions for hard menus. Never the API. No `ANTHROPIC_API_KEY`. |
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

  **Changed 2026-10-15 (Marcus):** robots.txt is ignored where `RESPECT_ROBOTS=false` is set (`discover-retry.yml`, `recheck.yml`), at most about once a month per site, with the honest User-Agent and per-host pacing. TDM opt-outs are still honoured. Bot protection (Cloudflare challenges, CAPTCHAs and the like) is labelled `bot_blocked` and not circumvented.
- **Opt-out path (2026-10-16).** A public GitHub issue form (`.github/ISSUE_TEMPLATE/opt-out.yml`, linked from the site's About page, footer and every restaurant page). To act on one: add the slug to `data/seed/optouts.csv` (slug, date, note); `export-site` then leaves the restaurant out entirely (name, menus, and every share, pair and preparation count), including menus collected later. Also remove its check source/recipe so its site isn't read again. `correction.yml` takes reports of wrong ingredients or matches.
- **Public logs.** Actions logs are public, so log IDs, hashes and statuses only. Never log menu text or secrets.

## Where work runs

Cloud Claude Code sessions only allow HTTPS out, through a proxy. Object storage and Neon Auth work from there, but Postgres on port 5432 does not (checked 2026-10-15). Don't tunnel or relay Postgres through HTTPS: Claude Code's permission system blocks that as a containment escape. Instead, the private `snapshots` bucket is the handoff point:

| Who | Does what | Postgres? |
|---|---|---|
| **GitHub Actions** (scheduled) | `check`, then `pending --to-bucket` writes unextracted menus to `pending/`, `extract-auto` extracts them with the lexicon, and `import --from-bucket` loads `extracted/` into Postgres. | Yes, direct |
| **Cloud session** | Reads `pending/`, extracts, writes JSON to `extracted/`. Also writes recipes and code, and runs the offline tests. | No |
| **Local machine** | Occasional manual work: migrations, ad-hoc queries, `smoke`. | Yes |

Extracted data reaches Postgres on the next Actions run (scheduled, or a manual `workflow_dispatch`). Raw menus and extraction output stay in the private bucket and never enter the repo. Neon's SQL-over-HTTPS API is the fallback if this round trip becomes a problem, at the cost of a second database code path.

## Website (started 2026-10-15)

Marcus revised "no web app yet" on 2026-10-15: start a front-end now, as a GitHub Pages site.

- **Static, client-side.** `site/` is plain HTML, CSS and JS with no build step. It loads one `data/data.json` and does search, filters (region, hemisphere-adjusted season, minimum restaurants) and charts in the browser. The Coverage tab is a world map of the seed list by menu status (Marcus asked for a map over a list, 2026-10-15). See `site/README.md`.
- **`menudb export-site --source synthetic|bucket|db`** (`src/menudb/site.py`) writes that JSON from the same `analysis.py` statistics as the notebook: ingredient shares overall, by region and by season; preparations per ingredient; pairs with lift/PMI seen at 2+ restaurants; coverage; restaurant name, city, region and website link. No course text; per-course manifests only with `--menus` (see "Menus on restaurant pages" below).
- **`.github/workflows/site.yml`** has two jobs. `synthetic` builds from the fixture and uploads a workflow artifact. `real` runs after each successful `check`, on pushes to `main` that touch the site, or by hand with `source: db`; it exports from Postgres (with `--menus`), stores the JSON in the bucket under `site/` (`data.json`, `pairs.json`, `menus/NN.json`; split so the first load is about 1.5 MB, 0.3 MB gzipped, at 4,554 restaurants), and `deploy` publishes `site/` with that data to GitHub Pages. `site/data/` stays gitignored: real data reaches Pages only through the workflow, never the repo.
- **Menus on restaurant pages (Marcus, 2026-10-15):** `export-site --menus` adds each restaurant's menus as manifests: courses in order, each with canonical ingredient names and preparations, plus menu type, price, language and first/last seen dates (dates only from the database). No menu wording and no raw files. This does rebuild the structure of each dish; Marcus chose to publish it with the rest (2026-10-16). Since 2026-10-16 an item also carries the name the menu used for it when that name is in the lexicon (e.g. beef as "wagyu"), for the course lists behind the ingredient tags; other wording is dropped in `site._lexicon_terms`.
- **Dates (2026-10-15):** each restaurant in the export carries `first_seen`/`last_seen` (its menus' earliest first-seen and latest last-seen dates, from the database; bucket-only builds approximate them from when documents were queued). Ingredient season shares still use each menu's first-seen date, so they only mean something once the dates span the year (the Wayback backfill, paused, would supply history).
- Coverage status per restaurant also uses menu discovery (`data/seed/discovery.csv`): found, none found, or the site opts out (robots/TDM), shown apart from "no menu".
- **Pairings and suggestions (2026-10-17, Marcus).** Trivial pairs are left out of the site's pairings (generic words, two names for one thing, an ingredient and its kind), and staples (bread, butter, pasta, ...) are hidden behind a toggle; the lists are in `data/lexicon/pairings.txt`. Visitors can suggest a restaurant's website or menu page through the issue form `website.yml`; it is applied only after a maintainer adds the `website-approved` label (`website-suggestion.yml`). Menu uploads were considered and dropped: a static site can't take files into the private bucket.
- **Publishing real data: decided (Marcus, 2026-10-16).** The repo goes public and the site deploys to GitHub Pages after every successful `check`, with menu manifests. Known trade-offs, accepted: a public site can't be taken back once crawled or archived; it shows which listed restaurants publish menus and each one's full ingredient and preparation list; with one or two restaurants in a region, region/season shares are close to that restaurant's own menu. The legal stance above still recommends a review. Opt-outs go through the issue form (see "Opt-out path").
- **Public-facing copy (2026-10-16).** The About page describes collection as it is now: restaurant websites only, a self-identifying crawler, TDM reservations honoured, bot protection never bypassed. It no longer says robots.txt is always honoured, since the monthly discovery runs don't (see above). It also states the site isn't affiliated with the list publishers and that the MICHELIN star symbol is Michelin's trademark.

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
- The session runs `menudb extract-io list/get/put`. Claude writes JSON matching the pydantic `ExtractedMenu` schema (`src/menudb/extract/schema.py`): courses, then components with raw term, canonical English name, role and preparations, plus menu type, language, price and stated period. `put` validates it, writes `extracted/`, and moves the `pending/` object to `processed/` (import needs it there to record menus no `check` has seen). No database needed.
- `menudb import --from-bucket` (in Actions, after `check`) validates each `extracted/` object, replaces the menu's courses and components, stamps `extraction_version` and the extractor name, then moves the object to `imported/`. An extraction whose menu row doesn't exist yet is matched to its queue item in `processed/` (or `pending/`): import creates the snapshot and menu rows from it, under a source with `mode = "bucket"` that `check` skips and `recheck.yml` keeps fetching. With no queue item it stays in `extracted/` until `check` records the document. `menudb import <json>` stays available for local use.
- `ingredients.py` matches terms against aliases, then canonical names. A new suggested name becomes an ingredient at once (with an alias for the raw term) and is logged in `review_queue` as `auto` for audit; terms with no suggestion wait as `open`. `menudb review` maps, accepts or ignores terms through the bucket (`review/queue.json` out, `review/decisions/` in), run from `check.yml`.
- **Automating later:** a scheduled Claude Code routine (`/schedule`), or `claude-code-action` with a subscription token.

**`menudb backfill [slug...]`** (built; runs in Actions `backfill.yml`)

- Uses the Wayback CDX API with `collapse=digest` and replays the recipe against `web.archive.org/web/<ts>id_/<url>`.
- Stores the results as `origin=wayback` snapshots.
- Rate-limited to one request every 5 seconds (`WAYBACK_INTERVAL`; at 1 per second the archive started refusing connections).

**GitHub Actions**: `.github/workflows/check.yml`

- Triggers: daily cron plus `workflow_dispatch`.
- Steps: uv, Playwright Chromium (cached), `alembic upgrade head`, `menudb seed` (full list as restaurants only, then the curated list with recipes), `menudb check`, `menudb review apply --from-bucket`, `menudb import --from-bucket`, `menudb pending --to-bucket`, `menudb extract-auto`, `menudb import --from-bucket` again, `menudb consolidate`, `menudb review export --to-bucket`, `menudb stats`. Import runs after check so extractions made before their menu row existed can match.
- `menudb stats` (last step) adds a **Data quality** table to the run summary (`src/menudb/quality.py`): documents and menu URLs shared by several restaurants, website hosts shared by several restaurants or on news/guide/directory hosts (`src/menudb/domains.py`, also used by `find_websites.py`), extracted menus with no ingredients, restaurants whose courses mostly match nothing, duplicate menus (same dishes under different hashes), and sources failing 3+ times. Counts with a few slugs or hosts as examples; excluded menus are left out.
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

**Menu discovery** (`menudb discover`, `src/menudb/discover.py`) finds menus without hand recipes: generic link scoring plus a food-word count, with a headless Chromium retry for sites that need JavaScript. It writes recipes to `data/recipes_auto/`, statuses to `data/seed/discovery.csv`, and with `--to-bucket` stores the documents and queues them in `pending/`. `seed --auto-dir` (default `data/recipes_auto`) uses a generated recipe where no hand recipe exists. `check.yml` seeds every restaurant on the full list, but creates check sources only for the 34-restaurant test set, and runs daily. `recheck.yml` (weekly, Mondays, no database) re-walks one quarter of `data/recipes_auto/` with `pending --from-recipes --shard i/4` (shard = ISO week mod 4, stable by slug), so each auto-discovered site is checked about once a month; new documents are stored and queued in `pending/`.

First run (2026-10-15, cloud session): of 1,711 restaurants with a website, 493 `menu_found` and 214 `menu_maybe` (707 recipes; 706 documents stored and queued in `pending/`), 731 no menu found, 137 robots.txt disallow, 123 failed (HTTP errors, timeouts), 1 TDM opt-out. Robots refusals and failures include sites the cloud proxy can't reach; rerun those from Actions before treating them as opt-outs. 2,742 restaurants have no website yet.

After the Actions reruns, the Chromium pass and the first LangSearch run (2026-10-15 evening): 2,451 of 4,550 restaurants have a website; 720 `menu_found` + 311 `menu_maybe` (1,029 documents stored and queued), 1,217 no menu found, 89 `bot_blocked` (80 Cloudflare, 8 CAPTCHA, 1 DataDome; not circumvented), 95 failed (dead domains, certificate errors, timeouts). The first LangSearch run's websites were wrong: 692 of 693 were articles, directories or unrelated sites (e.g. one elitetraveler article matched 27 restaurants), which put shared, unrelated menus into the pairings. Since 2026-10-15 a search result counts only if its domain itself carries the restaurant's name, it isn't a news, guide, directory or blog host, and no other restaurant has that host (`official()` in `scripts/find_websites.py`); every run re-validates existing search-sourced websites first. The 692 rejected ones are in `data/seed/rejected_websites.csv` (website cleared, `website_source=search_rejected`, discovery status and auto recipe removed). `exclude.yml` (manual, counts first, `--undo` reverses) sets `menu.excluded_reason` on their menus, which analysis, the site, stats and the extraction queue skip, and moves their bucket items under `quarantine/`. Hand-picked exclusions go in `data/seed/exclusions.csv` (slug, optional normalized_hash for one menu, reason, wrong website to clear, note); the first batch (2026-10-15) covers documents shared between sister restaurants or duplicate listings (one copy kept) and websites on aggregator or tourism hosts. Websites shared by hotel or chef groups (Mandarin Oriental, Four Seasons, Gordon Ramsay, ...) are legitimate and stay. Under the strict rule LangSearch finds an official site for only ~2% of restaurants, so `find-websites.yml` is manual now; Brave (`BRAVE_SEARCH_API_KEY`) is the alternative if more websites are wanted. The full Brave run (2026-10-16, run 38828866520, ~1,450 new queries) added 671 official websites (2,099 → 2,770 of 4,550; a 25-site spot check found no wrong matches); discovery on them gave 259 `menu_found`, 81 `menu_maybe`, 279 no menu, 22 `bot_blocked`, 20 robots, 10 failed. Totals now: 938 `menu_found`, 352 `menu_maybe`, 1,275 no menu, 45 `bot_blocked`, 38 robots, 109 failed. Every restaurant without a website has now been searched with Brave; the remaining 1,780 found no official site. A second pass (`find_websites.py --local`, the `local` box in `find-websites.yml`, 2026-10-17) searches them again within each restaurant's country and language, compares Japanese names with domains across romanisation spellings, and uses the restaurant's name in its own script from Wikidata where known (also accepting a homepage whose title carries it); its matches are `website_source=search_local` and listed in the run summary for review. It found 36 (1,744 left). For the rest, `scripts/find_websites_browser.py` runs on Marcus's machine: it searches Google (or DuckDuckGo with `--engine ddg`) in his own visible Chrome (DrissionPage), a query every 10 to 20 seconds in the restaurant's region, with the same matching rules; it stops at the engine's bot check instead of getting past it, and never opens restaurant sites (`menudb discover` does that). Matches are `website_source=search_browser`, listed in `<cache>/browser_matches.csv`. Its first full run (2026-10-18) set 1,510 websites, but a spot check found that most pages *inside* other sites were directory listings (mishumap.com, diningcity.cn, speisekarte.menu, ...). Since then a page inside another site counts only on a hotel or resort host; `scripts/website_review.py triage` moved the other 714 to `data/seed/website_review.csv` (`website_source=browser_review`) for a person to mark `approve=y` and `apply`. Kept: 695 homepages and 101 hotel pages. For restaurants whose discovery finds no menu, `scripts/find_menus_browser.py` reruns discovery on Marcus's machine in a visible Chrome (DrissionPage) with the crawler's User-Agent, robots.txt, TDM and 1 s per page (5 s until 2026-10-20), following up to 8 links and trying /menu, /menus, /carte, /en/menu; it writes recipes and statuses like `menudb discover` and, with `--to-bucket`, queues the menus. `check.yml` (run 38839039729, 2026-10-16) then extracted and imported 494 new menus (2 left waiting): the database now has 1,560 menus from 1,297 restaurants (274 marked not-a-menu, 239 excluded), 38,106 courses, 102,745 components; the quality checks show no menus without ingredients, no duplicates and no aggregator hosts.

## Analysis notebook (`notebooks/01_pairings.ipynb`)

- Ingredient frequency overall, by region, and by hemisphere-relative season (`seasons.py`).
- Course-level co-occurrence of ingredient pairs, ranked by lift/PMI with minimum-support thresholds. Each distinct menu counts once, and results are weighted per restaurant.
- A coverage table (published vs. not, by region) next to every chart, so the bias stays visible.

## Status / build order

1. ✅ Scaffold: uv project, config, Alembic schema (applied to Neon), storage, CLI (`seed`, `check`, `smoke`), tests.
2. ✅ Fetcher (robots/TDM/rate limit/conditional GET), normalization, recipe runner, `check` pipeline. 19 tests pass.
3. ✅ Seed CSV committed (34 restaurants). Recipes for 15 restaurants (20 sources) in `data/recipes/`, with coverage notes in `data/recipes/README.md`. `menudb probe` walks recipes without a database; on 2026-10-15 all sources resolved and their raw documents were stored in the bucket from a cloud session.
4. ✅ Extraction via the bucket handoff: `pending` / `extract-io` / `import` built and tested offline. On 2026-10-15 a cloud session queued the 20 documents the recipes fetch (`pending --from-recipes`) and extracted all 20 into `extracted/`: 14 menus with dishes (213 courses, 766 components), 6 marked not-a-menu (landing pages without dishes, Brae's harvest list). A trial import into embedded Postgres matched every component. `menudb review` built. The 5-menu hand-labelled evaluation (target ≥0.85 ingredient recall) scored precision 0.99, recall 0.98 on 2026-10-16 (`docs/EXTRACTION.md`).
   On 2026-10-15 Marcus asked for bulk extraction by script, not agent tokens. `menudb extract-auto` (rule-based, multilingual lexicon in `data/lexicon/`) scored lenient recall 0.79 and precision 0.80 on the 19 session extractions. It ran on the 2 and 3 star restaurants: 148 documents, 127 menus with dishes, 3,342 courses, Marcus then said to run it on everything (738 documents). Unmatched phrases go to `review/lexicon_candidates.json`, not to the review queue (lexicon-2). `check.yml` now runs `extract-auto` and a second import, so new menus are extracted daily.
5. ✅ `wayback.py` / `menudb backfill` (Actions `backfill.yml`, manual). Monthly distinct captures of each recipe's start page, replayed through `id_` URLs at one request every 5 seconds; the restaurant's current robots.txt and TDM still apply; `render: js` sources are skipped. web.archive.org resets connections from cloud sessions, so it runs from Actions only (paused: the first run, 2026-10-15, was refused after about 70 requests at 1 per second).
6. ✅ GitHub Actions: secrets set; `check.yml` runs daily (steps under "GitHub Actions" above), with `recheck.yml`, `site.yml`, `test.yml` and the manual `backfill`, `discover-retry`, `find-websites` and `exclude` workflows.
7. ✅ Notebook `notebooks/01_pairings.ipynb` with the statistics in `src/menudb/analysis.py`: coverage, restaurant-weighted ingredient frequency (overall, by region, by season), and course-level pair lift/PMI with a minimum-restaurants threshold. Reads Postgres, the bucket's `extracted/` (no database), or a synthetic fixture (`scripts/make_synthetic_menus.py`); it has run on the synthetic data and, with `MENUDB_ANALYSIS_SOURCE=bucket`, on the first 20 extractions (too few restaurants yet for pairings to mean much).
8. ✅ Static site in `site/` (see "Website"): `menudb export-site` plus `site.yml`, which deploys real data to GitHub Pages after each successful `check`; live at https://msh-yi.github.io/themenuatlas/. The issue forms tag reports `opt-out` and `correction`; those labels must exist in the repo for the tags to apply.

## Verification

- **Tests:** `uv run pytest`. These run offline, and the DB tests use an embedded Postgres via pgserver.
- **Services:** `uv run menudb smoke` checks Postgres, object storage and Neon Auth end to end. In a cloud session the Postgres check fails after about 30 seconds (connect timeouts), and that is expected.
- **Pipeline:**
  - `menudb check --limit 3` creates snapshots and menus, and an immediate rerun reports 0 changed.
  - Backfill on a restaurant with PDF menus yields several distinct menus over time.
  - In Actions, a manual `workflow_dispatch` succeeds with no menu text in the logs.
