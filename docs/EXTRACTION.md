# Extracting menus

There are two extractors. Both write `ExtractedMenu` JSON to the bucket's `extracted/`, and `import --from-bucket` loads either.

- **Rule-based (`menudb extract-auto`)**: a script with no LLM, so it costs no session tokens and needs no API key. This is the default for bulk work.
- **In a Claude Code session (`menudb extract-io`)**: better on poetic, Japanese or oddly laid-out menus. Use it for documents the script handles badly, and for scans with no text layer.

## Rule-based extraction

```bash
uv run menudb extract-auto --min-stars 2 --dry-run   # counts only, writes nothing
uv run menudb extract-auto --min-stars 2             # or name slugs; no filter = all of pending/
uv run menudb extract-auto --rerun                   # after a lexicon change: redo earlier script output
uv run python scripts/eval_auto_extract.py build <scratch>/ref   # reference set from session extractions
uv run python scripts/eval_auto_extract.py score <scratch>/ref   # aggregate scores only
```

- It needs object storage only. It writes `extracted/` and moves each item from `pending/` to `processed/`, which keeps the text so `--rerun` can redo it. Scans with no text layer stay in `pending/` for a session.
- It never overwrites a session's extraction. `--rerun` only replaces output whose `extractor` starts with `lexicon`.
- Code: `src/menudb/extract/auto.py`. Vocabulary: `data/lexicon/ingredients.txt` (canonical English name, category, optional parent, terms in 12 languages) and `data/lexicon/preparations.txt`. Bump `EXTRACTOR` when the rules change enough to re-run.
- How it works:
  - It detects the language from stop words and script.
  - It drops boilerplate, wine pairings and drink sections, then joins continuation lines into dishes. A line ending in a comma, a line starting in lowercase, or a short title over its description all join the dish before. A divider (✳ or a price line) between every dish makes each block one dish.
  - It matches ingredients longest-first, with plural folding and German/Dutch/Scandinavian compound splitting. Each preparation attaches to the nearest ingredient in its comma segment.
  - Roles come from the segment (sauce words) and category (herbs and spices are seasoning, grains are base). The first segment is `main`.
- Only lexicon matches become components. Phrases it can't match are mostly prose fragments, so they never reach the site or the review queue. They go to the bucket as lexicon candidates: `review/candidates/<slug>/<hash>.json` per document, and `review/lexicon_candidates.json` with each phrase, its language and how many restaurants use it. Adding the common real ingredients to the lexicon, then `--rerun`, is how coverage grows.
- Known weak spots: poetic dish titles, Japanese/Chinese/Korean menus (the lexicon is thin there), and menu type and price on multi-menu pages. It is coarser than a session (`bluefin tuna belly` becomes `tuna`).
- Scores on the 19-menu reference set (2026-10-15): lenient ingredient recall 0.79, strict 0.64, precision 0.80, is_menu 18/19. The lexicon was partly written with those menus in view, so expect lower numbers on unseen menus. The hand-labelled eval is still the real test.
- Hand-labelled check (set up 2026-10-15): five script-extracted menus not used to build the lexicon (en, fr, de, it, es), on a private labelling page (claude.ai artifact "Extraction Check", answers in its database collection `labels`). The menus shown are stored privately as `review/hand-labels/pick.json` in the bucket. To score: read the `labels` collection into a scratch directory as `<slug>.json`, then `uv run python scripts/score_hand_labels.py <pick.json> <dir>` (prints rates only).
- Hand-labelled result (2026-10-16, Marcus, 5 menus / 63 courses): precision 0.99, recall 0.98; 10 courses marked "not a dish", mostly lone blanket terms.
- Blanket singletons and false friends (2026-10-16): a course whose only ingredient is in `data/lexicon/blanket.txt` (wine, tea, coffee, spirits, rosé, cheese, bread, pastry, fish, offal, ...) is dropped as a heading or drink line. `data/lexicon/stopwords.txt` lists words that fold or stem onto an ingredient but almost never are one (English "the" → thé, French "rue", "mais", Spanish "calle", the German suffix "-ment" → mint, ...); they never match alone or as a compound tail. Singletons of a specific ingredient ("Tuna", "Lamb") stay: in the hand labels they were real courses about as often as not, and they add nothing to pairings either way. On the 1,303 bucket documents: courses 31,514 → 26,992, singleton share 33.8% → 23.6%. Applied to the database by `check.yml` with `rerun` ticked (run 38851744987): courses 38,106 → 33,584, components 102,745 → 97,586, not-a-menu 274 → 281.
- Every course needs two ingredients, and names are canonical (2026-10-16, Marcus: "remove all singleton menu items", replacing the blanket list): `auto.tidy()` runs on every extraction at import, lexicon or session. It maps each name onto the lexicon's canonical one (Unicode and spacing normalized; "beef ribeye", "wagyū" → beef, "parmigiano reggiano" → parmesan) and drops courses with fewer than two distinct ingredients (a component still waiting for review counts). `extract-auto --rerun` re-queues session extractions so they go through it too. The site shows one alias chip per spelling (not both "ribeye" and "rib eye"). On the bucket documents: lexicon courses 26,992 → 20,627; session menus 213 → 194 courses, names outside the lexicon 109 → 17. Applied with `rerun` (run 38934124104): courses 33,584 → 27,219, components 97,586 → 91,221. Ingredient rows that earlier imports created for such variants keep catching menus through their aliases, so `menudb consolidate` (in `check.yml` after import) merges every ingredient the lexicon folds onto another into it (components, aliases and children move; the variant row goes), leaving ingredients chosen in review alone. First run (38939252418): 43 ingredients merged, 625 → 582.
- Oversized courses (2026-10-16). On the 16 Oct bucket build 135 courses had more than 15 ingredients (81 menus) and gave 21% of all within-course pairs. Causes, from the worst menus: (1) on a menu with dividers between most dishes, every block between two dividers was kept as one dish, including 10 to 27 line sections (romano, enoteca-pinchiorri, eipic, a-wong, thai-diner, schwarzwaldstube); (2) all-lowercase menus with a bare price ending each line chained every line as a continuation (the-bar-room-at-the-modern, state-bird-provisions); (3) Dutch prices ("24,-") left a trailing dash that read as a continuation (de-nederlanden). Fixes in `auto.py`: a divided block is kept whole only up to 4 lines; a line ending in a price, with or without a currency ("16", "24,-"), ends its dish; a trailing dash continues only as hyphenation ("Rinder-"). A first try that treated lowercase starts as style broke stacked-ingredient dishes and title-over-description layouts in the hand-labelled menus, so it was dropped; with the final rules the labelled menus are unchanged except one better grouping (animalon). Safety net: a course naming more than `MAX_DISH_INGREDIENTS` (15) ingredients gets kind `section` (extractor and `tidy()`), `analysis.pair_stats` skips it, and the site manifests mark it `"k": "section"`, which the site shows unnumbered and leaves out of course lists. On the 1,303 bucket documents: courses over 15 ingredients 135 → 17 (all flagged), over 10 339 → 126; share of pairs from courses over 15 21.2% → 2.1% before the skip, 0% after; menus 1,130 → 1,145, courses 20,627 → 21,654.

## Extracting in a session

Extraction can run in a Claude Code session, not through the API. The session needs object storage only, no Postgres. See "Where work runs" in `docs/PLAN.md` for the full round trip.

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

### Review page: new ingredients and lexicon candidates

Two kinds of term wait for a person: ingredients that import auto-created from session extractions (not in the lexicon), and phrases `extract-auto` couldn't match (`review/lexicon_candidates.json`, seen at 2+ restaurants). Both go through one list, in a session, with no database:

```bash
uv run menudb review build --out <scratch>        # items.json + ingredients.json; skips terms the lexicon knows or that were rejected
# decide each item on the private review page, or write <scratch>/choices.json by hand
uv run menudb review decide <scratch>/choices.json  # edits data/lexicon/ingredients.txt; queues alias decisions; remembers rejects
git commit data/lexicon/ingredients.txt             # the lexicon is in the repo
uv run menudb extract-auto --rerun                 # re-extract with the new terms; check.yml imports them
```

A choice is `{kind, term, language, action, canonical, category, parent}` with `action`:

- `approve`: a real ingredient, under `canonical` (new, or existing). A new one goes to the end of its `## category` section.
- `merge`: another name for the existing ingredient `canonical`; the term joins that line in its language.
- `reject`: not an ingredient. Candidates go to `review/lexicon_rejected.json` (bucket, private: they are menu phrases) and aren't offered again; auto-created ingredients become `ignore` decisions.

For auto-created ingredients, `decide` also uploads `map`/`ignore` decisions to `review/decisions/`, which `check.yml` applies. A canonical that is already another ingredient's term ("rum" is a name for spirits) is redirected to that ingredient; a term that already means something else, or is shorter than 3 letters, changes the database only, never the lexicon.

The review page is a private artifact (https://claude.ai/artifact/Gxtfy43RK33shFVPoX1NAx). It loads `items.json`/`ingredients.json` published with it and saves each choice in its database collection `decisions` (doc id = the item's `key`). To apply: `ArtifactData list decisions` with `out_dir`, turn the documents into a choices file, run `review decide`, commit, rerun, rebuild and republish the page's data files. Claude made the first pass on 2026-10-15 (1,954 items: 146 approved, 187 merged, 1,621 rejected, most from aggregator pages wrongly assigned to restaurants); the page shows those as applied, and choices there override them.
