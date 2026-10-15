# Ingredient reference data

`wikipedia.csv` links each canonical ingredient to its English Wikipedia article. It is written by `uv run menudb wiki-links` and edited by hand. The site links an ingredient only when its row is `ok` or `manual`.

| status | meaning | what to do |
|---|---|---|
| `ok` | Resolved to an article whose short description reads as food or an organism. | Spot-check. |
| `check` | Resolved, but the description doesn't say food (for example "uni" pointing at a university). | Set `title` and `wikidata` and change to `manual`, or set `none`. |
| `ambiguous` | The name is a disambiguation page. `candidates` lists any qualified titles found, such as `Lamb and mutton`. | Pick one and set `manual`, or `none`. |
| `missing` | No article under that name. | Set `manual` with the right title, or `none`. |
| `manual` | Set by hand. | Never overwritten. |
| `none` | No link wanted. | Never overwritten. |

Rerunning only looks up names that aren't in the file yet. `--retry` also redoes the `check`, `ambiguous` and `missing` rows. `--names-file` adds names from a file, one per line, such as an ingredient lexicon.

```bash
uv run menudb wiki-links --source bucket                  # names from the bucket's extractions
uv run menudb wiki-links --source synthetic --names-file <lexicon.txt>
```
