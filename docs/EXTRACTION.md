# Extracting menus in a session

Extraction runs in a Claude Code session, not through the API. The session needs object storage only, no Postgres. See "Where work runs" in `docs/PLAN.md` for the full round trip.

## Steps

```bash
uv run menudb extract-io list                         # keys, kind, text size
uv run menudb extract-io get <key> --out <scratch>    # pending JSON (+ raw file if no text layer)
# read it, write <scratch>/<name>.json matching the schema
uv run menudb extract-io put <scratch>/*.json         # validates, writes extracted/, deletes pending/
uv run menudb extract-io schema                       # the JSON schema
```

- Keep downloads and drafts in the session's scratchpad, never in the repo. They contain menu text.
- Don't paste menu text into replies, commits or logs. Report counts and slugs only.
- Image documents (`kind: image`, or a PDF with no text layer) come with the raw file. Convert WebP with Pillow if needed, then read the image.
- No queue yet because `check` hasn't run? `menudb pending --to-bucket --from-recipes` walks the recipes and queues what they fetch, with no database. Import waits until `check` records the same documents (same slug and normalized hash).

## Conventions (extraction_version 1)

**Menu level.**

- `is_menu: false` when the document has no dishes: a landing page, a booking blurb, a farm harvest list. Keep the price and currency if stated. Import records `menu_type = not_a_menu`.
- `menu_type`: `tasting`, `a_la_carte`, `vegetarian`, `lunch`, `prix_fixe_lunch`, `prix_fixe_dinner`, `kaiseki`, `event`.
- `language`: ISO 639-1 of the menu text. `price` as a number in the menu's `currency` (ISO 4217); for an à la carte menu with no set price, leave it empty.
- One document holding several menus (e.g. tasting + à la carte on one page): list each distinct dish once, so co-occurrence isn't double counted. Use the main menu's type and price.

**Courses.** One per dish, in menu order. `raw_text` is the dish's lines as written (private). Leave out wine pairings, supplements and allergy notes. `kind`: `snack`, `course`, `dessert`, `mignardise`, `drink`.

**Components.** One per ingredient named in the dish.

- `raw_term`: as written, without the producer or place when it is just a qualifier ("Cornish brill" is fine; the name is what matters).
- `name`: canonical English, singular, lowercase, specific enough to be useful but not a cultivar: `tomato` (not "Noire de Crimée"), `porcini` for cep, `beetroot`, `courgette flower`. Keep culturally specific ingredients as themselves (`nopal`, `huitlacoche`); a technique such as jeotgal goes in `preparations`.
- `role`: `main`, `sauce`, `garnish`, `seasoning`, `base` (pastry, rice, pasta, bread), `drink`, `other`.
- `preparations`: lowercase English verbs or techniques stated or plainly implied: `grilled`, `raw`, `smoked`, `pickled`, `fermented`, `confit`, `sorbet`, `custard`.
- Poetic dish titles with no ingredient ("La Luce") get no components. Don't guess.

## Import

`menudb import --from-bucket` (Actions) matches each component to an ingredient: an alias of the raw term first, then a canonical name equal to `name`. A new `name` becomes a new ingredient at once, and the term goes to `review_queue` with status `auto` so a human can audit it later. Terms with no `name` wait as `open`.

## Review

Review goes through the bucket the same way. `check.yml` runs `review apply --from-bucket` before import and `review export --to-bucket` at the end.

```bash
uv run menudb review pull --out <scratch>     # review/queue.json: term, language, status, suggested and current match
# write <scratch>/decisions.json: a JSON list of decisions
uv run menudb review push <scratch>/decisions.json   # validates, uploads to review/decisions/
```

Each decision has `raw_term` and `language` (as in the queue), and an `action`:

- `map` with `canonical`: point the term at that ingredient (created if new). Past components are relinked; an auto-created ingredient left unused is removed.
- `accept`: confirm the current match.
- `ignore`: not an ingredient; unlink it.

Optional on `map` and `accept`: `parent` (e.g. `porcini` under `mushroom`), `category` (vegetable, fruit, seafood, fish, meat, dairy, herb, spice, grain, nut, fungus, other), `is_composite` (dashi, XO sauce).

With Postgres (local or Actions), `menudb review export` prints the queue and `menudb review apply <file>` applies a local file.
