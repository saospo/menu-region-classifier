"""build_map.py: turn data/results.json into one static, self-contained map page (site/index.html).

    python build_map.py                       # data/results.json -> site/index.html
    python build_map.py --results other.json --out public/index.html

The page needs no server: open the file, or host it anywhere (GitHub Pages, an artifact link). The results are embedded
in the page; Leaflet comes from cdnjs and the map tiles from CARTO/OpenStreetMap, so viewing needs an internet connection.
"""
from __future__ import annotations
import argparse, json
from pathlib import Path

import paths
CCD_URL = "https://chinesecookingdemystified.substack.com/p/63-chinese-cuisines-the-complete"

PAGE_KEYS = ("id", "name", "name_zh", "address", "neighborhood", "website", "lat", "lng", "menu_quality",
             "menu_source_url", "label", "label_name", "label_source", "is_specialist", "confidence", "top3",
             "sections", "self_label")
EVIDENCE_KEYS = ("menu_line", "dish_en", "dish_zh", "section", "region", "how", "weight")


def page_data(results: dict) -> dict:
    """Only what the page shows: keeps the embedded JSON small and leaves out matcher internals."""
    rows = []
    for r in results["restaurants"]:
        row = {k: r.get(k) for k in PAGE_KEYS}
        row["evidence"] = [{k: e.get(k) for k in EVIDENCE_KEYS} for e in r.get("evidence", [])
                           if e.get("region") == r.get("label")][:6]
        rows.append(row)
    return {"generated_at": results.get("generated_at"), "summary": results.get("summary", {}),
            "gold": {k: results.get("gold", {}).get(k) for k in ("n", "correct")}, "restaurants": rows,
            "ccd_url": CCD_URL}


def render(results: dict) -> str:
    blob = json.dumps(page_data(results), ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.replace("__DATA__", blob)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=str(paths.RESULTS_PATH))
    ap.add_argument("--out", default=str(paths.SITE_PATH))
    a = ap.parse_args()
    results = paths.read_json(a.results)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(results), encoding="utf-8")
    print(f"{len(results['restaurants'])} restaurants -> {out}")


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Regional Chinese Map</title>
<meta name="description" content="Chinese restaurants classified by the regional cuisine their menus actually serve.">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
:root {
  --bg: #f7f5f0; --panel: #fffdf8; --ink: #1f1d1a; --muted: #6b665d; --line: #e4dfd4;
  --accent: #b23a2a; --chip: #efe9dd; --good: #2f7d4f; --warn: #a0651c;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #161513; --panel: #1f1e1b; --ink: #ece8df; --muted: #a39d91; --line: #34312c;
    --accent: #e0745f; --chip: #2a2824; --good: #6cc08f; --warn: #d9a25a;
  }
}
:root[data-theme="dark"] {
  --bg: #161513; --panel: #1f1e1b; --ink: #ece8df; --muted: #a39d91; --line: #34312c;
  --accent: #e0745f; --chip: #2a2824; --good: #6cc08f; --warn: #d9a25a;
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--ink);
  font: 15px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif; }
.app { display: grid; grid-template-columns: minmax(340px, 420px) 1fr; height: 100vh; }
aside { display: flex; flex-direction: column; border-right: 1px solid var(--line); background: var(--panel); min-height: 0; }
header { padding: 20px 20px 12px; border-bottom: 1px solid var(--line); }
h1 { font-size: 20px; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; margin: 0; }
.stat { margin: 14px 0 0; font-size: 14px; }
.stat b { font-size: 26px; color: var(--accent); font-variant-numeric: tabular-nums; margin-right: 6px; }
.controls { display: flex; gap: 8px; align-items: center; padding: 12px 20px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
select, label.toggle { font: inherit; font-size: 13px; color: var(--ink); background: var(--chip); border: 1px solid var(--line);
  border-radius: 8px; padding: 6px 10px; }
label.toggle { display: inline-flex; gap: 6px; align-items: center; cursor: pointer; }
.list { overflow-y: auto; padding: 8px 12px 24px; flex: 1; }
.card { border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; margin: 8px 0; background: var(--panel); cursor: pointer; }
.card:hover, .card.active { border-color: var(--accent); }
.card h2 { font-size: 15px; margin: 0; display: flex; justify-content: space-between; gap: 8px; }
.zh { color: var(--muted); font-weight: 400; margin-left: 6px; }
.meta { color: var(--muted); font-size: 12px; margin: 2px 0 8px; }
.region { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; font-weight: 600; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex: none; }
.badge { font-size: 11px; border-radius: 999px; padding: 2px 8px; background: var(--chip); color: var(--muted); white-space: nowrap; }
.badge.spec { background: var(--accent); color: #fff; }
.badge.agrees { color: var(--good); } .badge.conflicts { color: var(--warn); }
.ev { margin: 8px 0 0; padding: 0; list-style: none; font-size: 12.5px; display: none; }
.card.active .ev { display: block; }
.ev li { padding: 4px 0; border-top: 1px dashed var(--line); display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
.ev .line { color: var(--muted); overflow-wrap: anywhere; }
.ev .dish { text-align: right; }
.links { font-size: 12px; margin-top: 8px; display: none; gap: 12px; flex-wrap: wrap; }
.card.active .links { display: flex; }
a { color: var(--accent); }
#map { height: 100%; width: 100%; background: var(--bg); }
.empty { color: var(--muted); padding: 20px; font-size: 14px; }
footer { font-size: 11.5px; color: var(--muted); padding: 10px 20px; border-top: 1px solid var(--line); }
.leaflet-popup-content { font: 13px/1.4 ui-sans-serif, system-ui, sans-serif; }
@media (max-width: 760px) {
  .app { grid-template-columns: 1fr; grid-template-rows: 45vh auto; height: auto; }
  #map-wrap { order: -1; height: 45vh; }
  aside { border-right: 0; }
  .list { overflow: visible; }
  header, .controls, footer { padding-left: 16px; padding-right: 16px; }
}
</style>
</head>
<body>
<div class="app">
  <aside>
    <header>
      <h1>Regional Chinese Map</h1>
      <p class="sub">Restaurants classified by the regional cuisine their menus serve, using the
        <a id="ccd" target="_blank" rel="noopener">63 Chinese Cuisines</a> taxonomy.</p>
      <p class="stat" id="stat"></p>
    </header>
    <div class="controls">
      <select id="region" aria-label="Filter by region"><option value="">All regions</option></select>
      <label class="toggle"><input type="checkbox" id="spec"> Specialists only</label>
    </div>
    <div class="list" id="list"></div>
    <footer id="foot"></footer>
  </aside>
  <div id="map-wrap"><div id="map"></div></div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
(function () {
  const D = JSON.parse(document.getElementById("data").textContent);
  const R = D.restaurants;
  const PALETTE = ["#c0392b", "#2e86ab", "#d68910", "#7d3c98", "#1e8449", "#c2185b", "#5d6d7e", "#a04000",
                   "#117a65", "#2c3e94", "#9a7d0a", "#6c3483"];
  const NEUTRAL = { generic: "#9e9a92", unknown: "#cfcac0" };
  const regions = [...new Set(R.filter(r => !(r.label in NEUTRAL)).map(r => r.label))];
  const color = l => NEUTRAL[l] || PALETTE[regions.indexOf(l) % PALETTE.length];
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls;
    if (text != null) e.textContent = text; return e; };

  document.getElementById("ccd").href = D.ccd_url;
  const s = D.summary || {};
  const stat = document.getElementById("stat");
  if (s.with_menu) {
    stat.append(el("b", null, `${s.specialists}/${s.with_menu}`),
      document.createTextNode(` restaurants with menus are regional specialists, not generic "Chinese".`));
  } else { stat.textContent = "No menus classified yet."; }
  const foot = document.getElementById("foot");
  foot.textContent = `Generated ${D.generated_at || ""}` +
    (D.gold && D.gold.n ? ` · gold set: ${D.gold.correct}/${D.gold.n} correct` : "") +
    " · Map data © OpenStreetMap contributors © CARTO";

  const sel = document.getElementById("region");
  const names = {};
  R.forEach(r => names[r.label] = r.label_name);
  [...regions, ...Object.keys(NEUTRAL).filter(k => R.some(r => r.label === k))].forEach(l => {
    const o = el("option", null, `${names[l]} (${R.filter(r => r.label === l).length})`); o.value = l; sel.append(o);
  });

  let map = null, markers = {};
  if (window.L) {
    map = L.map("map", { zoomControl: true });
    const dark = matchMedia("(prefers-color-scheme: dark)").matches;
    L.tileLayer(`https://{s}.basemaps.cartocdn.com/${dark ? "dark_all" : "light_all"}/{z}/{x}/{y}{r}.png`, {
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
      maxZoom: 19 }).addTo(map);
    const pts = [];
    R.forEach(r => {
      if (r.lat == null || r.lng == null) return;
      const m = L.circleMarker([r.lat, r.lng], { radius: r.is_specialist ? 9 : 6, weight: 2,
        color: dark ? "#161513" : "#ffffff", fillColor: color(r.label), fillOpacity: 0.95 }).addTo(map);
      const pop = el("div"); pop.append(el("b", null, r.name), el("br"), document.createTextNode(r.label_name));
      m.bindPopup(pop); m.on("click", () => select(r.id, false));
      markers[r.id] = m; pts.push([r.lat, r.lng]);
    });
    if (pts.length) map.fitBounds(pts, { padding: [40, 40], maxZoom: 14 }); else map.setView([40.73, -73.93], 11);
  } else {
    document.getElementById("map").append(el("p", "empty", "The map couldn't load (no internet?). The list still works."));
  }

  function card(r) {
    const c = el("div", "card"); c.id = "card-" + r.id;
    const h = el("h2"); const n = el("span", null, r.name);
    if (r.name_zh) n.append(el("span", "zh", r.name_zh));
    h.append(n);
    if (r.is_specialist) h.append(el("span", "badge spec", "specialist"));
    c.append(h);
    c.append(el("div", "meta", [r.neighborhood || r.address, r.lat == null ? "no map location" : null]
      .filter(Boolean).join(" · ") || " "));
    const reg = el("div", "region"); const dot = el("span", "dot"); dot.style.background = color(r.label);
    reg.append(dot, document.createTextNode(r.label_name));
    if (r.confidence != null) reg.append(el("span", "badge", `${Math.round(r.confidence * 100)}%`));
    if (r.label_source === "adjudicator") reg.append(el("span", "badge", "LLM"));
    if (r.label_source === "no menu") reg.append(el("span", "badge", "no menu"));
    const sl = r.self_label && r.self_label.status;
    if (sl === "agrees") reg.append(el("span", "badge agrees", "name agrees"));
    if (sl === "conflicts") reg.append(el("span", "badge conflicts", "name says otherwise"));
    c.append(reg);
    if (r.sections && r.sections.length && !(r.label in NEUTRAL))      // generic tags aren't CCD sub-cuisines
      c.append(el("div", "meta", "CCD sections: " + r.sections.join(", ")));
    const ul = el("ul", "ev");
    (r.evidence || []).forEach(e => {
      const li = el("li"); li.append(el("span", "line", e.menu_line),
        el("span", "dish", [e.dish_zh, e.dish_en].filter(Boolean).join(" · "))); ul.append(li);
    });
    if (!(r.evidence || []).length) ul.append(el("li", "line", "No matched dishes for this label."));
    c.append(ul);
    const links = el("div", "links");
    const add = (href, text) => { if (!href) return; const a = el("a", null, text); a.href = href; a.target = "_blank";
      a.rel = "noopener"; links.append(a); };
    add(r.menu_source_url, `menu source (${r.menu_quality})`); add(r.website, "website"); add(D.ccd_url, "CCD guide");
    c.append(links);
    c.addEventListener("click", () => select(r.id, true));
    return c;
  }

  function select(id, fromList) {
    document.querySelectorAll(".card.active").forEach(c => c.classList.remove("active"));
    const c = document.getElementById("card-" + id);
    if (c) { c.classList.add("active"); if (!fromList) c.scrollIntoView({ block: "nearest", behavior: "smooth" }); }
    const m = markers[id];
    if (m && map && fromList) { map.setView(m.getLatLng(), Math.max(map.getZoom(), 14)); m.openPopup(); }
  }

  function draw() {
    const region = sel.value, spec = document.getElementById("spec").checked;
    const list = document.getElementById("list"); list.replaceChildren();
    const order = r => (r.is_specialist ? 0 : 1) + (r.label in NEUTRAL ? 2 : 0);
    const shown = R.filter(r => (!region || r.label === region) && (!spec || r.is_specialist))
      .sort((a, b) => order(a) - order(b) || a.name.localeCompare(b.name));
    shown.forEach(r => list.append(card(r)));
    if (!shown.length) list.append(el("p", "empty", "No restaurants match these filters."));
    Object.entries(markers).forEach(([id, m]) => {
      const vis = shown.some(r => r.id === id);
      if (vis && !map.hasLayer(m)) m.addTo(map); if (!vis && map.hasLayer(m)) map.removeLayer(m);
    });
  }
  sel.addEventListener("change", draw);
  document.getElementById("spec").addEventListener("change", draw);
  draw();
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
