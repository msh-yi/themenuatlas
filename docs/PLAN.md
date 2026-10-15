# Plan: first slice (pipeline + analysis)

Approved 2026-10-14. Updated the same day: Neon replaces Cloudflare R2, and extraction runs in Claude Code sessions instead of through the Anthropic API.

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
| Repo | Code will be public. Data stays private (Neon) and never goes in the repo. |
| Scope now | Pipeline and analysis notebook. No web app yet. |

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

**Extraction** (to build). Runs in a session, not through the API.

- `menudb pending` exports unextracted menus: id, language, text, or a pointer to the PDF/image.
- Claude in the session produces JSON matching a pydantic `Menu` schema: courses, then components with raw term, suggested canonical English name, role and preparations, plus menu type, language and stated dates.
- `menudb import <json>` validates the JSON, writes courses and components, and stamps `extraction_version` and the extractor name.
- `ingredients.py` matches terms against aliases. Unmatched terms go to `review_queue`, and `menudb review` maps them.
- **Automating later:** a scheduled Claude Code routine (`/schedule`), or `claude-code-action` with a subscription token.

**`menudb backfill <slug>`** (to build)

- Uses the Wayback CDX API with `collapse=digest` and replays the recipe against `web.archive.org/web/<ts>id_/<url>`.
- Stores the results as `origin=wayback` snapshots.
- Rate-limited to about 1 request per second.

**GitHub Actions** (to build): `.github/workflows/check.yml`

- Triggers: daily cron plus `workflow_dispatch`.
- Steps: uv, Playwright Chromium (cached), `menudb check`.
- Secrets: `DATABASE_URL`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_ENDPOINT_URL_S3`, `AWS_REGION`. Bulk-set them with `gh secret set -f <file>`.
- Scheduled workflows on public repos are disabled after 60 days without repo activity.

## Seed set (~30, in `data/seed/restaurants.csv`)

**Sources.** Draft from the official Michelin 3-star list and World's 50 Best. Verify each list status against the official source, and don't scrape the guide sites.

**Chosen for spread, not prestige:**

- **Regions:** Nordic, France, Spain, Italy, UK, US, Mexico/Peru, Japan, rest of East Asia, Southeast Asia, and Australia (for Southern Hemisphere coverage).
- **Formats:** HTML, PDF, JS-rendered, image-only and non-English menus.
- **Gaps:** a few restaurants known not to publish menus, so the coverage gap is measured.

The user reviews the list before recipes are written.

CSV columns: `slug,name,city,country,lat,lon,website,publishes_menu,lists`. `lists` takes the form `michelin:2026:3;w50b:2026:12`.

## Analysis notebook (`notebooks/01_pairings.ipynb`)

- Ingredient frequency overall, by region, and by hemisphere-relative season (`seasons.py`).
- Course-level co-occurrence of ingredient pairs, ranked by lift/PMI with minimum-support thresholds. Each distinct menu counts once, and results are weighted per restaurant.
- A coverage table (published vs. not, by region) next to every chart, so the bias stays visible.

## Status / build order

1. ✅ Scaffold: uv project, config, Alembic schema (applied to Neon), storage, CLI (`seed`, `check`, `smoke`), tests.
2. ✅ Fetcher (robots/TDM/rate limit/conditional GET), normalization, recipe runner, `check` pipeline. 19 tests pass.
3. ⏭ **Next:** draft the seed CSV (user reviews it), write recipes for the 30, then `seed` and the first live `check`.
4. Extraction in sessions (`pending` / `import` / `review`), plus a 5-menu hand-labeled evaluation. Target ≥0.85 ingredient recall.
5. `wayback.py` backfill for all 30.
6. GitHub Actions workflow and secrets.
7. Notebook.

## Verification

- **Tests:** `uv run pytest`. These run offline, and the DB tests use an embedded Postgres via pgserver.
- **Services:** `uv run menudb smoke` checks Postgres, object storage and Neon Auth end to end.
- **Pipeline:**
  - `menudb check --limit 3` creates snapshots and menus, and an immediate rerun reports 0 changed.
  - Backfill on a restaurant with PDF menus yields several distinct menus over time.
  - In Actions, a manual `workflow_dispatch` succeeds with no menu text in the logs.
