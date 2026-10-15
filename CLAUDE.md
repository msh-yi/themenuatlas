# themenuproject

Collects published fine-dining menus from restaurant websites and extracts ingredient-level data (ingredients, preparations, co-occurrence) for search and for analysis by season and geography.

**Read `docs/PLAN.md` first.** It holds the approved plan, the decisions behind it, and the current status and next step.

## Hard rules

- **Never display or commit raw menu content.**
  - Raw HTML and PDFs live only in the private Neon bucket `snapshots`.
  - `course.raw_text` is private.
  - The public product shows only derived data plus a link to the source.
- **Never log menu text or secrets.** GitHub Actions logs are public. Log IDs, hashes and statuses only.
- **Never commit `.env`, `.env.local` or `.neon`.**
- **Scrape politely.** Respect robots.txt and TDM opt-outs, keep the honest User-Agent, and don't lower the per-host `min_interval`.
- **No Anthropic API key.** Extraction happens in Claude Code sessions (see `docs/PLAN.md`), not through API calls.
- **Use the right database URL.** Migrations use `DATABASE_URL_UNPOOLED`; app traffic uses the pooled `DATABASE_URL`.

## Running

```bash
uv sync
uv run pytest                    # offline; DB tests use embedded Postgres (pgserver)
uv run menudb smoke              # Postgres + object storage + Neon Auth, end to end
uv run alembic upgrade head      # after adding a migration
uv run menudb seed               # data/seed/restaurants.csv + data/recipes/<slug>.yaml
uv run menudb check --limit 5
uv run menudb backfill [slug...]     # Wayback history; needs Postgres and web.archive.org (Actions/local)
uv run menudb probe [slug...]        # walk recipes with no database; --to-bucket stores raw files
uv run menudb extract-io list          # extraction in a session: list/get/put via the bucket (docs/EXTRACTION.md)
uv sync --group analysis && MENUDB_ANALYSIS_SOURCE=synthetic uv run jupyter nbconvert --to notebook --execute notebooks/01_pairings.ipynb --output /tmp/out.ipynb   # db | bucket | synthetic
uv run playwright install --with-deps chromium   # only when a recipe uses render: js
```

**Environment variables:**

- Neon values (`DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `AWS_*`, `NEON_AUTH_*`):
  - locally, they're in `.env.local` (from `neon env pull`);
  - in cloud sessions, they're set as environment variables.
- `CONTACT_URL` is optional.
- Commit `notebooks/` with outputs cleared; results from real data stay private.
- `CHROMIUM_PATH` is optional. In cloud sessions, set it to `/opt/pw-browsers/chromium` for `render: js` recipes.

Cloud sessions install dependencies through the SessionStart hook (`scripts/session-start.sh`).

**Cloud sessions can't reach Postgres** (HTTPS-only proxy; port 5432 is blocked). Object storage and Auth work. Don't try to tunnel Postgres; see "Where work runs" in `docs/PLAN.md`.

## Conventions

- **Schema changes:** edit `src/menudb/db/models.py`, then `alembic revision --autogenerate`, and check the generated migration.
- **Keep the pipeline testable offline.**
  - `Fetcher` accepts an httpx `transport` plus `sleep` and `clock` overrides.
  - `recipes.resolve` takes a `get_html` callable.
  - Pipeline tests monkeypatch `storage.put_raw`.
- **HTML parsing:** `selectolax.lexbor`. The old `selectolax.parser` backend was removed in 1.0.
- **Neon CLI:** needs Node ≥20.19. Neon skills are in `.claude/skills/`.
