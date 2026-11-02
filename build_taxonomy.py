"""taxonomy_src.json -> taxonomy.json : flat REGION-level fingerprints.

The grouping list is called "regions" in taxonomy_src.json, taxonomy.json and everywhere downstream, since
several (Sichuan & Chongqing, Dongbei, Southeast Asian Chinese) aren't provinces.

Sub-cuisines in the source are just how the article organizes dishes; here they're merged
into their region. Each dish keeps `section` (the article sub-cuisine it came from)
only so evidence cards can cite the article.

specificity = 1 / #regions whose dish names contain this name (capped by weight_override).
"""
import json, collections
from text import is_short_name, norm
src = json.load(open("taxonomy_src.json"))
subs = {s["id"]: s for s in src["subcuisines"]}
problems = []

home = {}
for p in src["regions"]:
    for sid in p["subcuisines"]:
        if sid not in subs: problems.append(f"unknown subcuisine {sid}")
        if sid in home: problems.append(f"{sid} in two regions")
        home[sid] = p["id"]
problems += [f"{sid} not in any region" for sid in subs if sid not in home]

def names(d): return ([d["zh"]] if d.get("zh") else []) + d.get("zh_alt", [])

region_names = collections.defaultdict(set)   # zh name -> regions
for s in subs.values():
    for d in s["dishes"]:
        for n in names(d): region_names[n].add(home[s["id"]])

def regions_with(name):                   # incl. longer names containing it
    w = set(region_names[name])
    if is_short_name(name): return w       # short names are matched exactly, so containment doesn't apply
    for other, ps in region_names.items():
        if other != name and name in other: w |= ps
    return w

out = []
for p in src["regions"]:
    dishes, seen, markers, labels = [], set(), [], list(p["self_labels"])
    for sid in p["subcuisines"]:
        s = subs[sid]
        for l in s["self_labels"]:
            if l not in labels: labels.append(l)
        for m in s["markers"]:
            if m not in markers: markers.append(m)
        for d in s["dishes"]:
            key = d.get("zh") or norm(d["en"])
            if key in seen: continue
            seen.add(key)
            d = json.loads(json.dumps(d))
            ov = d.pop("weight_override", None)
            spec = 1 / len(regions_with(d["zh"])) if d.get("zh") else 1.0
            d["specificity"] = round(min(spec, ov) if ov else spec, 3)
            d["exact_only"] = [n for n in names(d) if is_short_name(n)]
            d.setdefault("zh_alt", [])
            d["section"] = s["name"]
            dishes.append(d)
    out.append({"id": p["id"], "name": p["name"], "zh": p["zh"],
                "self_labels": labels, "dishes": dishes, "markers": markers})

json.dump({"meta": {**src["meta"], "level": "region"}, "regions": out},
          open("taxonomy.json", "w"), ensure_ascii=False, indent=2)
print("problems:", problems or "none")
for p in out:
    strong = sum(d["specificity"] >= 0.5 for d in p["dishes"])
    print(f"{p['name']:<22} {len(p['dishes']):>3} dishes ({strong} strong), {len(p['markers'])} markers, {len(p['self_labels'])} self-labels")
print("\nshared across regions:",
      {n: sorted(regions_with(n)) for n in region_names if len(regions_with(n)) > 1})
