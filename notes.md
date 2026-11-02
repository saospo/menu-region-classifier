# Notes / TODO

Status as of v0.1.0. Done items stay here, checked, so the history is visible; open ideas are unchecked.

## Signals we're missing: script and romanization
`match.py` throws these away right now. `text.norm()` converts traditional → simplified and lowercases everything before matching. Measure them on the **raw** menu text first (`menus.py` saves it unnormalized), then normalize.

- [ ] **Traditional vs simplified characters.** Count characters that only exist in traditional script (e.g. 過 橋 線 鍋 雞 麵 灣) against simplified-only ones (过 桥 线 锅 鸡 面 湾). Mostly traditional points to Hong Kong, Taiwan or Macau, or an older Cantonese/Taishanese diaspora restaurant. Mostly simplified points to mainland migrants from after the 1990s. A traditional-script menu with a Fujian or Sichuan fingerprint is interesting in its own right.
  - Easy version: `ratio = trad_only / (trad_only + simp_only)` using opencc t2s, counting characters that changed during conversion.
- [ ] **Pinyin or no pinyin.** Mainland-run places often print pinyin (xiao long bao, mala xiang guo, liangpi, roujiamo). Older or Cantonese places translate dishes into English instead ("Soup Dumplings," "Cold Skin Noodles"). Feature: how many of the menu's English tokens are valid pinyin syllables.
- [ ] **Wade-Giles / postal / Cantonese romanization vs Hanyu Pinyin.** The spelling dates and locates the menu:
  - Wade-Giles / postal: Szechuan, Kung Pao, Peking, Tsingtao, Hunan, Chungking, Tientsin
  - Cantonese / Taishanese: chow mein, chop suey, lo mein, har gow, siu mai, cheung fun, chow fun, wonton, bok choy, gai lan, char siu
  - Hokkien / Teochew: kway teow, mee, popiah, bak kut teh, kueh
  - Hanyu Pinyin: Sichuan, Gong Bao, Beijing, Chongqing, xiaolongbao
  - Feature: a romanization profile per menu, e.g. `{pinyin: 0.6, wade_giles: 0.1, cantonese: 0.3}`. "Szechuan" vs "Sichuan" alone is a decent sign of old-school American-Chinese vs newer mainland Sichuan.
- [ ] Show these as menu "provenance" next to the region label. They tell you who runs the place and when they arrived, not just what they serve. Example: a Lanzhou noodle shop run by Fujianese owners.
- [ ] Possibly feed them in as weak priors: traditional-heavy → slight boost to HK/Macau, Taiwan and Guangdong; Hokkien romanization → Fujian or Southeast Asia.
- Put the measuring functions in `text.py` (it's the only module that does text normalization; a test enforces that).

## Pipeline
- [x] `llm.py`: `call_llm(prompt, schema) -> dict` with Claude, Gemini and OpenAI in a configurable fallback order; every call cached to disk.
- [x] `discover.py`: Nimble search by dish name and by self-labels, plus a `focus="location"` sweep → `data/restaurants.json`. Also `--from-gold` (0 calls); merges into the existing file and keeps ids.
- [x] `menus.py`: Nimble search with `full_content=True`, then Extract → `data/menus/{id}.txt`. Skips PDF and image menus. `--search-only` and `--limit` for small budgets.
- [x] `adjudicate.py`: only for results with `needs_llm=True`, with the scored regions and Prompt C; `final_label`.
- [x] `validate.py`: self-label check and gold-set scoring.
- [ ] `validate.py`: press and Reddit corroboration through Nimble, and a combined per-restaurant badge (corroborated / menu-only / conflicting).
- [x] UI: a static map page (`build_map.py`) instead of the planned Streamlit + Folium app, with evidence cards.
- [x] `pipeline.py`: runs match → adjudicate → validate over the fetched menus and writes `data/results.json`.
- [x] Nimble budget cap (`NIMBLE_BUDGET`) tracked across runs.

## Matcher improvements
- [x] Restaurant names can't classify a restaurant: the own name is stripped from unpriced menu lines before matching.
- [x] Fuzzy alias matching compares word runs (no more "yunnan rice noodle" → "hand rice noodle"); exact aliases beat fuzzy ones.
- [ ] Two-character exact-only names miss when they start a longer item: 乳饼炒火腿 didn't match 乳饼 by Chinese (it was caught by the English alias). Allow a match at the start or end of a CJK run when followed or preceded by a cooking verb (炒 烧 炖 拌 煎 炸).
- [ ] Count repeated hits: three versions of 过桥米线 on one menu should count a bit more than one.
- [ ] Tune `SIZE_EXPONENT`, `MIN_LABEL_SCORE` and `SPECIALIST_MIN` on real menus, not the toy test menus.
- [ ] Grow `aliases` from real menus. Log lines that look like dishes but match nothing (`unmatched_lines` output) and review them.

## Data / validation
- [ ] Gold set: hand-label 10–15 NYC restaurants. It has the 4 Yunnan places so far; add non-Yunnan controls (Xi'an Famous Foods should come out as Shaanxi, plus a Fujian, a Dongbei and a plain takeout place).
- [ ] Watch for this in Flushing: "Lanzhou hand-pulled noodle" shops run by Fujianese owners. The menu says Northwest, the owners are from Fujian. Good demo story.
- [ ] Instagram is a stretch goal and only for corroboration. Never use it to score.

## Taxonomy
- [ ] Markers include well-known dishes the article doesn't list (小笼包, 炸酱面, 羊肉串, 卤肉饭, 佛跳墙, 汽锅鸡…). Keep them weak, and flag them in the UI as "not from CCD."
- [x] `taxonomy_src.json`'s region list renamed from `provinces` to `regions`, matching everything downstream (`build_taxonomy.py` updated).
- Rebuild after any edit: `python build_taxonomy.py`.
