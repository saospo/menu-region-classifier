# menu-region-classifier — Technical Spec

(The map page it produces is titled "Regional Chinese Map".)

**Status:** v0.0.9. See README.md for setup and the budget-aware run plan.

**One-liner:** Find Chinese restaurants in a city, classify each one into a region using its menu, put them on a map, and show the evidence for every call.

**Pitch:** On Google Maps, every restaurant is just "Chinese." Chinese Cooking Demystified counts 63 distinct cuisines. We use live web data from Nimble to read each menu and work out which one a restaurant actually serves, so you can find the Yunnan or Guizhou spot hiding behind a generic name.

**Taxonomy source:** [63 Chinese Cuisines: the Complete Guide](https://chinesecookingdemystified.substack.com/p/63-chinese-cuisines-the-complete), by Chinese Cooking Demystified. The UI links to it rather than reproducing its text. Accuracy is measured against the hand-labeled gold set (§7).

---

## 1. Architecture

```
[taxonomy.json] ──────────────────────────────┐
                                              ▼
 city ─► DISCOVER ─► restaurants.json ─► MENUS ─► menus/*.txt ─► CLASSIFY ─► VALIDATE ─► results.json ─► MAP
       (Nimble search,                 (Nimble search       (matcher +          (self-label,                 (static
        dish-driven)                    + full_content)      LLM adjudicator)    gold set)                    page)
```

Every stage reads and writes JSON on disk. Any stage can be rerun or cached on its own, and the LLM step can use Claude, Gemini or OpenAI, in a configurable fallback order (`LLM_PROVIDERS`).

**Stack:** Python 3.11, `requests` (Nimble REST API), `rapidfuzz`, `opencc-python-reimplemented` (traditional to simplified Chinese), `anthropic`, `google-genai` and/or `openai`, and a static Leaflet page for the UI (no server). There's no database; plain JSON files are enough.

```
(repository root)
  README.md, CHANGELOG.md, requirements.txt, SPEC.md, notes.md
  taxonomy_src.json    # hand-edited source: sub-cuisines and which region each belongs to
  build_taxonomy.py    # taxonomy_src.json → taxonomy.json (merges into regions, computes specificity)
  taxonomy.json        # region fingerprints (generated; don't edit by hand)
  taxonomy_interface.py # the one reader of taxonomy.json; output labels (generic, unknown); signature dishes;
                        #   label names; restaurant-name (self-label) lookup for validate
  text.py              # shared text normalization and matching rules (the only module that normalizes text)
  paths.py             # every data location, JSON read/write, gold-set loading, the shared disk cache (§11b)
  nimble_client.py     # the only module that calls Nimble: search + extract, cache, retries, budget
  discover.py          # Nimble → data/restaurants.json; restaurant extraction (Prompt B)
  menus.py             # Nimble → data/menus/{id}.txt + data/menus/index.json
  match.py             # deterministic dish matcher
  adjudicate.py        # LLM second opinion at region level (Prompt C)
  validate.py          # agreement metrics
  pipeline.py          # menus → match → (adjudicate) → validate → data/results.json + gold score
  build_map.py         # data/results.json → site/index.html (static map page)
  llm.py               # call_llm(prompt, schema) → dict  (Claude/Gemini/OpenAI, cache; no prompts live here)
  test.py              # offline test suite (§12)
  gold_set.json        # hand-labeled restaurants for validation
  test_menus/          # small sample menus used by test.py
  data/                # git-ignored ($DATA_DIR): caches, Nimble ledger, restaurants, menus, results
  site/                # generated map page
```

---

## 2. Taxonomy (`taxonomy.json`)

**Labels are regions**, and every data field that holds one is called `region` (evidence, `top3`, self-labels,
`gold_set.json`, the adjudicator's output). The classifier answers "which region's food does this menu serve?" There are 26 regions plus `generic`:
mostly provinces (Yunnan, Fujian, Hunan…), with a few groups where restaurants and the source article treat several places as one
food culture: Sichuan & Chongqing; Jiangsu & Shanghai; Beijing, Tianjin & Hebei; Dongbei; the Northwest (Gansu/Qinghai/Ningxia);
Hong Kong & Macau; Taiwan; Southeast Asian Chinese; and the Tibetan Plateau. Regions follow the Chinese Cooking Demystified article.

**Sub-cuisines are color, not labels.** The article's 70 sub-cuisines (Han Yunnan, Dai, Teochew, Upper River Sichuan…) are kept
as each dish's `section`, so evidence cards can say "matches 过桥米线 (Han Yunnan, CCD)". The classifier never predicts them.

**Other output labels** live in `taxonomy_interface.py` as `LABELS`, `GENERIC` and `UNKNOWN`: `generic` (a standard pan-Chinese or
American-Chinese menu; it's also scored as a region using takeout dishes) and `unknown` (not enough evidence). `match.py` and
`adjudicate.py` import their ids from there.

**`taxonomy_interface.py`** is the only code that reads `taxonomy.json`: `load_taxonomy` (read once, cached; treat as
read-only), `signature_dishes(region, n)` (most distinctive dishes, used by discovery queries and the adjudicator),
`label_name(label)` (display names), and the restaurant-name lookup used by validation (`self_label_terms`,
`find_self_labels`).

**Files:**
- `taxonomy_src.json`: the hand-edited source. `subcuisines` (each with dishes, markers, self_labels, neighbors) and `regions`
  (each region's id, name, zh, self_labels and the list of sub-cuisine ids it contains). Dishes may carry `weight_override`.
- `build_taxonomy.py`: merges sub-cuisines into their region, computes `specificity` and `exact_only`, checks for errors, writes
  `taxonomy.json`. Run it after every edit to the source.
- `taxonomy.json`: generated. Don't edit by hand.

**`taxonomy.json` schema:**
```json
{
  "meta": {"source": "...", "source_url": "...", "coverage": "...", "notes": "...", "level": "region"},
  "regions": [
    {
      "id": "yunnan", "name": "Yunnan", "zh": "云南",
      "self_labels": ["Yunnan", "云南", "Yun Nan", "South of the Clouds", "云之南", "..."],
      "dishes": [
        {"en": "Crossing the bridge rice noodles", "zh": "过桥米线", "zh_alt": [],
         "aliases": ["crossing the bridge noodle", "guoqiao mixian", "..."],
         "specificity": 1.0, "exact_only": [], "section": "Han Yunnan"}
      ],
      "markers": ["米线", "mixian", "汽锅鸡", "wild mushroom", "..."]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `self_labels` | Names a restaurant would use for itself. Used for validation, never for scoring. |
| `zh`, `zh_alt` | Chinese dish names. The strongest signal. |
| `aliases` | English and pinyin menu spellings. Grow these from real menus. |
| `specificity` | `1 / (number of regions whose dish names contain this name)`, capped by `weight_override` for dishes on nearly every US menu (kung pao 0.1, mapo 0.15). |
| `exact_only` | The dish's Chinese names of ≤2 characters (`text.is_short_name`). They only match a whole Chinese run, never inside a longer name. |
| `section` | The article sub-cuisine the dish came from. For display only. |
| `markers` | Ingredients, sauces and well-known dishes the article doesn't list. Weak evidence. One-character Chinese markers are ignored. |

---

## 3. Discover (`discover.py`)

The key idea is to search by dish, not by "Chinese restaurant." Searching for signature dishes turns up regional places that call themselves generic.

```
python discover.py --city "New York" --area "Flushing, Queens" --regions yunnan,shaanxi --seed-gold --dry-run
python discover.py --city "New York" --area "Flushing, Queens" --area "Chinatown, Manhattan" --geocode
python discover.py --city "New York" --from-gold      # 0 Nimble calls
```
**`--from-gold`** adds every `gold_set.json` restaurant to `data/restaurants.json` without any search (name, Chinese name,
area as neighborhood, coordinates if given; `discovered_via: [{"kind": "gold"}]`). It merges with an existing file and is
idempotent. The gold label is never copied, so classification can't see it.
Always `--dry-run` first: it prints every query and the number of Nimble searches and LLM calls the run will make
(e.g. Yunnan + Shaanxi + gold set in one area: 11 searches, ~4 LLM calls; all regions in two areas: 158 searches, ~54 LLM calls).

**Queries, per area** (`build_queries`):
- `"{zh} {area}"` for each region's most distinctive dishes (specificity ≥ 0.5, `--dishes-per-region`, default 2); `--english` adds `"{en} {area}"`
- `"{first self-label} restaurant {area}"` once per region ("Yunnan restaurant Flushing, Queens")
- `"Chinese restaurant {area}"` with `focus="location"`, `search_depth="lite"`, 20 results. This is the coverage sweep: it finds the generic places, which are the negatives the classifier needs to see.
- `--seed-gold` adds each gold-set restaurant's name and area. `--regions` limits everything to some regions; `generic` is never searched.

**Nimble calls** go through `nimble_client.py` (§3a). Results are de-duplicated by URL, and each URL remembers which queries returned it.

**Turning results into restaurants:** Prompt B (`extract_restaurants`) runs on batches of 30 results and returns `{name, name_zh, address, neighborhood, website, source_url, evidence}`. Nimble's `additional_data` (where location results may carry addresses) is passed along. Records are merged **into the existing `data/restaurants.json`** (so restaurants seeded with `--from-gold` or found in an earlier run are kept) when they share a normalized English or Chinese name (`text.name_keys`) and their street numbers don't conflict; a missing field is filled from a later record, never overwritten.

**Output:** `data/restaurants.json`, a list of `{id, name, name_zh, address, neighborhood, website, lat, lng, sources: [url…], evidence: [quote…], discovered_via: [{query, kind, region}…]}`. `id` is a slug of the name plus a short hash of name and street number, made once when the restaurant is first recorded and never changed after, so menus saved under it stay linked even when a later run fills in the address.

**Geocoding** (`--geocode`): OpenStreetMap Nominatim at 1 request per second, cached in `data/geocode_cache.json`. Restaurants without an address stay without coordinates.

**Scope for the demo:** one or two neighborhoods and a few regions. Everything is cached, so re-runs are free.

### 3a. Nimble client (`nimble_client.py`)

The only module that calls Nimble (REST via `requests`; no SDK needed).
- `search(query, max_results, search_depth, full_content, focus, country, time_range, include_domains, exclude_domains, output_format)` → `POST https://sdk.nimbleway.com/v2/search`, returns the `results` list (`{title, description, url, content, metadata, additional_data?}`). Focus modes other than `general` need `search_depth="lite"`.
- `extract(url, formats, render, country)` → `POST https://sdk.nimbleway.com/v2/extract`, returns `{status, status_code, url, markdown, html, links}`.
- Auth: `Authorization: Bearer $NIMBLE_API_KEY`. Cache: `data/nimble_cache/<sha256>.json` (via `paths.py`), keyed on endpoint + request body; cached requests work without a key or network. Retries with exponential backoff on 429 and 5xx; other errors (bad key, out of credits) fail immediately with `NimbleError`.
- **Budget:** every live HTTP attempt (retries included) is recorded in `data/nimble_usage.json`, which persists across runs. `NIMBLE_BUDGET` is the total allowed across all runs; a live call that would exceed it raises `NimbleBudgetError` before anything is sent. With `NIMBLE_BUDGET` unset, live calls are refused (cached calls still work); `NIMBLE_BUDGET=unlimited` turns the cap off. `python nimble_client.py usage` prints calls used, the budget and the log. `discover.py` stops searching and `menus.py` stops fetching on a budget error.

---

## 4. Menus (`menus.py`)

```
python menus.py --city "New York"                       # every restaurant in data/restaurants.json without a menu yet
python menus.py --city "New York" --id <id> --refresh
python menus.py --city "New York" --search-only --limit 1    # exactly 1 Nimble call
```
`--search-only` skips step 2 below, so each restaurant costs exactly one call. `--limit N` fetches at most N restaurants
this run. Restaurants are fetched in priority order: gold set, then dish searches, region searches, seeds, and generic
sweep results last, so a tight limit goes where it matters.

For each restaurant, stop at the first source with at least 15 menu-looking lines (short lines with a price or Chinese characters):
1. Nimble Search `"{name} {neighborhood or city} menu"`, 5 results, `full_content=True`. Among results with enough menu lines, prefer the restaurant's own site, then delivery and menu sites (Grubhub, Seamless, DoorDash, Uber Eats, ChowNow, Toast, MenuPages, Yelp…), then the most menu lines.
2. If the website is known: Nimble Extract on it, then on a same-site link containing "menu" if the home page isn't the menu.
3. Otherwise keep the best partial text (reviews and listings that name dishes): `menu_quality="partial"`. Nothing at all: `menu_quality="none"`.

PDF and image URLs are skipped in v1. Save the raw text to `data/menus/{id}.txt`, and record `{name, menu_quality, menu_source_url, n_menu_lines, query}` per id in `data/menus/index.json`. The index is saved after every restaurant, and restaurants already in it are skipped unless `--refresh`. A failed search is reported as `error` and not indexed, so it's retried on the next run; a budget stop isn't recorded as `none` either.

**Normalization:** run all text through `text.norm` (§11): opencc converts traditional to simplified Chinese, everything is lowercased, and the Chinese characters are kept. Save the raw menu text unnormalized, because script and romanization are signals of their own (see `notes.md`). A bilingual menu is the strongest signal you can get.

---

## 5. Classify

### 5a. Deterministic matcher (`match.py`)
This step is fast, free, and explainable, so run it first. **It looks at menu text only, never the restaurant's name.**
Before matching, `pipeline.py` removes the restaurant's own English and Chinese name from every line without a price
(`text.strip_names`): page titles, headers and banners repeat the name, while priced lines are menu items. So a place
named after its signature dish (桂林米粉) still gets credit for that dish where it's sold. Every line is then normalized
with `text.norm`.

**Matching hierarchy, per menu line:**

| Order | Rule | Strength |
|---|---|---|
| 1 | Chinese name, exact substring. Names are tried longest-first, and matched characters are consumed so 东江酿豆腐 isn't also counted as 酿豆腐. | 1.0 |
| 2 | Chinese name, in order with up to 2 extra characters (金钱鸡蛋 finds 金钱蛋). | 0.8 |
| 3 | Names in a dish's `exact_only` list (≤2 characters) match only as a whole Chinese run. | 1.0 |
| 4 | English or pinyin alias, whole-word substring (a plural "s" is allowed), tried longest-first. Only when no Chinese name matched on that line. | 0.9 |
| 5 | Alias, fuzzy, for dishes rule 4 didn't match: aliases of 8+ characters only; rapidfuzz `ratio ≥ 90` between the alias and a run of the line's words of about the same length (±1 word). Typos and spacing match ("crosing the bridge noodle", "guo qiao mixian"); a shared tail doesn't ("yunnan rice noodle" ≠ "hand rice noodle"). | 0.7 |

Each dish counts once per menu, at its best strength. Markers add 0.15 each, capped at 1.0 per region. One-character Chinese markers are ignored.

```
dish_score[r]  = Σ strength × specificity            (over region r's matched dishes)
score[r]       = dish_score[r] / n_dishes[r] ** 0.25 + marker_bonus[r]
probs          = softmax(score / 0.5)  over regions with score > 0 only
```
The output is `{label, is_specialist, needs_llm, top3, scores, margin, entropy, markers, evidence: [{menu_line, matched, how, region, dish_en, dish_zh, section, strength, specificity, weight}]}`.

**Label:** the top region if its score ≥ 0.8 and beats `generic`; otherwise `generic` if the takeout dishes dominate; otherwise `unknown` (labels from `taxonomy_interface.py`).
**Specialist:** top score ≥ 1.2 and ≥ 60% of all regional score. Entropy is reported too, for display.

### 5b. LLM adjudicator (`adjudicate.py`)
**Trigger:** the matcher sets `needs_llm` when the label is `unknown`, or when the label is a region and the relative gap between the top two is small: `(top − second) / top < 0.25`. `generic` labels don't trigger it. You can also call it manually for the demo.

**Candidates:** the regions the matcher actually scored, highest first, up to 6 (`generic` is always offered separately). If fewer than 3 regions scored, every region is offered. Each candidate comes with its 6 most distinctive dishes. Send the menu (trimmed to 8k characters), the candidates, the matcher's summary and Prompt C. It gets the same name-stripped menu text as the matcher. The model must return JSON matching a fixed schema (Claude tool use, Gemini JSON mode or OpenAI JSON-schema output), and any answer that isn't a region id, `generic` or `unknown` becomes `unknown`.

**Final label** (`final_label(match_result, verdict)`): the matcher's label, unless the matcher set `needs_llm` and a verdict was obtained; then the adjudicator's `primary` and `is_specialist`. `pipeline.py` keeps both (`matcher_label` and `adjudicator`) and records `label_source`. The adjudicator only runs with `pipeline.py --llm`; an LLM failure falls back to the matcher.

---

## 6. Prompts (portable to Claude, Gemini or OpenAI)

Prompts and their JSON schemas live in the module that uses them; `llm.py` only sends them. (A prompt to generate the taxonomy itself is a planned feature, §13.)

**Prompt B: restaurant extraction from search results** (`discover.py`)
```
From these web search results, list distinct restaurants that are physically located in {city}.
Only include actual restaurants (not articles, authors, grocery stores, or closed places).
Do not invent addresses or websites: use null when the results don't state them.
Include the Chinese name if the results give one.
RESULTS: <<<...>>>
Schema: {restaurants: [{name, name_zh|null, address|null, neighborhood|null, website|null, source_url, evidence}]}
(evidence: a short quote showing it's a restaurant in the city)
```

**Prompt C: region adjudicator** (`adjudicate.py`)
```
System: classify by the regional cuisine the menu actually serves, using the CCD taxonomy;
judge only from dishes, not the restaurant's name or marketing.

Classify this restaurant's menu into ONE region id from this list:
{candidate region ids, each with its 6 most distinctive dishes}
Also allowed: "generic" (standard pan-Chinese / American-Chinese menu), "unknown" (not enough evidence).
- Several region-specific dishes are strong evidence. One famous dish (kung pao, mapo tofu, dumplings) is weak.
- US menus use English or pinyin names; recognize dishes by description too.
- is_specialist = the menu is clearly centered on that one region.
A rule-based matcher already scored this menu: {top 3 scores, generic score, matched dishes}
Return JSON: {primary, secondary|null, confidence 0-1, is_specialist,
  evidence:[{menu_item, dish, region}], notes}
MENU: <<<...>>>
```

---

## 7. Validate (`validate.py`)

This step is what sets the project apart, so it gets three independent checks:
1. **Self-label agreement.** Does the restaurant's own name or description include a `self_labels` term (云南, Yunnan, 云之南, Yun Nan, 贵州, and so on)? Report how often that agrees with the predicted region. This works as free, noisy ground truth. Implemented as `self_label_check(name, predicted)` → `agrees`, `conflicts` or `no_label` (most restaurants; not evidence either way). The lookup lives in `taxonomy_interface.py`; the matcher doesn't use it (classification never looks at names), and `validate.py` never imports `match.py`, so validation stays independent of the matcher's scoring.
2. **External corroboration.** Run `nimble.search(f'"{name}" {city} Yunnan OR Guizhou OR Sichuan ...', time_range="year")` against press and Reddit, excluding the restaurant's own domain. Count independent sources that mention the predicted region.
3. **Gold set.** Hand-label 10–15 NYC restaurants you already know (`gold_set.json`). `score_gold(results, gold)` matches results by English or Chinese name and reports accuracy and a region-level confusion matrix. Gold restaurants with no result (`missing`) or no fetched menu (`no_menu`) are listed but not scored wrong, since they measure discovery and fetching, not classification.

v0.1 implements 1 and 3; 2 (external corroboration) is planned. The map shows the self-label result as a badge ("name agrees" / "name says otherwise").

**Stretch goal: Instagram.** Search public posts that mention signature dishes along with the restaurant name, and use them only as corroboration. Don't use them for classification, since influencer content is biased toward visuals.

---

## 8. Pipeline and map (`pipeline.py`, `build_map.py`)

**`pipeline.py`** (`python pipeline.py [--llm]`): for each restaurant in `data/restaurants.json`, read `data/menus/{id}.txt`
(no menu → `unknown`, `label_source: "no menu"`), strip the restaurant's own name from unpriced lines (`text.strip_names`),
run the matcher, the adjudicator if `--llm` and `needs_llm`, `final_label`, and the self-label check (once, after the label
is known; `unchecked` for a restaurant with no menu whose name names a region). Writes `data/results.json`: `{generated_at, summary, gold, restaurants}`, where `summary` counts
restaurants, menus, regional, specialists, generic, unknown, adjudicated and per-region totals, and `gold` is `score_gold`'s output.

**`build_map.py`** (`python build_map.py`): one self-contained `site/index.html` with the results embedded (display fields
only, and only the evidence for the chosen label). Leaflet 1.9.4 from cdnjs, CARTO light/dark tiles. The page has:
- the headline stat: how many restaurants with menus are regional specialists
- a region filter (with counts) and a "specialists only" toggle
- a map colored by region, with specialists drawn larger; restaurants without coordinates are listed as "no map location"
- a card per restaurant: name (with Chinese name), region, confidence, LLM / no-menu badges, the self-label badge, the CCD
  sections (not shown for generic), the matched menu lines next to their CCD dishes, and links to the menu source,
  the website and the CCD guide
- light and dark themes, and a stacked layout on phones

---

## 9. Release plan (v0.1)

The hackathon build order is done. v0.1 is run on a 20-call Nimble budget; the step-by-step plan, with call counts, is
in README.md ("Running on a small Nimble budget"): seed the gold set (0 calls), fetch its menus one call each,
classify and map (0), then discover one area for one region (3 calls) and fetch up to 8 more menus.

## 10. Risks

- **Few Yunnan restaurants in NYC.** The region-level classifier still works across all the classes, so lead with the overall map and use Yunnan as the zoom-in.
- **Image or PDF menus.** Skip them and show them as "no menu."
- **Running out of credits.** Every Nimble and LLM response is cached on disk; `NIMBLE_BUDGET` caps live Nimble calls across runs; `llm.py` falls back across Claude, Gemini and OpenAI.
- **Romanization noise** (mixian, mi xian, mee sin). Prefer Chinese-character matches; aliases match as whole words first, and fuzzy matching compares whole word runs, only for aliases of 8+ characters (§5a).

---

## 11. Shared text normalization (`text.py`)

The only module that normalizes text. Everything else imports from it, so the rules can't drift apart between modules.

| Function | Does |
|---|---|
| `CJK` | The one regex for Chinese characters (Unified Ideographs, Ext A, Ext B–G). |
| `to_simplified(text)` | Traditional → simplified with opencc (`opencc-python-reimplemented`, pure Python). Warns and passes text through if it isn't installed. |
| `norm(text)` | Simplified, lowercase, apostrophes dropped, other punctuation to single spaces. |
| `cjk_runs`, `cjk_of`, `strip_cjk` | Split a normalized line into its Chinese runs, all Chinese characters, or the English/pinyin remainder. |
| `is_cjk`, `is_single_cjk` | All-Chinese check; one-character check (used to skip markers and labels like 酸 or 川). |
| `is_short_name(name)` | The ≤2-character rule. `build_taxonomy.py` uses it to write `exact_only`; `match.py` reads that field. |
| `find_terms(text, terms)` | Which `(term, tag)` pairs appear in a normalized text, returned as `{tag: [terms]}`. Chinese terms must sit inside a single run of Chinese characters; other terms must match whole words. Used for markers and self-labels. Dish matching is separate (§5a). |
| `PRICE`, `has_price(line)` | The menu-price pattern ($12.95, 12.95). Used to count menu lines (`menus.py`) and by `strip_names`. |
| `name_keys(*names)` | A restaurant's names, normalized; two records sharing a key share a name. Used by discovery merging and gold scoring. |
| `strip_names(text, names)` | Removes a restaurant's own name from lines without a price (§5a). English words match case-insensitively with any punctuation between them (Xi'an = XI AN); Chinese matches after traditional → simplified. |

Used by `build_taxonomy.py`, `match.py`, `pipeline.py`, `discover.py`, `menus.py`, `validate.py` and `taxonomy_interface.py`;
the script/romanization signals in `notes.md` should use it too.

## 11b. Data locations and disk cache (`paths.py`)

Every data path is defined here, so `DATA_DIR` (default `./data`) moves everything, the caches and the Nimble budget
ledger included. `LLM_CACHE_DIR`, `NIMBLE_CACHE_DIR` and `NIMBLE_USAGE_PATH` can still be set individually.

| Name | Path |
|---|---|
| `RESTAURANTS_PATH`, `MENUS_DIR`, `MENU_INDEX_PATH`, `RESULTS_PATH` | `data/restaurants.json`, `data/menus/`, `data/menus/index.json`, `data/results.json` |
| `NIMBLE_CACHE_DIR`, `NIMBLE_USAGE_PATH`, `LLM_CACHE_DIR`, `GEOCODE_CACHE` | `data/nimble_cache/`, `data/nimble_usage.json`, `data/llm_cache/`, `data/geocode_cache.json` |
| `GOLD_PATH`, `SITE_PATH` | `gold_set.json` (in the repo), `site/index.html` |

Helpers: `read_json(path, default)`, `write_json(path, obj)`, `load_gold()`, and the shared disk cache used by `llm.py` and
`nimble_client.py`: `cache_path(dir, *parts)` (a SHA-256 of the request's parts), `cache_get`, `cache_put`.

---

## 12. Tests (`test.py`)

Offline: no API keys or network. LLM calls are mocked, and the cache test uses a temporary folder.

```
python test.py      # PASS/FAIL per test, exit code 1 on any failure
pytest test.py      # also works
```

| Area | Checks |
|---|---|
| `text.py` | Traditional → simplified, punctuation, Chinese/Latin splitting, one-character and short-name rules, `find_terms` (whole words, no matching across Chinese runs, no duplicates), `name_keys`, `strip_names` (headers stripped, priced lines kept, Xi'an = XI'AN, traditional pages). |
| Taxonomy | Every dish's `exact_only` agrees with `text.is_short_name`; the output labels in `taxonomy_interface.py` are consistent with `taxonomy.json` (`generic` is a region, `unknown` isn't). |
| `match.py` | Expected labels and specialist flags on `test_menus/`; traditional menus match by Chinese; longest-first consumption; subsequence and exact-only matching; probabilities only over scored regions; `unknown` on an empty menu; `classify` takes menu text only; fuzzy alias matching (typos match, a shared tail doesn't, exact beats fuzzy). |
| `taxonomy_interface.py` | Taxonomy read once and cached; self-label terms include 云之南 and "yun nan" and drop one-character labels; every gold-set name finds its region; `signature_dishes` order; `label_name`. |
| `paths.py` | JSON round trip; cache paths stable per request; `DATA_DIR` moves every data path, the caches and the ledger. |
| `validate.py` | `self_label_check` gives `agrees`, `conflicts` and `no_label` correctly; every gold-set restaurant agrees with its label. |
| `llm.py` | JSON extraction; falls back to the other provider on a credit error without retrying; cached answers make no API calls; provider order (`LLM_PROVIDERS`, `LLM_PROVIDER`, unknown names rejected); OpenAI request shape with a mocked SDK. |
| `adjudicate.py` | Candidates are scored regions (or all regions when fewer than 3 scored); invalid ids become `unknown`. |
| `nimble_client.py` | Request body and auth header; retries after a 429; no retry on a 401; cached requests make no calls; clear error without a key; budget: refused when unset, enforced before sending, cached calls free, ledger persists across budget changes. |
| `discover.py` | `extract_restaurants` returns the model's restaurant list; query building (kinds, region filter, no `generic`); merging duplicates and keeping separate branches apart; `--from-gold` (no calls, idempotent, no label leak); discovery after `--from-gold` keeps the gold restaurants and their ids; stops on a budget error; an end-to-end run with Nimble and the LLM mocked. |
| `menus.py` | Own site preferred over delivery sites; PDFs skipped; Extract follows a menu link; partial and none fallbacks; raw text and index written; already-fetched menus skipped; `--search-only`, `--limit`, fetch priority; errors and budget stops aren't indexed. |
| `adjudicate.final_label` | Matcher label unless it asked for a second opinion and got one; verdict ignored when the matcher was confident. |
| `pipeline.py` | A restaurant's name can't classify it (header stripped), but its priced namesake dish still counts. End to end offline on the sample menus: region, generic and no-menu results, self-label agreement, CCD sections, summary counts, gold score, results file written. |
| `validate.score_gold` | Accuracy and confusion matrix; matching by Chinese name; missing and no-menu gold restaurants listed, not scored wrong. |
| `build_map.py` | Data embedded safely (a restaurant name can't close the script tag); display fields only; only the chosen label's evidence. |
| Structure | `llm.py` imports no pipeline modules; `validate.py` doesn't import `match.py`; only `taxonomy_interface.py` reads `taxonomy.json`; only `nimble_client.py` calls Nimble; only `paths.py` defines data locations and cache hashing; no module except `text.py` does its own normalization. |

Add a test menu to `test_menus/` and an entry to `EXPECTED` in `test.py` for every real menu you validate by hand.

---

## 13. Future planned features

- **Prompt to generate the taxonomy for cuisines.** Paste a source article (or a section of one) and get `taxonomy_src.json` entries back: sub-cuisines, dishes with Chinese names, menu-style aliases, markers and self-labels. Would let the taxonomy cover new sources or other cuisines without hand-building.
- Script and romanization signals (traditional vs simplified, pinyin vs Wade-Giles vs Cantonese spellings). See `notes.md`.
- Instagram as corroboration only (§7).
