"""validate.py: independent checks on the classifier's region labels (SPEC.md §7).

Done:
    self_label_check(name, predicted, description=None)
        Does the restaurant name itself after a region, and does that agree with the predicted label?
        -> {"status": "agrees" | "conflicts" | "no_label", "self_labels": [...], "predicted": ...}

    score_gold(results, gold)
        Compare pipeline results with hand-labeled gold_set.json entries, matched by English or Chinese name.
        -> {"n", "correct", "accuracy", "rows": [{name, gold, predicted, correct, menu_quality}], "confusion",
            "missing": [gold restaurants with no result], "no_menu": [gold restaurants without a fetched menu]}

Planned:
    external corroboration via Nimble (press / Reddit mentioning the predicted region), and a combined
    per-restaurant badge (corroborated / menu-only / conflicting). The map already shows the self-label result.

Uses taxonomy_interface.py for the restaurant-name lookup. It does not import match.py, so validation
stays independent of the matcher's scoring.
"""
from __future__ import annotations
from functools import lru_cache

from taxonomy_interface import TAXONOMY_PATH, load_taxonomy, self_label_terms, find_self_labels
from text import name_keys


@lru_cache(maxsize=None)
def _terms(path: str) -> tuple:
    return tuple(self_label_terms(load_taxonomy(path)))


def self_label_check(name: str | None, predicted: str | None, description: str | None = None,
                     taxonomy_path=TAXONOMY_PATH) -> dict:
    """agrees    the restaurant names the predicted region
       conflicts the restaurant names one or more regions, none of them the predicted one
       no_label  the restaurant doesn't name any region (most restaurants; not evidence either way)"""
    text = " ".join(x for x in (name, description) if x)
    found = find_self_labels(text, list(_terms(str(taxonomy_path))))
    regions = [f["region"] for f in found]
    if not regions:
        status = "no_label"
    elif predicted in regions:
        status = "agrees"
    else:
        status = "conflicts"
    return {"status": status, "self_labels": found, "predicted": predicted}


def score_gold(results: list[dict], gold: list[dict]) -> dict:
    """Accuracy of the final region label on the gold restaurants that were classified from a menu.
    Gold restaurants with no result go in "missing"; those with no fetched menu go in "no_menu". Neither is scored
    wrong: they measure discovery and fetching, not classification."""
    rows, missing, no_menu, confusion = [], [], [], {}
    for g in gold:
        if not g.get("name"):
            continue
        keys = name_keys(g.get("name"), g.get("name_zh"))
        r = next((r for r in results if name_keys(r.get("name"), r.get("name_zh")) & keys), None)
        if r is None:
            missing.append(g["name"])
            continue
        if r.get("label_source") == "no menu":
            no_menu.append(g["name"])
            continue
        ok = r["label"] == g["region"]
        rows.append({"name": g["name"], "gold": g["region"], "predicted": r["label"], "correct": ok,
                     "menu_quality": r.get("menu_quality")})
        confusion.setdefault(g["region"], {}).setdefault(r["label"], 0)
        confusion[g["region"]][r["label"]] += 1
    correct = sum(row["correct"] for row in rows)
    return {"n": len(rows), "correct": correct, "accuracy": round(correct / len(rows), 3) if rows else None,
            "rows": rows, "confusion": confusion, "missing": missing, "no_menu": no_menu}
