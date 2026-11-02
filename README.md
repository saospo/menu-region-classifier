# menu-region-classifier

*Regional Chinese Map: what a "Chinese" restaurant's menu actually serves.*

**On Google Maps, every one of these restaurants is just "Chinese." This project reads each menu and works out which regional cuisine it actually serves**: Yunnan, Fujian, Dongbei, Shaanxi, Hunan and about 20 more. It shows the evidence for every call and puts the results on a map.

It uses [Nimble](https://www.nimbleway.com/)'s live web search to find restaurants and their menus. The region taxonomy comes from Chinese Cooking Demystified's [*63 Chinese Cuisines: the Complete Guide*](https://chinesecookingdemystified.substack.com/p/63-chinese-cuisines-the-complete).

> **v0.1.0**: first release. The classifier, pipeline and map work end to end and pass 52 offline tests. Real-world accuracy so far is measured on a small hand-labeled NYC gold set. See [Limitations](#limitations).

## How it works

```
gold_set.json ─┐
               ├─► discover.py ─► data/restaurants.json ─► menus.py ─► data/menus/*.txt
Nimble search ─┘   (dish-driven search)                  (Nimble)
                                                                          │
taxonomy.json ─► match.py ─► adjudicate.py (optional LLM) ─► validate.py ─┴─► pipeline.py ─► data/results.json ─► build_map.py ─► site/index.html
```

1. **Find restaurants by dish, not by "Chinese restaurant."** Searching for 过桥米线 (crossing-the-bridge noodles) or 肉夹馍 (roujiamo) turns up regional places that call themselves generic.
2. **Fetch each menu** with Nimble Search (full page content), preferring the restaurant's own site, then delivery sites.
3. **Match the menu against the taxonomy, not the name.** The restaurant's own name is removed from the menu's titles and headers first, so it can't count as evidence. Chinese dish names count most, then English and pinyin aliases, then ingredients. Dishes found in only one region count more than dishes found everywhere: kung pao chicken is weak evidence, 稀豆粉 (xidoufen) is strong.
4. **Ask an LLM only when the matcher isn't sure.** This is optional and supports Claude, Gemini or OpenAI.
5. **Validate.** Does the restaurant's own name agree with the predicted region (云之南 = "south of the clouds" = Yunnan)? How does the result compare with the hand-labeled gold set?
6. **Map it.** The output is one static HTML page, with a card for each restaurant showing the menu lines that matched and the CCD dishes they matched.

See [SPEC.md](SPEC.md) for the full design.

## Setup

Python 3.11+.

```bash
pip install -r requirements.txt        # core + all three LLM SDKs; drop the ones you won't use
python test.py                          # 52 offline tests, no keys or network needed
```

Keys (only the ones you use):

| Variable | For |
|---|---|
| `NIMBLE_API_KEY` | Nimble search and extract |
| `NIMBLE_BUDGET` | **Required for live Nimble calls.** The total number of live calls allowed across all runs, e.g. `20`, or `unlimited` |
| `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` | LLM providers (any one is enough) |
| `LLM_PROVIDERS` | Fallback order, e.g. `openai,anthropic,gemini` (default `anthropic,gemini,openai`) |
| `ANTHROPIC_MODEL`, `GEMINI_MODEL`, `OPENAI_MODEL` | Override the default models; check them against your account |

## Running on a small Nimble budget

Every live Nimble call (retries included) is recorded in `data/nimble_usage.json`. A call that would exceed `NIMBLE_BUDGET` is refused before anything is sent. Everything is cached, so re-running any step is free. Check usage at any time:

```bash
python nimble_client.py usage
```

This plan fits in **20 calls** and leaves headroom:

| Step | Command | Nimble calls |
|---|---|---|
| 0 | `python test.py` | 0 |
| 1. Seed the gold set | `python discover.py --city "New York" --from-gold` | 0 |
| 2. One menu, then inspect | `python menus.py --city "New York" --search-only --limit 1` | 1 |
| | Look at `data/menus/index.json` and the `.txt` file before going on | |
| 3. The other gold menus | `python menus.py --city "New York" --search-only` | 3 |
| 4. Classify + map | `python pipeline.py && python build_map.py` | 0 |
| 5. Discover one area | `python discover.py --city "New York" --area "Flushing, Queens" --regions yunnan --dishes-per-region 1` (run with `--dry-run` first) | 3 |
| 6. Menus for what was found | `python menus.py --city "New York" --search-only --limit 8` | ≤ 8 |
| 7. Classify + map again | `python pipeline.py && python build_map.py` | 0 |
| | **Total** | **≤ 15**, leaving 5 for retries |

Set `NIMBLE_BUDGET=20` once. The ledger keeps counting across runs, so the cap holds for the whole plan.

- `--search-only` makes each menu cost exactly one call, skipping the Extract fallback on the restaurant's own site.
- `menus.py` fetches gold-set restaurants first, then those found by dish or region searches, and generic sweep results last.
- A failed search isn't recorded, so the next run retries it. A budget stop isn't recorded as "no menu".
- Discovery adds to `data/restaurants.json` and never removes anything: the gold restaurants from step 1 stay, with the same ids, so their menus from steps 2–3 stay linked.

**Make the gold set more convincing before step 2:** add a few non-Yunnan restaurants you know (a Shaanxi, Fujian or Dongbei place, and a plain takeout spot) to `gold_set.json`. Each costs one call in step 3, and they show the classifier telling regions apart, not just recognizing one.

## Outputs

- `data/results.json`: per restaurant, the region label, where the label came from (matcher or LLM), confidence, specialist flag, the matched menu lines with their CCD dish and section, and the self-label check. It also holds the gold-set score.
- `site/index.html`: the map. Open it in a browser or host it anywhere, for example GitHub Pages. It loads Leaflet from cdnjs and the map tiles from CARTO/OpenStreetMap.

`data/` is git-ignored: it holds third-party page content and your API caches.

## Project layout

| File | Role |
|---|---|
| `taxonomy_src.json` → `build_taxonomy.py` → `taxonomy.json` | Hand-edited source → generated region fingerprints |
| `taxonomy_interface.py` | The only reader of `taxonomy.json`; output labels; restaurant-name lookup |
| `text.py` | The only text normalization and matching rules (traditional → simplified via opencc, name stripping) |
| `paths.py` | Every data location (`DATA_DIR` moves them all), JSON read/write, the shared disk cache |
| `nimble_client.py` | The only Nimble caller: cache, retries, budget |
| `llm.py` | The only LLM caller: Claude / Gemini / OpenAI, cache, fallback |
| `discover.py`, `menus.py` | Find restaurants, fetch menus |
| `match.py`, `adjudicate.py`, `validate.py` | Classify, second opinion, checks |
| `pipeline.py`, `build_map.py` | Run everything, render the map |
| `test.py`, `test_menus/`, `gold_set.json` | Offline tests, sample menus, hand labels |
| `SPEC.md`, `notes.md` | Design spec; roadmap and open ideas |

## Limitations

- **Tested on NYC only**, and accuracy is measured on a small gold set.
- **English-only menus** depend on the alias lists in `taxonomy_src.json`. Add spellings as you see real menus, then run `python build_taxonomy.py`.
- **PDF and image menus are skipped.** Some of the most interesting regional places only have photo menus.
- **Nimble location results:** Nimble doesn't document their place fields, so addresses may come only from the pages found. `discover.py --geocode` fills coordinates from OpenStreetMap when there's an address.
- **The default LLM model names** may not match what your account offers. Set `*_MODEL` explicitly.
- **Region, not sub-cuisine.** Sub-cuisines (e.g. Dai, Han Yunnan) are shown as context, not predicted.

## License

Code: [MIT](LICENSE). The CCD guide's text and photos are not included; see Credits.

## Credits

- Region and dish taxonomy: built from [Chinese Cooking Demystified](https://chinesecookingdemystified.substack.com/)'s *63 Chinese Cuisines* guide. This project only uses dish names and regional groupings; read the guide itself for the food. This project is not affiliated with Chinese Cooking Demystified.
- Web search: [Nimble](https://www.nimbleway.com/).
- Map: [Leaflet](https://leafletjs.com/), tiles © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors © [CARTO](https://carto.com/attributions).
