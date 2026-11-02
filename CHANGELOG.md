# Changelog

## v0.1.0 (2026-10-28)

First release.

- **Taxonomy:** 26 regions plus `generic`, built from Chinese Cooking Demystified's *63 Chinese Cuisines* (70 sub-cuisines kept as descriptive sections), about 300 dishes with Chinese names, menu aliases and specificity weights.
- **Matcher** (`match.py`): deterministic, explainable region scoring of menu text only: Chinese names first (longest-first, in-order near matches, exact-only short names), then English/pinyin aliases (exact, then fuzzy over word runs), then weak markers. The restaurant's own name is stripped from unpriced menu lines first, so a name can't classify a restaurant.
- **LLM adjudicator** (`adjudicate.py`): optional second opinion when the matcher isn't confident; Claude, Gemini or OpenAI with configurable fallback order (`llm.py`).
- **Nimble** (`nimble_client.py`, `discover.py`, `menus.py`): dish-driven restaurant discovery and menu fetching, cached on disk, with a hard live-call budget (`NIMBLE_BUDGET`) tracked across runs.
- **Validation** (`validate.py`): self-label check (does the restaurant's own name agree?) and gold-set scoring.
- **Pipeline** (`pipeline.py`) writes `data/results.json`; **map** (`build_map.py`) turns it into one static page.
- 52 offline tests (`python test.py`), no keys or network needed.

Known limits: tested on NYC only; English-only menus depend on the alias lists; PDF and image menus are skipped; Nimble doesn't document location-result fields, so addresses may need geocoding from other sources.
