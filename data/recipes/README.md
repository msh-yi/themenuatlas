# Recipes

One `<slug>.yaml` per restaurant in `data/seed/restaurants.csv` (format in `docs/PLAN.md`, "Recipes").
`menudb seed` loads them; `menudb probe [slug...]` walks them without a database and prints
kind, size and hash per document. In a cloud session, set `CHROMIUM_PATH=/opt/pw-browsers/chromium`
for `render: js` recipes.

## Coverage (checked 2026-10-15)

| Status | Restaurants |
|---|---|
| Recipe (20 sources) | arpege (PDF), atomix (image), brae, central, core, disfrutar (PDF), geranium, kitcho-arashiyama (Japanese), le-bernardin (JS, 4 menus), lenclume (2 menus), mingles (PDFs listed in JSON), mirazur, osteria-francescana, quintonil, the-ledbury (JS) |
| No dish-level menu on the site (rechecked rendered pages and their network requests) | alchemist, attica, diverxo, gaggan, maaemo, maido, odette, septime (carte blanche only), singlethread |
| Bot challenge (reCAPTCHA) for headless browsers; not worked around | frantzen |
| Not expected to publish (`publishes_menu=false`) | alinea, asador-etxebarri, den |
| Unreachable from cloud sessions (proxy) | sezanne, uliassi: retry from Actions |
| Refused our User-Agent from a cloud session | pujol (429), sorn (403), the-chairman (403): recheck once from Actions; if they still refuse, record as opted out |
| Closed | lido-84: website corrected to ristorantelido84.com, which says "Lido 84 is currently closed" (third-party sources say the last service was March 2026). Candidate for Wayback history only, or replacement. |
