"""taxonomy_interface.py: the one place that reads taxonomy.json and pulls lookup data out of it.

    LABELS, GENERIC, UNKNOWN   -> the output labels that aren't regions (generic, unknown)
    load_taxonomy(path)        -> the parsed taxonomy.json (read once per path, then cached)
    self_label_terms(tax)      -> [(normalized self-label, region id)], one-character Chinese labels dropped
    find_self_labels(text, terms) -> [{"region": region id, "labels": [labels found]}] for a restaurant name
    signature_dishes(region, n)   -> a region's n most distinctive dishes (highest specificity first)
    label_name(label, tax)     -> display name for a region id, "generic" or "unknown"

validate.py uses the restaurant-name lookup; classification (match.py) deliberately doesn't look at names.
build_taxonomy.py writes taxonomy.json and doesn't use this module.

The cached taxonomy is shared between callers: treat it as read-only.
"""
from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path

from text import norm, is_single_cjk, find_terms

TAXONOMY_PATH = Path(__file__).with_name("taxonomy.json")

# Output labels other than regions. Regions come from taxonomy.json; these are the only other values a
# classifier may return. "generic" is also a region in taxonomy.json, where its takeout dishes are scored.
LABELS = {
    "generic": {
        "id": "generic",
        "name": "Generic / Pan-Chinese",
        "description": "A standard pan-Chinese or American-Chinese menu with no regional center.",
    },
    "unknown": {
        "id": "unknown",
        "name": "Unknown",
        "description": "Not enough evidence on the menu to assign a region or call it generic.",
    },
}
GENERIC = LABELS["generic"]["id"]
UNKNOWN = LABELS["unknown"]["id"]


@lru_cache(maxsize=None)
def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_taxonomy(path: str | Path = TAXONOMY_PATH) -> dict:
    """The parsed taxonomy.json. Read from disk once per path; later calls return the same object."""
    return _load(str(Path(path).resolve()))


def self_label_terms(tax: dict) -> list[tuple[str, str]]:
    """Every region's self-labels as (normalized term, region id) pairs, ready for find_terms.
    One-character Chinese labels (川, 湘, 粤…) are dropped: they'd match far too many names."""
    terms = []
    for p in tax["regions"]:
        for label in p.get("self_labels", []):
            term = norm(label)
            if term and not is_single_cjk(term):
                terms.append((term, p["id"]))
    return terms


def find_self_labels(text: str | None, terms: list[tuple[str, str]]) -> list[dict]:
    """Regions a restaurant names itself after, from its name and/or description (raw text, not normalized)."""
    if not text:
        return []
    found = find_terms(norm(text), terms)
    return [{"region": k, "labels": v} for k, v in found.items()]


def signature_dishes(region: dict, n: int, require_zh: bool = False, min_specificity: float = 0.0) -> list[dict]:
    """A region's n most distinctive dishes, highest specificity first (ties keep taxonomy order).
    require_zh: only dishes with a Chinese name. Used for discovery queries and the adjudicator's candidate list."""
    dishes = [d for d in region["dishes"]
              if d["specificity"] >= min_specificity and (d.get("zh") or not require_zh)]
    return sorted(dishes, key=lambda d: -d["specificity"])[:n]


def label_name(label: str, tax: dict | None = None) -> str:
    """Display name for an output label: a region's name (generic is a region too), or the name of unknown."""
    for p in (tax or load_taxonomy())["regions"]:
        if p["id"] == label:
            return p["name"]
    return LABELS.get(label, {}).get("name", label)
