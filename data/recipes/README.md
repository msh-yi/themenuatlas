# Recipes

One `<slug>.yaml` per restaurant in `data/seed/restaurants.csv` (format in `docs/PLAN.md`, "Recipes").
`menudb seed` loads them; `menudb probe [slug...]` walks them without a database and prints
kind, size and hash per document. In a cloud session, set `CHROMIUM_PATH=/opt/pw-browsers/chromium`
for `render: js` recipes.

## Coverage (checked 2026-10-15)

| Status | Restaurants |
|---|---|
| Recipe (18 sources) | arpege (PDF), atomix (image), brae, central, core, disfrutar (PDF), geranium, kitcho-arashiyama (Japanese), le-bernardin (JS, 4 menus), lenclume (2 menus), mirazur, osteria-francescana, quintonil, the-ledbury (JS) |
| No dish-level menu on the site | alchemist, attica, diverxo, frantzen, gaggan, maaemo, maido, odette, septime (carte blanche only), singlethread |
| Menu behind a JS download button | mingles |
| Not expected to publish (`publishes_menu=false`) | alinea, asador-etxebarri, den |
| Unreachable from cloud sessions (proxy) | sezanne, uliassi: retry from Actions |
| Refused our User-Agent from a cloud session | pujol (429), sorn (403), the-chairman (403): recheck once from Actions; if they still refuse, record as opted out |
| Website in the seed CSV looks wrong | lido-84 (lido84.com is a parked domain) |
