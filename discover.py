"""discover.py: find candidate restaurants for a city and write data/restaurants.json.

    python discover.py --city "New York" --area "Flushing, Queens" --area "Chinatown, Manhattan" --dry-run
    python discover.py --city "New York" --area "Flushing, Queens" --regions yunnan,guizhou --seed-gold --geocode
    python discover.py --city "New York" --from-gold      # 0 Nimble calls: seed restaurants.json from gold_set.json

How (SPEC.md §3):
  1. build_queries   dish-driven searches, using each region's most distinctive Chinese dish names ("过桥米线
                     Flushing, Queens"), one self-label search per region ("Yunnan restaurant Flushing, Queens"),
                     and a focus="location" sweep ("Chinese restaurant Flushing, Queens") that finds the generic
                     places the classifier also needs to see. --seed-gold adds the gold-set restaurant names.
  2. Nimble Search   via nimble_client (cached on disk).
  3. extract_restaurants  Prompt B turns batches of results into restaurant records (via llm.call_llm).
  4. merge           into the existing data/restaurants.json: same restaurant = same normalized name (or Chinese
                     name) and no conflicting street number. Existing records keep their ids.
  5. geocode         optional, OpenStreetMap Nominatim at 1 request/second, cached.

Run with --dry-run first: it prints the queries and how many Nimble searches and LLM calls the run will make.
"""
from __future__ import annotations
import argparse, hashlib, json, re, sys, time
from pathlib import Path

import nimble_client
import paths
from llm import call_llm
from text import norm, name_keys
from taxonomy_interface import GENERIC, load_taxonomy, signature_dishes

MIN_SPECIFICITY = 0.5        # only dishes this distinctive are worth a search
RESULTS_PER_LLM_CALL = 30    # search results per extract_restaurants call
SWEEP_RESULTS = 20

RESTAURANTS_SCHEMA = {
    "type": "object",
    "properties": {"restaurants": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "name_zh": {"type": ["string", "null"]},
            "address": {"type": ["string", "null"]},
            "neighborhood": {"type": ["string", "null"]},
            "website": {"type": ["string", "null"]},
            "source_url": {"type": "string"},
            "evidence": {"type": "string", "description": "short quote from the result that shows it's a restaurant in the city"},
        },
        "required": ["name", "source_url", "evidence"]}}},
    "required": ["restaurants"],
}

PROMPT_RESTAURANTS = """From these web search results, list distinct restaurants that are physically located in {city}.
Only include actual restaurants (not articles, authors, grocery stores, or closed places).
Do not invent addresses or websites: use null when the results don't state them.
Include the Chinese name if the results give one.

RESULTS:
<<<
{results}
>>>"""


def extract_restaurants(results: list[dict], city: str) -> list[dict]:
    """results: Nimble search results [{title, url, description, content, additional_data?}, ...]"""
    blob = "\n\n".join(
        f"[{i}] {r.get('title', '')}\nURL: {r.get('url', '')}\n{(r.get('description') or '')}\n{(r.get('content') or '')[:1500]}"
        + (f"\nDATA: {json.dumps(r['additional_data'], ensure_ascii=False)[:800]}" if r.get("additional_data") else "")
        for i, r in enumerate(results))
    out = call_llm(PROMPT_RESTAURANTS.format(city=city, results=blob[:60000]), schema=RESTAURANTS_SCHEMA)
    return out.get("restaurants", [])


# ---------------------------------------------------------------- 1. queries
def build_queries(areas: list[str], regions: list[str] | None = None, dishes_per_region: int = 2,
                  english: bool = False, seed_names: list[str] = ()) -> list[dict]:
    """Each query: {query, kind: dish|self_label|sweep|seed, region?, dish?, and Nimble search options}."""
    queries = []
    for p in load_taxonomy()["regions"]:
        if p["id"] == GENERIC or (regions and p["id"] not in regions):
            continue
        dishes = signature_dishes(p, dishes_per_region, require_zh=True, min_specificity=MIN_SPECIFICITY)
        region_word = (p.get("self_labels") or [p["name"]])[0]
        for area in areas:
            for d in dishes:
                queries.append({"query": f"{d['zh']} {area}", "kind": "dish", "region": p["id"], "dish": d["en"]})
                if english:
                    queries.append({"query": f"{d['en']} {area}", "kind": "dish", "region": p["id"], "dish": d["en"]})
            queries.append({"query": f"{region_word} restaurant {area}", "kind": "self_label", "region": p["id"]})
    for area in areas:
        queries.append({"query": f"Chinese restaurant {area}", "kind": "sweep",
                        "focus": "location", "search_depth": "lite", "max_results": SWEEP_RESULTS})
    for name in seed_names:
        queries.append({"query": name, "kind": "seed"})
    return queries


def seed_from_gold(out_path: Path = paths.RESTAURANTS_PATH, gold_path: Path = paths.GOLD_PATH) -> list[dict]:
    """Add every gold-set restaurant to restaurants.json without any Nimble call, merged like any other record:
    existing records and their ids are kept, and a gold restaurant already present isn't added twice.
    The gold label is NOT copied: classification must not see it. validate.py compares against gold_set.json."""
    gold = [{"name": g["name"], "name_zh": g.get("name_zh"), "address": g.get("address"), "neighborhood": g.get("area"),
             "website": g.get("website"), "lat": g.get("lat"), "lng": g.get("lng"), "discovered_via": [{"kind": "gold"}]}
            for g in paths.load_gold(gold_path) if g.get("name")]
    restaurants = merge(gold, existing=paths.read_json(out_path, []))
    paths.write_json(out_path, restaurants)
    print(f"{len(restaurants)} restaurants -> {out_path} (0 Nimble calls)", file=sys.stderr)
    return restaurants


def gold_seed_names(city: str) -> list[str]:
    return [f'{g["name"]} {g.get("area") or city}' for g in paths.load_gold() if g.get("name")]


# ---------------------------------------------------------------- 2-3. search + extract
def run_searches(queries: list[dict]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Returns (unique results, url -> the queries that returned it)."""
    results, by_url = [], {}
    for i, q in enumerate(queries, 1):
        opts = {k: q[k] for k in ("focus", "search_depth", "max_results") if k in q}
        try:
            hits = nimble_client.search(q["query"], **opts)
        except nimble_client.NimbleBudgetError as e:            # out of budget: keep what we have, stop searching
            print(f"  [{i}/{len(queries)}] stopping: {e}", file=sys.stderr)
            break
        except nimble_client.NimbleError as e:
            print(f"  [{i}/{len(queries)}] skipped: {e}", file=sys.stderr)
            continue
        for h in hits:
            url = h.get("url") or ""
            tag = {k: q[k] for k in ("query", "kind", "region") if k in q}
            if url not in by_url:
                by_url[url] = []
                results.append(h)
            if tag not in by_url[url]:
                by_url[url].append(tag)
        print(f"  [{i}/{len(queries)}] {len(hits):>3} results  {q['query']}", file=sys.stderr)
    return results, by_url


def extract_all(results: list[dict], city: str) -> list[dict]:
    found = []
    for start in range(0, len(results), RESULTS_PER_LLM_CALL):
        found += extract_restaurants(results[start:start + RESULTS_PER_LLM_CALL], city)
    return found


# ---------------------------------------------------------------- 4. merge
def street_number(address: str | None) -> str | None:
    m = re.search(r"\b(\d+(?:-\d+)?)\b", address or "")
    return m.group(1) if m else None


def same_restaurant(a: dict, b: dict) -> bool:
    if not name_keys(a.get("name"), a.get("name_zh")) & name_keys(b.get("name"), b.get("name_zh")):
        return False
    na, nb = street_number(a.get("address")), street_number(b.get("address"))
    return na is None or nb is None or na == nb              # same name, no conflicting address -> same place


def restaurant_id(r: dict) -> str:
    """Made once, when a restaurant is first recorded, and never changed after (menus are saved under it)."""
    slug = re.sub(r"[^a-z0-9]+", "-", norm(r.get("name") or "").encode("ascii", "ignore").decode()).strip("-")[:40]
    digest = hashlib.sha1(f'{norm(r.get("name") or "")}|{street_number(r.get("address")) or ""}'.encode()).hexdigest()[:6]
    return f"{slug or 'r'}-{digest}"


FIELDS = ("name_zh", "address", "neighborhood", "website", "lat", "lng")


def merge(found: list[dict], by_url: dict[str, list[dict]] | None = None, existing: list[dict] = ()) -> list[dict]:
    """Merge new records into existing ones and return the combined list (existing records first, in order).
    Existing records keep their id; a missing field is filled from a new record, never overwritten; sources,
    evidence and discovered_via accumulate. A record's discovered_via comes from its own field plus the queries
    that returned its source_url (by_url)."""
    by_url = by_url or {}
    merged = [{**e, **{k: list(e.get(k) or []) for k in ("sources", "evidence", "discovered_via")}} for e in existing]
    for r in found:
        if not r.get("name"):
            continue
        via = list(r.get("discovered_via") or []) + by_url.get(r.get("source_url") or "", [])
        match = next((m for m in merged if same_restaurant(m, r)), None)
        if match is None:
            merged.append({"name": r["name"], **{f: None for f in FIELDS}, "sources": [], "evidence": [],
                           "discovered_via": []})
            match = merged[-1]
        for field in FIELDS:                                                 # fill gaps, never overwrite
            if match.get(field) is None and r.get(field) is not None:
                match[field] = r[field]
        if r.get("source_url") and r["source_url"] not in match["sources"]:
            match["sources"].append(r["source_url"])
        if r.get("evidence") and r["evidence"] not in match["evidence"]:
            match["evidence"].append(r["evidence"])
        for tag in via:
            if tag not in match["discovered_via"]:
                match["discovered_via"].append(tag)
    for m in merged:
        if not m.get("id"):                                                  # existing ids are never changed
            m["id"] = restaurant_id(m)
    return merged


# ---------------------------------------------------------------- 5. geocode
def geocode(restaurants: list[dict], city: str) -> None:
    """Fill lat/lng from OpenStreetMap Nominatim (free, 1 request/second). Restaurants without an address are skipped."""
    import requests
    cache = paths.read_json(paths.GEOCODE_CACHE, {})
    for r in restaurants:
        if not r.get("address") or r.get("lat") is not None:
            continue
        q = r["address"] if city.lower() in r["address"].lower() else f'{r["address"]}, {city}'
        if q not in cache:
            try:
                resp = requests.get("https://nominatim.openstreetmap.org/search",
                                    params={"q": q, "format": "json", "limit": 1},
                                    headers={"User-Agent": "menu-region-classifier/0.1"}, timeout=20)
                hits = resp.json() if resp.ok else []
                cache[q] = [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None
            except Exception as e:                                         # noqa: BLE001
                print(f"  geocode failed for {q!r}: {e}", file=sys.stderr)
                continue
            time.sleep(1.0)
        if cache.get(q):
            r["lat"], r["lng"] = cache[q]
    paths.write_json(paths.GEOCODE_CACHE, cache)


# ---------------------------------------------------------------- pipeline
def discover(city: str, areas: list[str], regions: list[str] | None = None, dishes_per_region: int = 2,
             english: bool = False, seed_gold: bool = False, do_geocode: bool = False,
             out_path: Path = paths.RESTAURANTS_PATH) -> list[dict]:
    """Search, extract and merge into out_path. Restaurants already in the file (e.g. the gold set) are kept, with
    their ids, so menus already fetched for them stay linked."""
    queries = build_queries(areas, regions, dishes_per_region, english, gold_seed_names(city) if seed_gold else [])
    results, by_url = run_searches(queries)
    restaurants = merge(extract_all(results, city), by_url, existing=paths.read_json(out_path, []))
    if do_geocode:
        geocode(restaurants, city)
    paths.write_json(out_path, restaurants)
    print(f"{len(queries)} searches, {len(results)} unique results, {len(restaurants)} restaurants -> {out_path}",
          file=sys.stderr)
    return restaurants


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", required=True, help='e.g. "New York"')
    ap.add_argument("--area", action="append", help='repeatable, e.g. "Flushing, Queens" (required unless --from-gold)')
    ap.add_argument("--from-gold", action="store_true", help="seed restaurants.json from gold_set.json; no searches")
    ap.add_argument("--regions", help="comma-separated region ids (default: all)")
    ap.add_argument("--dishes-per-region", type=int, default=2)
    ap.add_argument("--english", action="store_true", help="also search each dish's English name")
    ap.add_argument("--seed-gold", action="store_true", help="also search the gold-set restaurant names")
    ap.add_argument("--geocode", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the queries and expected calls, then stop")
    a = ap.parse_args()
    if a.from_gold:
        seed_from_gold()
        return
    if not a.area:
        ap.error("--area is required unless --from-gold")
    regions = a.regions.split(",") if a.regions else None
    if a.dry_run:
        qs = build_queries(a.area, regions, a.dishes_per_region, a.english, gold_seed_names(a.city) if a.seed_gold else [])
        for q in qs:
            print(f'{q["kind"]:<10} {q.get("region", ""):<20} {q["query"]}')
        est = sum(q.get("max_results", 10) for q in qs)
        print(f"\n{len(qs)} Nimble searches; up to {est} results -> about "
              f"{-(-est // RESULTS_PER_LLM_CALL)} LLM calls (fewer after duplicate URLs are removed)")
        return
    discover(a.city, a.area, regions, a.dishes_per_region, a.english, a.seed_gold, a.geocode)


if __name__ == "__main__":
    main()
