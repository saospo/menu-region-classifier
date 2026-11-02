"""test.py: offline tests for the pipeline. No API keys or network needed; LLM calls are mocked.

    python test.py            # runs everything, prints PASS/FAIL per test, exit code 1 on any failure
    pytest test.py            # also works

What's covered:
    text.py         normalization, traditional -> simplified, Chinese/Latin splitting, short-name rule, find_terms,
                    name_keys, strip_names (headers stripped, priced lines kept, spelling variants, traditional pages)
    taxonomy.json   every dish's exact_only list agrees with text.is_short_name; output labels are consistent
    match.py        expected labels on test_menus/, longest-first consumption, subsequence and exact-only
                    matching, probabilities over scored regions only, classify takes menu text only,
                    fuzzy alias matching (typos match, a shared tail doesn't, exact beats fuzzy)
    taxonomy_interface.py  taxonomy read once and cached; self-label terms; every gold-set name finds its region;
                    signature_dishes order; label_name
    paths.py        JSON round trip, stable cache paths, DATA_DIR moves every data path, cache and the ledger
    validate.py     self-label check (agrees / conflicts / no_label), every gold-set restaurant agrees, score_gold
    llm.py          JSON extraction, provider fallback on a credit error, cache hits without API calls,
                    provider order (LLM_PROVIDERS / LLM_PROVIDER), OpenAI request shape (mocked SDK)
    adjudicate.py   candidate selection (scored regions, or all regions when too few scored), invalid-id guard,
                    final_label
    discover.py     extract_restaurants returns the model's restaurant list; query building; merging duplicates;
                    --from-gold; discovery after --from-gold keeps gold restaurants and their ids; budget stop;
                    an end-to-end run with Nimble and the LLM mocked
    nimble_client.py  request body and auth header, retry on 429, no retry on 401, cache hits, missing key, budget
    menus.py        own site preferred over delivery sites, PDFs skipped, Extract follows a menu link,
                    partial/none fallbacks, raw text and index written, already-fetched menus skipped,
                    --search-only, --limit, fetch priority, errors and budget stops not indexed
    pipeline.py     a name can't classify a restaurant (its priced namesake dish still counts); end to end offline
    build_map.py    data embedded safely, display fields only, only the chosen label's evidence
    structure       llm.py doesn't import pipeline modules; validate.py doesn't import match.py;
                    only taxonomy_interface.py reads taxonomy.json; only text.py does text normalization;
                    only nimble_client.py calls Nimble; only paths.py defines data locations and cache hashing
"""
from __future__ import annotations
import ast, json, os, shutil, sys, tempfile, traceback
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import text
from match import Matcher
from taxonomy_interface import GENERIC, UNKNOWN, LABELS

M = Matcher()

EXPECTED = {                      # test menu -> (label, is_specialist)
    "yunnan.txt": ("yunnan", True),
    "takeout.txt": (GENERIC, False),
    "xian.txt": ("shaanxi", True),
    "fuzhou.txt": ("fujian", True),
    "dongbei.txt": ("dongbei", True),
}


def classify_file(name):
    return M.classify((HERE / "test_menus" / name).read_text(encoding="utf-8"))


# ------------------------------------------------------------------ text.py
def test_norm_traditional_and_punctuation():
    assert text.norm("過橋米線 Crossing-the-Bridge Noodle’s!") == "过桥米线 crossing the bridge noodles"


def test_cjk_split():
    line = text.norm("乳饼炒火腿 Goat Cheese 汽锅鸡")
    assert text.cjk_runs(line) == ["乳饼炒火腿", "汽锅鸡"]
    assert text.cjk_of(line) == "乳饼炒火腿汽锅鸡"
    assert text.strip_cjk(line) == "goat cheese"


def test_find_terms():
    terms = [("云南", "yunnan"), ("云之南", "yunnan"), ("yun nan", "yunnan"), ("nan", "x"), ("云南", "yunnan")]
    assert text.find_terms(text.norm("South of the Clouds 云之南"), terms) == {"yunnan": ["云之南"]}
    assert text.find_terms(text.norm("Yun Nan Flavour Garden"), terms) == {"yunnan": ["yun nan"], "x": ["nan"]}
    assert text.find_terms(text.norm("Nanjing Garden"), terms) == {}            # whole words only
    assert text.find_terms(text.norm("云 Garden 南"), terms) == {}               # no matching across Chinese runs
    assert text.find_terms(text.norm("雲南菜 云南"), terms) == {"yunnan": ["云南"]}   # traditional input; no duplicates


def test_single_cjk_and_short_names():
    assert text.is_single_cjk("酸") and not text.is_single_cjk("酸汤") and not text.is_single_cjk("a")
    assert text.is_short_name("烧鹅") and not text.is_short_name("过桥米线")


# ------------------------------------------------------------------ taxonomy / labels
def test_exact_only_matches_short_name_rule():
    for p in M.regions.values():
        for d in p["dishes"]:
            names = ([d["zh"]] if d.get("zh") else []) + d.get("zh_alt", [])
            assert d["exact_only"] == [n for n in names if text.is_short_name(n)], d["en"]


def test_output_labels():
    assert LABELS["generic"]["id"] == GENERIC and LABELS["unknown"]["id"] == UNKNOWN
    assert not (HERE / "labels.json").exists()   # labels live in taxonomy_interface.py now
    assert GENERIC in M.regions            # generic is also scored as a region
    assert UNKNOWN not in M.regions


# ------------------------------------------------------------------ match.py
def test_expected_labels():
    for name, (label, specialist) in EXPECTED.items():
        r = classify_file(name)
        assert r["label"] == label, f"{name}: got {r['label']}"
        assert r["is_specialist"] == specialist, f"{name}: specialist={r['is_specialist']}"


def test_traditional_menu_matches_by_chinese():
    r = classify_file("yunnan.txt")
    hit = [e for e in r["evidence"] if e["dish_zh"] == "过桥米线"][0]
    assert hit["how"] == "zh", hit                 # 過橋米線 on the menu, matched after conversion


def test_longest_first_consumption():
    hits = M._match_line("东江酿豆腐 Dongjiang Stuffed Tofu")
    dishes = {M.dish(k)["zh"] for k, *_ in hits}
    assert dishes == {"东江酿豆腐"}, dishes          # not also Hakka 酿豆腐


def test_subsequence_match():
    hits = M._match_line("金钱鸡蛋")
    assert [(M.dish(k)["zh"], how) for k, _, _, how in hits] == [("金钱蛋", "zh_subseq")]


def test_exact_only_uses_field():
    whole = M._match_line("乳饼")
    inside = M._match_line("乳饼炒火腿")
    assert [how for *_, how in whole] == ["zh_exact"]
    assert not any(how.startswith("zh") and M.dish(k)["zh"] == "乳饼" for k, _, _, how in inside)


def test_probabilities_over_scored_only():
    r = classify_file("xian.txt")
    scored = {k for k, v in r["scores"].items() if v > 0}
    assert r["top3"][0]["prob"] > 0.8
    probs = {t["region"]: t["prob"] for t in r["top3"]}
    assert all(probs[k] == 0.0 for k in probs if k not in scored)


def test_unknown_label_on_empty_menu():
    r = M.classify("Coffee\nTea\nWater")
    assert r["label"] == UNKNOWN and r["needs_llm"] and r["label_name"] == LABELS["unknown"]["name"]


def test_classification_takes_menu_text_only():
    import inspect
    assert list(inspect.signature(Matcher.classify).parameters) == ["self", "menu_text"]
    assert not hasattr(M, "labels") and not hasattr(M, "self_labels")
    assert "self_labels" not in M.classify("过桥米线")


def test_strip_names():
    menu = ("Guilin Rice Noodle 桂林米粉 | Order Online\n桂林米粉 Guilin Rice Noodle $9.95\n"
            "XI'AN famous foods — menu\n雲南小館 home\n过桥米线 12.50")
    out = text.strip_names(menu, ["Guilin Rice Noodle", "桂林米粉"]).split("\n")
    assert "Guilin" not in out[0] and "桂林米粉" not in out[0]                 # header: name removed
    assert out[1] == "桂林米粉 Guilin Rice Noodle $9.95"                      # priced menu item: untouched
    assert "famous foods" not in text.strip_names(menu, ["Xi'an Famous Foods"]).lower()   # XI'AN = Xi'an
    stripped = text.strip_names(menu, ["Yunnan Kitchen", "云南小馆"]).split("\n")
    assert "雲南小館" not in stripped[3] and "home" in stripped[3]              # traditional on the page, simplified name
    assert text.strip_names(menu, [None, ""]) == menu
    assert text.strip_names("Noodle Bar\nSamsa", ["Noodle"]).split() == ["Bar", "Samsa"]
    assert text.strip_names("Noodles Bar", ["Noodle"]) == "Noodles Bar"                   # whole words only


def test_name_keys():
    assert text.name_keys("Yun Nan Flavour Garden", None, "雲南味道", "") == {"yun nan flavour garden", "云南味道"}


def test_fuzzy_alias_matching():
    import match
    assert not match.fuzzy_word_match("hand rice noodle", "ten seconds yunnan rice noodle")
    assert match.fuzzy_word_match("crossing the bridge noodle", "13 crosing the bridge noodle 16 95")
    assert match.fuzzy_word_match("guoqiao mixian", "guo qiao mixian special")
    assert not match.fuzzy_word_match("dry fried green beans", "dry fried string beans")
    hits = M._match_line("1. Egg Roll (1)")                                   # exact "egg roll" beats fuzzy "egg rolls"
    assert [(M.dish(k)["en"], how) for k, _, _, how in hits] == [("Egg roll", "alias")]
    assert M.classify("十秒到 Ten Seconds Yunnan Rice Noodle")["evidence"] == []


# ------------------------------------------------------------------ taxonomy_interface.py / validate.py
def test_taxonomy_interface():
    import taxonomy_interface as ti
    tax = ti.load_taxonomy()
    assert ti.load_taxonomy() is tax                                   # read once, then cached
    terms = ti.self_label_terms(tax)
    assert ("云之南", "yunnan") in terms and ("yun nan", "yunnan") in terms
    assert not any(text.is_single_cjk(t) for t, _ in terms)            # 川, 湘… dropped
    assert ti.find_self_labels(None, terms) == []
    yunnan = next(p for p in tax["regions"] if p["id"] == "yunnan")
    sig = ti.signature_dishes(yunnan, 3, require_zh=True, min_specificity=0.5)
    assert [d["zh"] for d in sig] == ["过桥米线", "小锅米线", "老奶洋芋"]
    specs = [d["specificity"] for d in ti.signature_dishes(next(p for p in tax["regions"] if p["id"] == "guangdong"), 10)]
    assert specs == sorted(specs, reverse=True)
    assert [ti.label_name(x) for x in ("yunnan", GENERIC, UNKNOWN)] == ["Yunnan", "Generic / Pan-Chinese", "Unknown"]


def test_paths_helpers_and_data_dir():
    import paths, subprocess
    tmp = Path(tempfile.mkdtemp())
    try:
        paths.write_json(tmp / "a" / "x.json", {"k": "云南"})
        assert paths.read_json(tmp / "a" / "x.json") == {"k": "云南"} and paths.read_json(tmp / "nope.json", []) == []
        p1, p2 = paths.cache_path(tmp, "search", {"q": 1}), paths.cache_path(tmp, "search", {"q": 1})
        assert p1 == p2 and p1 != paths.cache_path(tmp, "search", {"q": 2}) and paths.cache_get(p1) is None
        paths.cache_put(p1, {"results": []})
        assert paths.cache_get(p1) == {"results": []}
        # DATA_DIR moves every data path, the caches and the budget ledger included
        code = ("import paths, llm, nimble_client, menus; print(paths.RESTAURANTS_PATH, llm.CACHE_DIR, "
                "nimble_client.CACHE_DIR, nimble_client.USAGE_PATH, menus.MENUS_DIR)")
        env = {k: v for k, v in os.environ.items() if k not in ("LLM_CACHE_DIR", "NIMBLE_CACHE_DIR", "NIMBLE_USAGE_PATH")}
        out = subprocess.run([sys.executable, "-c", code], cwd=HERE, env={**env, "DATA_DIR": str(tmp)},
                             capture_output=True, text=True, check=True).stdout.split()
        assert all(o.startswith(str(tmp)) for o in out), out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_validate_self_label_check():
    import validate
    assert validate.self_label_check("Yun Nan Flavour Garden", "yunnan")["status"] == "agrees"
    assert validate.self_label_check("Sichuan Garden", "yunnan")["status"] == "conflicts"
    assert validate.self_label_check("Golden Palace", "yunnan")["status"] == "no_label"


# ------------------------------------------------------------------ llm.py
def test_parse_json():
    import llm
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}


def test_fallback_and_cache():
    import llm
    calls = []

    def broke(*a):
        calls.append("anthropic"); raise RuntimeError("Your credit balance is too low")

    def ok(*a):
        calls.append("gemini"); return {"x": 1}

    saved = (dict(llm.CALLERS), llm.CACHE_DIR, llm.CONFIG)
    tmp = Path(tempfile.mkdtemp())
    try:
        llm.CALLERS.update(anthropic=broke, gemini=ok)
        llm.CACHE_DIR = tmp
        llm.CONFIG = {"providers": ["anthropic", "gemini"], "anthropic_api_key": "test", "gemini_api_key": "test"}
        assert llm.call_llm("hi", schema={"type": "object"}) == {"x": 1}
        assert calls == ["anthropic", "gemini"], calls      # credit error -> no retries, straight to Gemini
        calls.clear()
        assert llm.call_llm("hi", schema={"type": "object"}) == {"x": 1}
        assert calls == [], calls                           # served from cache, no API calls
    finally:
        llm.CALLERS.clear(); llm.CALLERS.update(saved[0]); llm.CACHE_DIR = saved[1]; llm.CONFIG = saved[2]
        shutil.rmtree(tmp, ignore_errors=True)


def test_llm_provider_order_and_openai():
    import llm
    saved = llm.CONFIG
    try:
        llm.CONFIG = {"providers": ["anthropic", "gemini", "openai"], "openai_api_key": "x", "anthropic_api_key": "y",
                      "gemini_api_key": ""}
        assert llm._providers() == ["anthropic", "openai"]                 # keyless gemini skipped
        llm.CONFIG["providers"] = ["openai", "anthropic", "gemini"]
        assert llm._providers() == ["openai", "anthropic"]
        llm.CONFIG["provider"] = "anthropic"
        assert llm._providers() == ["anthropic", "openai"]
        llm.CONFIG["providers"] = ["openai", "mistral"]
        try:
            llm._providers(); assert False, "expected LLMError"
        except llm.LLMError as e:
            assert "mistral" in str(e)

        # the OpenAI caller sends a JSON-schema response_format and parses the reply
        sent = {}

        class FakeCompletions:
            def create(self, **kw):
                sent.update(kw)
                msg = type("M", (), {"content": '{"primary": "yunnan"}'})()
                return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()

        class FakeOpenAI:
            def __init__(self, *a, **k):
                sent["api_key"] = k.get("api_key")
                self.chat = type("Chat", (), {"completions": FakeCompletions()})()
        import openai
        with patched(openai, OpenAI=FakeOpenAI):
            out = llm._call_openai("menu", {"type": "object"}, "sys", 0.0)
        assert out == {"primary": "yunnan"}
        assert sent["response_format"]["type"] == "json_schema" and "temperature" not in sent
        assert sent["messages"][0] == {"role": "system", "content": "sys"} and sent["api_key"] == "x"
    finally:
        llm.CONFIG = saved


# ------------------------------------------------------------------ adjudicate.py
def _mock_call_llm(module, reply):
    seen = {}

    def fake(prompt, schema=None, system=None, **kw):
        seen["prompt"] = prompt
        return dict(reply)
    original = module.call_llm
    module.call_llm = fake
    return seen, original


def test_adjudicate_candidates_are_scored_regions():
    import adjudicate
    r = classify_file("dongbei.txt")
    ids = adjudicate.candidate_ids(r, M)
    expected = [pid for pid, s in r["scores"].items() if s > 0 and pid != GENERIC][:adjudicate.MAX_CANDIDATES]
    if len(expected) >= adjudicate.MIN_SCORED:
        assert ids == expected
    assert ids[0] == "dongbei"
    assert GENERIC not in ids


def test_adjudicate_offers_all_regions_when_few_scored():
    import adjudicate
    r = M.classify("Coffee\nTea")
    ids = adjudicate.candidate_ids(r, M)
    assert set(ids) == set(M.regions) - {GENERIC}


def test_adjudicate_invalid_id_becomes_unknown():
    import adjudicate
    reply = {"primary": "atlantis", "confidence": 0.9, "is_specialist": True, "evidence": [], "notes": ""}
    seen, original = _mock_call_llm(adjudicate, reply)
    try:
        out = adjudicate.adjudicate("锅包肉\n地三鲜", M.classify("锅包肉\n地三鲜"), M)
        assert out["primary"] == UNKNOWN and "atlantis" in out["notes"]
        assert '"dongbei"' in seen["prompt"]
    finally:
        adjudicate.call_llm = original


def test_final_label():
    import adjudicate
    confident = classify_file("xian.txt")
    assert adjudicate.final_label(confident)["label"] == "shaanxi"
    assert adjudicate.final_label(confident)["source"] == "matcher"
    verdict = {"primary": "yunnan", "is_specialist": True, "confidence": 0.7}
    assert adjudicate.final_label(confident, verdict)["source"] == "matcher"     # matcher didn't ask: verdict ignored
    unsure = M.classify("Coffee\nTea")
    out = adjudicate.final_label(unsure, verdict)
    assert out == {"label": "yunnan", "is_specialist": True, "source": "adjudicator", "confidence": 0.7}
    assert adjudicate.final_label(unsure, None)["label"] == UNKNOWN


# ------------------------------------------------------------------ pipeline.py / score_gold
def test_score_gold():
    import validate
    gold = [{"name": "A", "region": "yunnan"}, {"name": "B", "name_zh": "云之南", "region": "yunnan"},
            {"name": "C", "region": "fujian"}, {"name": "D", "region": "yunnan"}]
    results = [{"name": "A", "label": "yunnan", "label_source": "matcher"},
               {"name": "Bee", "name_zh": "云之南", "label": "sichuan_chongqing", "label_source": "matcher"},
               {"name": "C", "label": UNKNOWN, "label_source": "no menu"}]
    s = validate.score_gold(results, gold)
    assert (s["n"], s["correct"], s["accuracy"]) == (2, 1, 0.5)
    assert s["missing"] == ["D"] and s["no_menu"] == ["C"]
    assert s["confusion"] == {"yunnan": {"yunnan": 1, "sichuan_chongqing": 1}}


def test_pipeline_strips_own_name():
    """A restaurant's name can't classify it: the header is stripped, priced dishes still count."""
    import pipeline
    named_after_dish = {"id": "g", "name": "Guilin Rice Noodle", "name_zh": "桂林米粉"}
    takeout = (HERE / "test_menus" / "takeout.txt").read_text(encoding="utf-8")
    header_only = pipeline.classify_restaurant(named_after_dish, "桂林米粉 Guilin Rice Noodle\n" + takeout, {}, M, False)
    assert header_only["label"] != "guangxi"                                  # the name alone no longer counts
    with_dish = pipeline.classify_restaurant(named_after_dish, "桂林米粉 Guilin Rice Noodle\n桂林米粉 Guilin Rice Noodle $9.95\n"
                                             "螺蛳粉 Luosifen $12\n老友粉 Laoyou noodles $11", {}, M, False)
    assert with_dish["label"] == "guangxi"                                    # the dish on the menu still does
    assert with_dish["self_label"]["status"] == "agrees"


def test_pipeline_end_to_end_offline():
    import pipeline
    tmp = Path(tempfile.mkdtemp())
    try:
        menus_dir = tmp / "menus"; menus_dir.mkdir()
        restaurants = [
            {"id": "y", "name": "Yun Nan Flavour Garden", "neighborhood": "Sunset Park", "lat": 40.64, "lng": -74.0},
            {"id": "x", "name": "Some Noodle Bar"},
            {"id": "t", "name": "Golden Palace"},
            {"id": "n", "name": "South of the Clouds", "name_zh": "云之南"},           # no menu fetched
        ]
        (tmp / "restaurants.json").write_text(json.dumps(restaurants), encoding="utf-8")
        for rid, f in (("y", "yunnan.txt"), ("x", "xian.txt"), ("t", "takeout.txt")):
            shutil.copy(HERE / "test_menus" / f, menus_dir / f"{rid}.txt")
        (menus_dir / "index.json").write_text(json.dumps({"y": {"menu_quality": "full", "menu_source_url": "u"}}))
        gold = {"restaurants": [{"name": "Yun Nan Flavour Garden", "region": "yunnan"},
                                {"name": "South of the Clouds", "name_zh": "云之南", "region": "yunnan"}]}
        (tmp / "gold.json").write_text(json.dumps(gold), encoding="utf-8")
        out = pipeline.run(use_llm=False, restaurants_path=tmp / "restaurants.json", menus_dir=menus_dir,
                           out_path=tmp / "results.json", gold_path=tmp / "gold.json")
        by_id = {r["id"]: r for r in out["restaurants"]}
        assert by_id["y"]["label"] == "yunnan" and by_id["y"]["self_label"]["status"] == "agrees"
        assert "Han Yunnan" in by_id["y"]["sections"] and by_id["y"]["menu_quality"] == "full"
        assert by_id["x"]["label"] == "shaanxi" and by_id["x"]["self_label"]["status"] == "no_label"
        assert by_id["t"]["label"] == GENERIC
        assert by_id["n"]["label"] == UNKNOWN and by_id["n"]["label_source"] == "no menu"
        assert out["summary"]["regional"] == 2 and out["summary"]["specialists"] == 2
        assert out["gold"]["correct"] == 1 and out["gold"]["no_menu"] == ["South of the Clouds"]
        assert json.loads((tmp / "results.json").read_text(encoding="utf-8")) == out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_build_map():
    import build_map
    results = {"generated_at": "t", "summary": {"with_menu": 1, "specialists": 1}, "gold": {"n": 0, "correct": 0},
               "restaurants": [{"id": "a", "name": "Evil </script><script>alert(1)</script>", "label": "yunnan",
                                "label_name": "Yunnan", "lat": 40.7, "lng": -73.9, "is_specialist": True,
                                "evidence": [{"menu_line": "过桥米线", "region": "yunnan", "dish_zh": "过桥米线"},
                                             {"menu_line": "Kung Pao", "region": "sichuan_chongqing"}],
                                "scores": {"yunnan": 3.0}}]}
    page = build_map.render(results)
    assert "__DATA__" not in page and page.count("</script>") == 3          # the name can't close the data tag
    data = json.loads(page.split('<script id="data" type="application/json">')[1].split("</script>")[0].replace("<\\/", "</"))
    row = data["restaurants"][0]
    assert row["name"].startswith("Evil") and "scores" not in row          # page gets display fields only
    assert [e["menu_line"] for e in row["evidence"]] == ["过桥米线"]          # only evidence for the chosen label
    assert "cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/" in page


# ------------------------------------------------------------------ discover.py
def test_extract_restaurants():
    import discover
    reply = {"restaurants": [{"name": "Yun Nan Flavour Garden", "source_url": "https://x", "evidence": "Sunset Park"}]}
    seen, original = _mock_call_llm(discover, reply)
    try:
        out = discover.extract_restaurants([{"title": "Yun Nan Flavour Garden", "url": "https://x"}], "New York")
        assert out == reply["restaurants"] and "New York" in seen["prompt"]
    finally:
        discover.call_llm = original


# ------------------------------------------------------------------ Nimble: nimble_client.py / discover.py / menus.py
from contextlib import contextmanager


@contextmanager
def patched(obj, **attrs):
    """Temporarily replace attributes on a module or object."""
    saved = {k: getattr(obj, k) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(obj, k, v)


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, json.dumps(data)

    def json(self):
        return self._data


def test_nimble_client_cache_retry_and_errors():
    import nimble_client as nc
    tmp = Path(tempfile.mkdtemp())
    calls = []
    replies = [_Resp(429, {"msg": "Rate limit exceeded"}), _Resp(200, {"results": [{"title": "A", "url": "u"}]})]

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers)); return replies.pop(0)
    saved = nc.CONFIG
    nc.CONFIG = {"api_key": "test-key", "budget": "unlimited"}
    try:
        with patched(nc, CACHE_DIR=tmp, USAGE_PATH=tmp / "usage.json"), patched(nc.requests, post=fake_post), \
             patched(nc.time, sleep=lambda s: None):
            assert nc.search("过桥米线 Flushing", max_results=5) == [{"title": "A", "url": "u"}]
            assert len(calls) == 2                                          # retried after the 429
            assert calls[0][1] == {"query": "过桥米线 Flushing", "max_results": 5, "country": "US"}
            assert calls[0][2]["Authorization"] == "Bearer test-key"
            assert nc.search("过桥米线 Flushing", max_results=5) == [{"title": "A", "url": "u"}]
            assert len(calls) == 2                                          # second call came from the cache
            replies[:] = [_Resp(401, {"msg": "bad key"})]
            try:
                nc.search("something else"); assert False, "expected NimbleError"
            except nc.NimbleError as e:
                assert "401" in str(e) and len(calls) == 3                 # no retries on auth errors
        nc.CONFIG = {"api_key": "", "budget": "unlimited"}                 # as left by secrets-example.toml
        with patched(nc, CACHE_DIR=tmp, USAGE_PATH=tmp / "usage.json"):
            try:
                nc.search("uncached query"); assert False, "expected NimbleError"
            except nc.NimbleError as e:
                assert "api_key" in str(e)
    finally:
        nc.CONFIG = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_nimble_budget():
    import nimble_client as nc
    tmp = Path(tempfile.mkdtemp())
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json["query"]); return _Resp(200, {"results": []})
    saved = nc.CONFIG
    nc.CONFIG = {"api_key": "test-key"}
    try:
        with patched(nc, CACHE_DIR=tmp, USAGE_PATH=tmp / "usage.json"), patched(nc.requests, post=fake_post):
            try:                                                   # no budget set: live calls refused
                nc.search("q0"); assert False, "expected NimbleBudgetError"
            except nc.NimbleBudgetError:
                assert sent == []
            nc.CONFIG["budget"] = 2
            nc.search("q1"); nc.search("q2")
            assert sent == ["q1", "q2"] and nc.used() == 2 and nc.remaining() == 0
            nc.search("q1")                                        # cached: free even with the budget used up
            try:
                nc.search("q3"); assert False, "expected NimbleBudgetError"
            except nc.NimbleBudgetError:
                assert sent == ["q1", "q2"]                        # refused before sending
            ledger = json.loads((tmp / "usage.json").read_text(encoding="utf-8"))
            assert [e["target"] for e in ledger["log"]] == ["q1", "q2"]
            nc.CONFIG["budget"] = 3                                # the ledger persists; raising the budget allows 1 more
            nc.search("q3")
            assert sent == ["q1", "q2", "q3"] and nc.used() == 3
    finally:
        nc.CONFIG = saved
        shutil.rmtree(tmp, ignore_errors=True)


def test_discover_build_queries():
    import discover
    qs = discover.build_queries(["Flushing"], regions=["yunnan"], dishes_per_region=2, seed_names=["Seed Place"])
    kinds = [q["kind"] for q in qs]
    assert kinds == ["dish", "dish", "self_label", "sweep", "seed"], kinds
    assert qs[0]["query"] == "过桥米线 Flushing" and qs[2]["query"] == "Yunnan restaurant Flushing"
    assert qs[3]["focus"] == "location" and qs[3]["search_depth"] == "lite"
    all_qs = discover.build_queries(["Flushing"])
    assert not any(q.get("region") == GENERIC for q in all_qs)


def test_discover_merge():
    import discover
    found = [
        {"name": "Yun Nan Flavour Garden", "address": "5121 8th Ave, Brooklyn", "source_url": "a", "evidence": "x"},
        {"name": "Yun Nan Flavour Garden", "address": None, "name_zh": "云南味道", "source_url": "b", "evidence": "y"},
        {"name": "Yun Nan Flavour Garden", "address": "99 Main St", "source_url": "c", "evidence": "z"},   # other branch
        {"name": "Golden Palace", "source_url": "a", "evidence": "w"},
        {"name": "", "source_url": "a", "evidence": "skip me"},
    ]
    by_url = {"a": [{"query": "q1", "kind": "dish", "region": "yunnan"}], "b": [{"query": "q2", "kind": "sweep"}]}
    out = discover.merge(found, by_url)
    assert [r["name"] for r in out] == ["Yun Nan Flavour Garden", "Yun Nan Flavour Garden", "Golden Palace"]
    first = out[0]
    assert first["sources"] == ["a", "b"] and first["name_zh"] == "云南味道" and first["address"].startswith("5121")
    assert [v["query"] for v in first["discovered_via"]] == ["q1", "q2"]
    assert out[0]["id"] != out[1]["id"] and out[0]["id"] == discover.restaurant_id(out[0])


def test_discover_end_to_end_mocked():
    import discover
    tmp = Path(tempfile.mkdtemp())

    def fake_search(query, **opts):
        return [{"title": f"Result for {query}", "url": f"https://x/{len(query)}", "description": "", "content": ""}]

    def fake_llm(prompt, schema=None, system=None, **kw):
        return {"restaurants": [{"name": "South of the Clouds", "address": "175 Sullivan St",
                                 "source_url": "https://x/12", "evidence": "Yunnan spot"}]}
    try:
        with patched(discover.nimble_client, search=fake_search), patched(discover, call_llm=fake_llm):
            out = discover.discover("New York", ["Flushing"], regions=["yunnan"], out_path=tmp / "r.json")
        saved = json.loads((tmp / "r.json").read_text(encoding="utf-8"))
        assert saved == out and len(out) == 1 and out[0]["name"] == "South of the Clouds"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_discover_seed_from_gold():
    import discover
    tmp = Path(tempfile.mkdtemp())
    try:
        out_path = tmp / "r.json"
        out_path.write_text(json.dumps([{"id": "x", "name": "Golden Palace", "address": None, "name_zh": None}]))
        calls = []
        with patched(discover.nimble_client, search=lambda *a, **k: calls.append(a) or []):
            out = discover.seed_from_gold(out_path)
            again = discover.seed_from_gold(out_path)                     # idempotent
        gold = json.loads((HERE / "gold_set.json").read_text(encoding="utf-8"))["restaurants"]
        assert calls == [] and len(out) == 1 + len(gold) and len(again) == len(out)
        assert out[0]["name"] == "Golden Palace"                           # existing records kept
        assert all("region" not in r and "gold_region" not in r for r in out)   # label not leaked
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_discover_keeps_existing_restaurants_and_ids():
    """The README plan: --from-gold, then discovery. Gold restaurants must survive, with the same ids."""
    import discover
    tmp = Path(tempfile.mkdtemp())
    try:
        out_path = tmp / "r.json"
        seeded = discover.seed_from_gold(out_path)
        gold_ids = {r["name"]: r["id"] for r in seeded}

        def fake_search(query, **opts):
            return [{"title": "x", "url": "https://x/1", "description": "", "content": ""}]

        def fake_llm(prompt, schema=None, system=None, **kw):              # one gold place (now with an address) + one new
            return {"restaurants": [
                {"name": "Yunnan Rice Noodle House", "address": "136-20 Roosevelt Ave", "source_url": "https://x/1", "evidence": "e"},
                {"name": "Golden Palace", "address": "1 Main St", "source_url": "https://x/1", "evidence": "e"}]}
        with patched(discover.nimble_client, search=fake_search), patched(discover, call_llm=fake_llm):
            out = discover.discover("New York", ["Flushing"], regions=["yunnan"], out_path=out_path)
        by_name = {r["name"]: r for r in out}
        assert set(gold_ids) <= set(by_name) and "Golden Palace" in by_name       # gold kept, new one added
        assert all(by_name[n]["id"] == i for n, i in gold_ids.items())            # ids unchanged
        house = by_name["Yunnan Rice Noodle House"]
        assert house["address"] == "136-20 Roosevelt Ave"                         # gap filled
        assert {v["kind"] for v in house["discovered_via"]} >= {"gold", "dish"}
        assert len(out) == len(seeded) + 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_discover_stops_on_budget():
    import discover, nimble_client as nc
    seen = []

    def fake_search(query, **opts):
        seen.append(query)
        if len(seen) == 2:
            raise nc.NimbleBudgetError("used up")
        return []
    with patched(discover.nimble_client, search=fake_search):
        discover.run_searches([{"query": f"q{i}", "kind": "dish"} for i in range(5)])
    assert seen == ["q0", "q1"]


MENU_TEXT = "\n".join(f"{i}. Dish number {i}  $12.95" for i in range(20))


def test_menus_priority():
    import menus
    rs = [{"id": "s", "discovered_via": [{"kind": "sweep"}]}, {"id": "d", "discovered_via": [{"kind": "dish"}]},
          {"id": "n"}, {"id": "g", "discovered_via": [{"kind": "gold"}]},
          {"id": "both", "discovered_via": [{"kind": "sweep"}, {"kind": "self_label"}]}]
    assert [r["id"] for r in sorted(rs, key=menus.priority)] == ["g", "d", "both", "s", "n"]


def test_menus_search_only_limit_budget_and_errors():
    import menus, nimble_client as nc
    tmp = Path(tempfile.mkdtemp())
    rs = [{"id": f"r{i}", "name": f"R{i}", "website": "https://r.com"} for i in range(4)]
    try:
        with patched(menus, MENUS_DIR=tmp, INDEX_PATH=tmp / "index.json"):
            extracts = []
            with patched(menus.nimble_client, search=lambda q, **k: [],
                         extract=lambda url, **k: extracts.append(url) or {"status": "failed", "url": url, "markdown": "", "links": []}):
                menus.fetch_all(rs[:1], "NY", search_only=True)
            assert extracts == []                                          # search-only: no Extract
            n = []
            with patched(menus.nimble_client, search=lambda q, **k: n.append(q) or []):
                menus.fetch_all(rs, "NY", search_only=True, limit=2)
            assert len(n) == 2                                             # limit counts restaurants fetched this run

            def failing(q, **k):
                raise nc.NimbleError("HTTP 500")
            with patched(menus.nimble_client, search=failing):
                index = menus.fetch_all(rs, "NY", search_only=True)
            assert "r3" not in index                                       # errors aren't indexed: retried later

            def budget_out(q, **k):
                raise nc.NimbleBudgetError("used up")
            with patched(menus.nimble_client, search=budget_out):
                index = menus.fetch_all(rs, "NY", search_only=True)
            assert "r3" not in index                                       # budget stop isn't recorded as "none"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_menus_prefers_own_site_then_delivery():
    import menus
    results = [
        {"url": "https://www.grubhub.com/r/place", "content": MENU_TEXT + "\n" + MENU_TEXT},   # more lines
        {"url": "https://placeyunnan.com/menu", "content": MENU_TEXT},                         # own site
        {"url": "https://placeyunnan.com/menu.pdf", "content": MENU_TEXT * 3},                 # skipped
    ]
    r = {"id": "p", "name": "Place", "website": "https://www.placeyunnan.com"}
    with patched(menus.nimble_client, search=lambda q, **k: results):
        m = menus.fetch_menu(r, "New York")
    assert m["menu_quality"] == "full" and m["menu_source_url"] == "https://placeyunnan.com/menu"


def test_menus_extract_follows_menu_link_then_partial():
    import menus
    r = {"id": "p", "name": "Place", "website": "https://place.com"}
    pages = {"https://place.com": {"status": "success", "url": "https://place.com", "markdown": "Welcome! 云南菜",
                                   "links": ["/about", "/our-menu"]},
             "https://place.com/our-menu": {"status": "success", "url": "https://place.com/our-menu",
                                            "markdown": MENU_TEXT, "links": []}}
    with patched(menus.nimble_client, search=lambda q, **k: [], extract=lambda url, **k: pages[url]):
        m = menus.fetch_menu(r, "New York")
    assert m["menu_quality"] == "full" and m["menu_source_url"] == "https://place.com/our-menu"
    review = [{"url": "https://yelp.com/biz/place", "content": "Loved the 过桥米线 and 汽锅鸡"}]
    with patched(menus.nimble_client, search=lambda q, **k: review):
        m = menus.fetch_menu({"id": "p", "name": "Place"}, "New York")
    assert m["menu_quality"] == "partial" and m["n_menu_lines"] == 1
    with patched(menus.nimble_client, search=lambda q, **k: []):
        assert menus.fetch_menu({"id": "p", "name": "Place"}, "New York")["menu_quality"] == "none"


def test_menus_fetch_all_writes_files_and_index():
    import menus
    tmp = Path(tempfile.mkdtemp())
    try:
        with patched(menus, MENUS_DIR=tmp, INDEX_PATH=tmp / "index.json"), \
             patched(menus.nimble_client, search=lambda q, **k: [{"url": "https://grubhub.com/x", "content": MENU_TEXT}]):
            index = menus.fetch_all([{"id": "abc", "name": "Place"}, {"id": "none1", "name": "Empty"}], "New York")
            assert (tmp / "abc.txt").read_text(encoding="utf-8") == MENU_TEXT       # raw, unnormalized
            assert index["abc"]["menu_quality"] == "full"
            calls = []
            with patched(menus, fetch_menu=lambda r, c: calls.append(r["id"]) or {}):
                menus.fetch_all([{"id": "abc", "name": "Place"}], "New York")
            assert calls == []                                                       # already in index: skipped
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ structure
def _imports(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_llm_has_no_pipeline_imports():
    assert not _imports(HERE / "llm.py") & {"match", "discover", "adjudicate", "validate", "text"}


def test_validate_does_not_import_match():
    assert "match" not in _imports(HERE / "validate.py")


def test_only_interface_reads_taxonomy():
    # build_taxonomy.py writes taxonomy.json; everything else must go through taxonomy_interface.py
    for f in HERE.glob("*.py"):
        if f.name in ("taxonomy_interface.py", "build_taxonomy.py", "test.py"):
            continue
        assert 'with_name("taxonomy.json")' not in f.read_text(encoding="utf-8"), f.name


def test_only_paths_defines_data_locations():
    for f in HERE.glob("*.py"):
        if f.name in ("paths.py", "test.py"):
            continue
        src = f.read_text(encoding="utf-8")
        for needle in ('with_name("data")', '"DATA_DIR"', 'with_name("gold_set.json")', "hashlib.sha256"):
            assert needle not in src, f"{f.name} contains {needle!r}"


def test_only_nimble_client_calls_nimble():
    for f in HERE.glob("*.py"):
        if f.name in ("nimble_client.py", "test.py"):
            continue
        assert "sdk.nimbleway.com" not in f.read_text(encoding="utf-8"), f.name


def test_only_text_py_normalizes():
    for f in HERE.glob("*.py"):
        if f.name in ("text.py", "test.py"):
            continue
        src = f.read_text(encoding="utf-8")
        for needle in ("OpenCC(", "zhconv", r"㐀", "㐀"):
            assert needle not in src, f"{f.name} contains {needle!r}"


# ------------------------------------------------------------------ runner
if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn(); print(f"PASS  {name}")
        except Exception:
            failed += 1; print(f"FAIL  {name}"); traceback.print_exc(limit=2)
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
