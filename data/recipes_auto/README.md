# Generated recipes

Written by `menudb discover` (`src/menudb/discover.py`) for restaurants in
`data/seed/restaurants_full.csv` that have no hand recipe in `data/recipes/`. Same format as hand
recipes; a hand recipe for the same slug always wins (`menudb seed` reads `data/recipes/` first).

How a recipe is chosen: from the home page, links that say "menu" in one of the supported
languages are scored, the best few are visited (one level deeper for PDFs), and each candidate
is rated by the number of distinct food words in its text. The best candidate becomes the recipe:
`menu_found` at 12 or more food words, `menu_maybe` at 5 to 11. Statuses for every restaurant,
including the ones with no recipe, are in `data/seed/discovery.csv`.

To fix a wrong pick, write a hand recipe in `data/recipes/<slug>.yaml`; don't edit files here,
since a later `menudb discover --redo` overwrites them.
