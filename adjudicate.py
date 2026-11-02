"""adjudicate.py: LLM second opinion for menus where match.py isn't confident (needs_llm=True).

    from adjudicate import adjudicate
    verdict = adjudicate(menu_text, match_result)     # -> {primary, secondary, confidence, is_specialist, evidence, notes}

Candidates shown to the model (Prompt C, region-level):
  * the regions the matcher actually scored, highest first, up to MAX_CANDIDATES (generic excluded; the
    prompt always offers generic/unknown separately)
  * if fewer than MIN_SCORED regions scored, the matcher has little to go on, so every region is offered
Each candidate is listed with its most distinctive dishes. The answer must be a taxonomy region id,
generic or unknown (taxonomy_interface.py); anything else becomes unknown.

final_label(match_result, verdict) applies the rule from SPEC.md §5b: the matcher's label unless the matcher asked
for a second opinion (needs_llm) and one was obtained; both stay in the pipeline's output.
"""
from __future__ import annotations

from llm import call_llm
from match import Matcher
from taxonomy_interface import GENERIC, UNKNOWN, signature_dishes

MAX_CANDIDATES = 6
MIN_SCORED = 3
SIGNATURE_DISHES = 6

ADJUDICATE_SCHEMA = {
    "type": "object",
    "properties": {
        "primary": {"type": "string"},
        "secondary": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "is_specialist": {"type": "boolean"},
        "evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {"menu_item": {"type": "string"}, "dish": {"type": "string"}, "region": {"type": "string"}},
            "required": ["menu_item", "dish", "region"]}},
        "notes": {"type": "string"},
    },
    "required": ["primary", "confidence", "is_specialist", "evidence", "notes"],
}

ADJUDICATE_SYSTEM = ("You classify Chinese restaurants by the regional cuisine their menu actually serves, "
                     "using the taxonomy from Chinese Cooking Demystified's '63 Chinese Cuisines'. "
                     "Judge only from dishes on the menu, not from the restaurant's name or marketing.")

PROMPT_ADJUDICATE = """Classify this restaurant's menu into ONE region id from this list:
{candidates}
Also allowed: "{generic}" (standard pan-Chinese / American-Chinese menu), "{unknown}" (not enough evidence).

Rules:
- Several region-specific dishes are strong evidence. One famous dish (kung pao, mapo tofu, dumplings) is weak.
- Menus in the US use English or pinyin names; recognize dishes by description too.
- is_specialist = the menu is clearly centered on that one region, not a mixed menu with a few regional items.
- evidence: up to 8 menu items that drove the decision.

A rule-based matcher already scored this menu:
{matcher_summary}

MENU:
<<<
{menu}
>>>"""


def candidate_ids(match_result: dict, matcher: Matcher) -> list[str]:
    """Scored regions, best first, up to MAX_CANDIDATES; every region if fewer than MIN_SCORED scored."""
    scored = [pid for pid, s in match_result["scores"].items() if s > 0 and pid != GENERIC]
    if len(scored) >= MIN_SCORED:
        return scored[:MAX_CANDIDATES]
    return [pid for pid in matcher.regions if pid != GENERIC]


def adjudicate(menu_text: str, match_result: dict, matcher: Matcher | None = None, max_chars: int = 8000) -> dict:
    matcher = matcher or Matcher()
    lines = []
    for pid in candidate_ids(match_result, matcher):
        p = matcher.regions[pid]
        sig = signature_dishes(p, SIGNATURE_DISHES)
        lines.append(f'- "{pid}" ({p["name"]}): ' + "; ".join(f'{d["en"]} {d.get("zh") or ""}'.strip() for d in sig))
    summary = ", ".join(f'{t["region"]}={t["score"]}' for t in match_result["top3"]) + \
              f'; {GENERIC}={match_result["generic_score"]}; matched: ' + \
              "; ".join(f'{e["dish_en"]}→{e["region"]}' for e in match_result["evidence"][:8])
    prompt = PROMPT_ADJUDICATE.format(candidates="\n".join(lines), generic=GENERIC, unknown=UNKNOWN,
                                      matcher_summary=summary, menu=menu_text[:max_chars])
    out = call_llm(prompt, schema=ADJUDICATE_SCHEMA, system=ADJUDICATE_SYSTEM)
    valid = set(matcher.regions) | {GENERIC, UNKNOWN}
    if out.get("primary") not in valid:                  # the model must answer with a taxonomy id
        out["notes"] = f'invalid id {out.get("primary")!r} → {UNKNOWN}. ' + out.get("notes", "")
        out["primary"] = UNKNOWN
    return out


def final_label(match_result: dict, verdict: dict | None = None) -> dict:
    """{"label", "is_specialist", "source": "matcher" | "adjudicator", "confidence"}.
    The adjudicator decides only when the matcher set needs_llm and a verdict exists."""
    if verdict and match_result.get("needs_llm"):
        return {"label": verdict["primary"], "is_specialist": bool(verdict.get("is_specialist")),
                "source": "adjudicator", "confidence": verdict.get("confidence")}
    top = match_result["top3"][0]["prob"] if match_result.get("top3") else None
    return {"label": match_result["label"], "is_specialist": match_result["is_specialist"],
            "source": "matcher", "confidence": top if match_result["label"] not in (GENERIC, UNKNOWN) else None}
