"""match.py — deterministic region matcher for Chinese restaurant menus.

Usage:
    python match.py menu.txt [--json]
    from match import Matcher; m = Matcher(); result = m.classify(menu_text)

Classification looks at menu text only, never at the restaurant's name: pipeline.py strips the restaurant's own name
from unpriced menu lines (text.strip_names) before calling classify, and the self-label check lives in validate.py.

How it scores:
  1. Each menu line is normalized with text.norm (opencc traditional -> simplified, lowercase, punctuation to spaces).
  2. Chinese dish names are matched longest-first and consumed, so 东江酿豆腐 isn't also counted as 酿豆腐:
       exact substring           -> strength 1.0
       in-order with ≤2 extras   -> strength 0.8   (金钱鸡蛋 still finds 金钱蛋)
       names in a dish's `exact_only` list (≤2 characters) -> must be a whole Chinese run, strength 1.0
  3. If no Chinese name matched on a line, English and pinyin aliases are tried, longest-first:
       whole-word substring      -> 0.9
       fuzzy                     -> 0.7   (aliases of 8+ characters only: rapidfuzz ratio ≥ 90 between the alias and
                                           a run of the line's words of about the alias's length, ±1 word, so
                                           "crosing the bridge noodle" matches but "yunnan rice noodle" ≠ "hand rice noodle")
  4. Each dish counts once per menu (its best hit). Markers (ingredients and sauces) add 0.15 each, capped at 1.0.
  5. A region's score is the sum of strength × specificity, divided by n_dishes ** 0.25 so big regions
     don't win just by having more dishes, plus the marker bonus.
  6. The result has a ranking, probabilities (over regions that scored), a specialist flag, a needs_llm flag
     and the evidence lines. Output labels other than regions (generic, unknown) come from taxonomy_interface.py.

pip install rapidfuzz opencc-python-reimplemented
"""
from __future__ import annotations
import json, math, re, sys, argparse
from pathlib import Path

from text import norm, cjk_runs, strip_cjk, is_single_cjk, find_terms
from taxonomy_interface import TAXONOMY_PATH, GENERIC, UNKNOWN, load_taxonomy, label_name

try:
    from rapidfuzz import fuzz
except ImportError:
    fuzz = None

# tunables
SUBSEQ_SLACK = 2
FUZZY_MIN = 90
FUZZY_MIN_ALIAS_LEN = 8
MARKER_WEIGHT = 0.15
MARKER_CAP = 1.0
SIZE_EXPONENT = 0.25
SOFTMAX_T = 0.5
MIN_LABEL_SCORE = 0.8        # below this: not enough regional evidence
SPECIALIST_SHARE = 0.6       # top region's share of all regional score
SPECIALIST_MIN = 1.2
LLM_MARGIN = 0.25            # (top - second) / top below this -> ask the adjudicator


def subseq_span(name: str, s: str, slack: int = SUBSEQ_SLACK):
    """Indices in s where name's characters appear in order within len(name)+slack. None if not found."""
    for start, ch in enumerate(s):
        if ch != name[0]:
            continue
        idx, j, k = [start], 1, start + 1
        while j < len(name) and k < len(s) and k - start < len(name) + slack:
            if s[k] == name[j]:
                idx.append(k); j += 1
            k += 1
        if j == len(name):
            return idx
    return None


def fuzzy_word_match(alias: str, latin: str) -> bool:
    """Is some run of the line's words, of about the alias's length (±1 word), a near-match for the alias?
    Comparing word runs, not any substring, keeps a shared tail ("rice noodle") from matching a different dish."""
    a, words = alias.split(), latin.split()
    for n in {max(1, len(a) - 1), len(a), len(a) + 1}:
        for i in range(len(words) - n + 1):
            if fuzz.ratio(alias, " ".join(words[i:i + n])) >= FUZZY_MIN:
                return True
    return False


class Matcher:
    def __init__(self, taxonomy_path: str | Path = TAXONOMY_PATH):
        tax = load_taxonomy(taxonomy_path)
        self.tax = tax
        self.regions = {p["id"]: p for p in tax["regions"]}
        self.zh_index, self.alias_index, self.marker_index = [], [], []
        for p in tax["regions"]:
            for i, d in enumerate(p["dishes"]):
                key = (p["id"], i)
                exact_only = set(d.get("exact_only", []))      # computed by build_taxonomy.py
                for z in ([d["zh"]] if d.get("zh") else []) + d.get("zh_alt", []):
                    self.zh_index.append((z, key, z in exact_only))
                for a in d.get("aliases", []):
                    a2 = norm(a)
                    if a2: self.alias_index.append((a2, key))
            # one-character Chinese markers (酸, 辣, 藏…) would fire on almost every menu, so they're skipped
            for m in p.get("markers", []):
                m2 = norm(m)
                if m2 and not is_single_cjk(m2):
                    self.marker_index.append((m2, p["id"]))
        self.zh_index.sort(key=lambda x: -len(x[0]))
        self.alias_index.sort(key=lambda x: -len(x[0]))
        self.n_dishes = {pid: max(len(p["dishes"]), 1) for pid, p in self.regions.items()}

    def dish(self, key):
        pid, i = key
        return self.regions[pid]["dishes"][i]

    # ---------- line-level matching ----------
    def _match_line(self, raw: str):
        line = norm(raw)
        if not line:
            return []
        hits = []
        runs = cjk_runs(line)
        cjk = "".join(runs)
        buf = list(cjk)                                         # consumed chars become "□"
        for name, key, exact_only in self.zh_index:
            if exact_only:
                if name in runs:
                    hits.append((key, 1.0, name, "zh_exact"))
                    runs = [r for r in runs if r != name]
                continue
            s = "".join(buf)
            pos = s.find(name)
            if pos >= 0:
                hits.append((key, 1.0, name, "zh"))
                for k in range(pos, pos + len(name)): buf[k] = "□"
                continue
            span = subseq_span(name, s)
            if span:
                hits.append((key, 0.8, name, "zh_subseq"))
                for k in span: buf[k] = "□"
        if hits: return hits                                    # a Chinese name matched: don't also try English aliases

        # exact aliases first (longest first, consumed), then fuzzy only for dishes still unmatched, so a longer
        # alias's near-match ("egg rolls") can't pre-empt a shorter exact one ("egg roll") for the same dish
        latin = " " + strip_cjk(line) + " "
        matched = set()
        for alias, key in self.alias_index:
            if key in matched:
                continue
            pat = f" {alias} "
            if pat in latin or f" {alias}s " in latin:
                hits.append((key, 0.9, alias, "alias"))
                matched.add(key)
                latin = latin.replace(pat, " ◇ ").replace(f" {alias}s ", " ◇ ")
        if fuzz:
            for alias, key in self.alias_index:
                if key not in matched and len(alias) >= FUZZY_MIN_ALIAS_LEN and fuzzy_word_match(alias, latin):
                    hits.append((key, 0.7, alias, "alias_fuzzy"))
                    matched.add(key)
        return hits

    # ---------- menu-level ----------
    def classify(self, menu_text: str) -> dict:
        lines = [l for l in re.split(r"[\n\r]+|(?<=\S)\s{3,}(?=\S)", menu_text) if l.strip()]
        best = {}                                               # dish key -> (strength, evidence)
        for raw in lines:
            for key, strength, matched_text, how in self._match_line(raw):
                if key not in best or strength > best[key][0]:
                    best[key] = (strength, {"menu_line": raw.strip()[:160], "matched": matched_text, "how": how})

        markers = find_terms(norm(menu_text), self.marker_index)     # {region: [markers found]}

        raw_scores, evidence = {pid: 0.0 for pid in self.regions}, []
        for key, (strength, ev) in best.items():
            pid = key[0]
            d = self.dish(key)
            w = strength * d["specificity"]
            raw_scores[pid] += w
            evidence.append({**ev, "region": pid, "dish_en": d["en"], "dish_zh": d.get("zh"),
                             "section": d.get("section"), "strength": strength,
                             "specificity": d["specificity"], "weight": round(w, 3)})
        scores = {}
        for pid in self.regions:
            dish_part = raw_scores[pid] / (self.n_dishes[pid] ** SIZE_EXPONENT)
            marker_part = min(MARKER_WEIGHT * len(markers.get(pid, ())), MARKER_CAP)
            scores[pid] = round(dish_part + marker_part, 3)

        regional = {k: v for k, v in scores.items() if k != GENERIC}
        ranked = sorted(regional.items(), key=lambda kv: -kv[1])
        top_id, top = ranked[0]
        second = ranked[1][1] if len(ranked) > 1 else 0.0
        reg_total = sum(regional.values()) or 1e-9
        generic_score = scores.get(GENERIC, 0.0)

        # probabilities only over regions that scored, so zero-score regions don't dilute a clear winner
        exps = {k: math.exp(v / SOFTMAX_T) for k, v in scores.items() if v > 0}
        z = sum(exps.values()) or 1.0
        probs = {k: round(exps[k] / z, 3) if k in exps else 0.0 for k in scores}
        nz = [v / reg_total for v in regional.values() if v > 0]
        entropy = -sum(p * math.log(p) for p in nz) / math.log(len(nz)) if len(nz) > 1 else 0.0

        if top >= MIN_LABEL_SCORE and top >= generic_score:
            label = top_id
        elif generic_score >= MIN_LABEL_SCORE * 0.5 or (top < MIN_LABEL_SCORE and generic_score > top):
            label = GENERIC
        else:
            label = UNKNOWN
        is_specialist = label == top_id and top >= SPECIALIST_MIN and top / reg_total >= SPECIALIST_SHARE
        margin = (top - second) / top if top else 0.0

        return {
            "label": label,
            "label_name": label_name(label, self.tax),
            "is_specialist": is_specialist,
            "needs_llm": label == UNKNOWN or (label != GENERIC and margin < LLM_MARGIN),
            "top3": [{"region": pid, "name": self.regions[pid]["name"], "score": s, "prob": probs[pid]}
                     for pid, s in ranked[:3]],
            "generic_score": generic_score,
            "margin": round(margin, 3),
            "entropy": round(entropy, 3),
            "scores": {k: v for k, v in sorted(scores.items(), key=lambda kv: -kv[1]) if v > 0},
            "markers": {k: sorted(v) for k, v in markers.items()},
            "evidence": sorted(evidence, key=lambda e: -e["weight"]),
            "n_lines": len(lines),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("menu", help="path to menu text file, or - for stdin")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    text = sys.stdin.read() if a.menu == "-" else Path(a.menu).read_text(encoding="utf-8")
    r = Matcher().classify(text)
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2)); return
    print(f"label: {r['label_name']}  specialist={r['is_specialist']}  needs_llm={r['needs_llm']}  "
          f"margin={r['margin']}  generic={r['generic_score']}")
    for t in r["top3"]:
        print(f"  {t['name']:<36} {t['score']:>6}  p={t['prob']}")
    for e in r["evidence"][:12]:
        print(f"   [{e['region']}] {e['dish_en']} ({e['dish_zh']}) ← \"{e['menu_line']}\"  {e['how']} w={e['weight']}")


if __name__ == "__main__":
    main()
