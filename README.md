# themenuproject

A repository of published fine-dining menus, broken down into ingredients,
preparations and combinations so they can be searched and analysed across
season and geography.

Only derived data (ingredients, preparations, dates, links to the source) is
ever shown publicly. Raw pages and PDFs are kept privately for re-extraction.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and, for Neon config, Node ≥ 20.19.

```bash
uv sync
neon env pull                    # writes DATABASE_URL, AWS_* (object storage), NEON_AUTH_* to .env.local
uv run alembic upgrade head      # uses DATABASE_URL_UNPOOLED
uv run menudb smoke              # checks Postgres, object storage and Neon Auth end to end
uv run playwright install chromium   # only needed for JS-rendered sites
```

Add `ANTHROPIC_API_KEY` to `.env.local` for menu extraction.

## Usage

```bash
uv run menudb seed               # data/seed/restaurants.csv + data/recipes/<slug>.yaml
uv run menudb check --limit 10   # fetch due sources; store new menus
uv run pytest                    # offline tests; DB tests use an embedded Postgres
```

## Layout

- `src/menudb/fetch/` polite HTTP (robots.txt, TDM opt-out, rate limits, conditional GET) and headless Chromium
- `src/menudb/recipes.py` per-restaurant navigation recipes (start URL → menu documents)
- `src/menudb/normalize.py` boilerplate removal and change-detection hashing
- `src/menudb/pipeline.py` the `check` loop
- `src/menudb/db/` SQLAlchemy models and Alembic migrations
- `neon.ts` Neon services (Postgres, Auth, `snapshots` bucket)
