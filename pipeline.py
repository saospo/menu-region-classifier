"""pipeline.py: classify every restaurant that has a fetched menu, and write data/results.json.

    python pipeline.py                  # matcher only: free, offline, deterministic
    python pipeline.py --llm            # also ask the LLM adjudicator when the matcher isn't confident (needs_llm)

For each restaurant in data/restaurants.json:
  1. menu      data/menus/{id}.txt (from menus.py). No menu -> label "unknown", label_source "no menu".
  2. strip     text.strip_names removes the restaurant's own name from unpriced menu lines (titles, headers),
               so the name can't count as evidence. Classification looks at the menu, not the name.
  3. match     match.Matcher.classify on the stripped menu text.
  4. adjudicate  only with --llm and only when the matcher set needs_llm; it sees the same stripped text.
               An LLM failure falls back to the matcher.
  5. final     adjudicate.final_label (SPEC.md §5b).
  6. validate  validate.self_label_check: does the restaurant's own name agree with the predicted region?
               (Run once, after the label is known. For a restaurant with no menu it's "unchecked" or "no_label".)
Then the gold-set score (validate.score_gold) is computed and saved alongside the results.

Output data/results.json: {"generated_at", "summary", "gold", "restaurants": [one record per restaurant]}.
"""
from __future__ import annotations
import argparse, sys
from datetime import datetime, timezone
from pathlib import Path

import paths
import validate
from adjudicate import adjudicate, final_label
from llm import LLMError
from match import Matcher
from taxonomy_interface import UNKNOWN, GENERIC, label_name
from text import strip_names

EVIDENCE_KEEP = 12


def classify_restaurant(r: dict, menu_text: str | None, menu_meta: dict, matcher: Matcher, use_llm: bool) -> dict:
    base = {k: r.get(k) for k in ("id", "name", "name_zh", "address", "neighborhood", "website", "lat", "lng")}
    base.update(menu_quality=menu_meta.get("menu_quality", "none"), menu_source_url=menu_meta.get("menu_source_url"))
    own_name = " ".join(x for x in (r.get("name_zh"), r.get("name")) if x)

    if not menu_text:
        checked = validate.self_label_check(own_name, UNKNOWN)
        status = "unchecked" if checked["self_labels"] else "no_label"      # nothing to agree or disagree with
        return {**base, "label": UNKNOWN, "label_name": label_name(UNKNOWN, matcher.tax), "label_source": "no menu",
                "is_specialist": False, "confidence": None, "top3": [], "evidence": [], "sections": [],
                "self_label": {"status": status, "self_labels": checked["self_labels"]}, "adjudicator": None}

    text = strip_names(menu_text, [r.get("name"), r.get("name_zh")])
    m = matcher.classify(text)
    verdict = None
    if use_llm and m["needs_llm"]:
        try:
            verdict = adjudicate(text, m, matcher)
        except LLMError as e:
            print(f"  adjudicator failed for {r['name']}: {e}", file=sys.stderr)
    final = final_label(m, verdict)
    label = final["label"]
    checked = validate.self_label_check(own_name, label)
    sections = sorted({e["section"] for e in m["evidence"] if e["region"] == label and e.get("section")})
    return {**base, "label": label, "label_name": label_name(label, matcher.tax), "label_source": final["source"],
            "is_specialist": final["is_specialist"], "confidence": final["confidence"],
            "matcher_label": m["label"], "needs_llm": m["needs_llm"], "top3": m["top3"],
            "generic_score": m["generic_score"], "margin": m["margin"],
            "evidence": m["evidence"][:EVIDENCE_KEEP], "markers": m["markers"], "sections": sections,
            "self_label": {"status": checked["status"], "self_labels": checked["self_labels"]},
            "adjudicator": verdict}


def run(use_llm: bool = False, restaurants_path: Path = paths.RESTAURANTS_PATH, menus_dir: Path = paths.MENUS_DIR,
        out_path: Path = paths.RESULTS_PATH, gold_path: Path = paths.GOLD_PATH) -> dict:
    restaurants = paths.read_json(restaurants_path)
    index = paths.read_json(Path(menus_dir) / "index.json", {})
    matcher = Matcher()
    results = []
    for r in restaurants:
        menu_file = Path(menus_dir) / f'{r["id"]}.txt'
        text = menu_file.read_text(encoding="utf-8") if menu_file.exists() else None
        results.append(classify_restaurant(r, text, index.get(r["id"], {}), matcher, use_llm))

    with_menu = [x for x in results if x["label_source"] != "no menu"]
    regional = [x for x in with_menu if x["label"] not in (GENERIC, UNKNOWN)]
    summary = {"restaurants": len(results), "with_menu": len(with_menu), "regional": len(regional),
               "specialists": sum(1 for x in regional if x["is_specialist"]),
               "generic": sum(1 for x in with_menu if x["label"] == GENERIC),
               "unknown": sum(1 for x in results if x["label"] == UNKNOWN),
               "adjudicated": sum(1 for x in results if x["label_source"] == "adjudicator"),
               "by_region": {}}
    for x in regional:
        summary["by_region"][x["label"]] = summary["by_region"].get(x["label"], 0) + 1
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "summary": summary,
           "gold": validate.score_gold(results, paths.load_gold(gold_path)), "restaurants": results}
    paths.write_json(out_path, out)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm", action="store_true", help="use the LLM adjudicator for menus the matcher isn't sure about")
    a = ap.parse_args()
    out = run(use_llm=a.llm)
    s, g = out["summary"], out["gold"]
    print(f'{s["restaurants"]} restaurants, {s["with_menu"]} with menus: {s["regional"]} regional '
          f'({s["specialists"]} specialists), {s["generic"]} generic, {s["unknown"]} unknown -> {paths.RESULTS_PATH}')
    for x in out["restaurants"]:
        print(f'  {x["label_name"]:<28} {x["label_source"]:<11} {x["menu_quality"]:<7} {x["name"]}')
    if g["n"]:
        print(f'gold set: {g["correct"]}/{g["n"]} correct')
        for row in g["rows"]:
            print(f'  {"✓" if row["correct"] else "✗"} {row["name"]}: gold {row["gold"]}, predicted {row["predicted"]}')
    if g["missing"]:
        print(f'gold restaurants without results: {", ".join(g["missing"])}')
    if g["no_menu"]:
        print(f'gold restaurants without a menu (not scored): {", ".join(g["no_menu"])}')


if __name__ == "__main__":
    main()
