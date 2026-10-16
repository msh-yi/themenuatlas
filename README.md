# The Menu Atlas

**What the world's best restaurants are cooking.**

The Menu Atlas reads the tasting menus that fine-dining restaurants publish on their own websites and turns them into ingredient data: which ingredients appear, how they're prepared, which ones chefs pair, and how that varies by region and season.

**Site:** https://msh-yi.github.io/themenuatlas/

## On the site

- **Ingredients:** how often each one is on the menu, where, in which season, and how it's prepared.
- **Restaurants:** search by name, city, region or accolade, and see each menu course by course, as ingredients.
- **Pairings:** ingredients that share a plate more often than chance.
- **Map:** every restaurant with a menu, on a world map.

## How it works

1. **Find.** A list of acclaimed restaurants (Michelin-starred, World's 50 Best and others) is matched to each restaurant's own website.
2. **Collect.** A crawler visits those sites slowly and saves the published menu privately.
3. **Extract.** Each menu is broken into ingredients and preparations by a rule-based program.
4. **Publish.** Only the derived facts go on the site, with a link back to the restaurant.

## Running it yourself

You need [uv](https://docs.astral.sh/uv/). The site preview and the tests run offline on invented data:

```bash
uv sync
uv run pytest                              # offline tests
uv run menudb export-site                  # invented test data -> site/data/
cd site && python3 -m http.server 8000     # open http://localhost:8000
```

The full pipeline needs a Neon project (Postgres and object storage); see [`CLAUDE.md`](CLAUDE.md) for the commands and [`docs/PLAN.md`](docs/PLAN.md) for the design.

## For restaurants

**Want to be removed?** [Open a removal request](https://github.com/msh-yi/themenuatlas/issues/new?template=opt-out.yml). We take out the restaurant, its menus and every figure built from them, usually within a few days, and stop visiting its website.

**Spotted a mistake?** [Report it here](https://github.com/msh-yi/themenuatlas/issues/new?template=correction.yml).

**About our crawler.** It identifies itself as `themenuproject/0.1 (+https://github.com/msh-yi/themenuatlas)`, reads pages slowly, one at a time, and never bypasses CAPTCHAs, logins or bot protection. It honours text-and-data-mining reservations ([TDM Reservation Protocol](https://www.w3.org/community/tdmrep/)).

## Legal

**What we publish.** The site shows facts derived from menus (ingredient names and preparation methods in course order, the menu's type, price and language, counts, and dates first and last seen), along with each restaurant's name, location, published accolades and a link to its website. It never publishes the menu's own wording: no dish names or descriptions, PDFs, photos or other creative content. Original menus are kept privately, only to re-check the extraction, and are never shared.

**Ownership.** Menus and all content on restaurants' websites belong to their owners. Restaurant names and trademarks belong to their owners. The Menu Atlas isn't affiliated with, endorsed by or sponsored by any restaurant, the MICHELIN Guide, The World's 50 Best Restaurants or The New York Times. "MICHELIN" and the MICHELIN star symbol are trademarks of Michelin, used here only to describe a restaurant's rating.

**Accuracy.** Data is extracted automatically and may be incomplete, out of date or wrong. Menus change, so check with the restaurant before relying on anything here, especially for allergies or dietary needs. The site and data are provided "as is", without warranty of any kind.

**Removal.** Restaurants can ask to be removed at any time (see [For restaurants](#for-restaurants)). For other concerns, including copyright, [open an issue](https://github.com/msh-yi/themenuatlas/issues/new).

**Licences.**
- **Code:** [MIT](LICENSE).
- **Data:** the MIT licence covers the code only. The menu-derived data on the site isn't released under an open licence.
- **Third-party data:** country outlines are from [Natural Earth](https://www.naturalearthdata.com/) (public domain); ingredient links go to Wikipedia.

This section is a plain-language description of how the project works, not legal advice.
