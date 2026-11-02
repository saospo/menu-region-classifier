"""menus.py: fetch each restaurant's menu as text and save it to data/menus/{id}.txt.

    python menus.py --city "New York"                 # every restaurant in data/restaurants.json without a menu yet
    python menus.py --city "New York" --id south-of-the-clouds-1a2b3c --refresh
    python menus.py --city "New York" --search-only --limit 1    # 1 Nimble call: one restaurant, no Extract fallback

Restaurants are fetched in priority order (gold set, dish searches, region searches, seeds, generic sweep last),
so a tight --limit goes where it matters.

For each restaurant, stop at the first source with at least MIN_MENU_LINES menu-looking lines (SPEC.md §4):
  1. Nimble Search "<name> <neighborhood> menu" with full_content=True. Among results with enough menu lines,
     prefer the restaurant's own site, then delivery/menu sites (Grubhub, Seamless, ChowNow, Toast…), then the most
     menu lines.
  2. If the website is known: Nimble Extract on it, then on a "menu" link from that page if the page itself isn't one.
  3. Otherwise keep the best partial text (reviews and listings that name dishes): menu_quality="partial".
PDF and image menus are skipped (menu_quality="none" when nothing else is found).

The raw text is saved unnormalized; match.py normalizes it, and the script/romanization signals in notes.md need it raw.
data/menus/index.json records, per restaurant id: menu_quality, menu_source_url, n_menu_lines, query.
"""
from __future__ import annotations
import argparse, sys
from urllib.parse import urljoin, urlparse

import nimble_client
import paths
from text import cjk_of, has_price

MENUS_DIR = paths.MENUS_DIR              # module-level so tests (and callers) can point them elsewhere
INDEX_PATH = paths.MENU_INDEX_PATH

MIN_MENU_LINES = 15
MAX_LINE_LEN = 140
DELIVERY_DOMAINS = ("grubhub.com", "seamless.com", "doordash.com", "ubereats.com", "postmates.com", "chownow.com",
                    "toasttab.com", "order.online", "menupages.com", "allmenus.com", "menufy.com", "beyondmenu.com",
                    "clover.com", "menupages.com", "allmenus.com", "chinesemenuonline.com", "beyondmenu.com",
                    "zmenu.com", "sirved.com",
                    # chinese-focused order sites
                    "fantuanorder.com", "hungrypanda.co", "chowbus.com")
SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".gif")
EXCLUDE_DOMAINS = ("*.ai", "restaurantguru.com", "tripadvisor.com", "restaurantji.com", "foursquare.com",
                   "yellowpages.com", "mapquest.com", "superpages.com", "nextdoor.com", "yelp.com", "reddit.com",
                   "facebook.com", "instagram.com", "tiktok.com", "pinterest.com", "x.com", "twitter.com",
                   "xiaohongshu.com", "eater.com", "theinfatuation.com", "timeout.com", "grubstreet.com", "nytimes.com",
                   "secretnyc.co", "wanderlog.com")


def menu_line_count(text: str) -> int:
    """Lines that look like menu items: short, with a price or Chinese characters."""
    return sum(1 for line in text.splitlines()
               if 0 < len(line.strip()) <= MAX_LINE_LEN and (has_price(line) or cjk_of(line)))


def domain(url: str | None) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_skipped(url: str) -> bool:
    return urlparse(url).path.lower().endswith(SKIP_EXTENSIONS)


def source_rank(url: str, website: str | None) -> int:
    """0 = the restaurant's own site, 1 = delivery/menu site, 2 = anything else."""
    d = domain(url)
    if website and d and d == domain(website):
        return 0
    return 1 if any(d == x or d.endswith("." + x) for x in DELIVERY_DOMAINS) else 2


def _menu_link(links: list[str], page_url: str) -> str | None:
    """A link on the restaurant's own site whose URL mentions 'menu'."""
    for link in links:
        full = urljoin(page_url, link)
        if "menu" in full.lower() and domain(full) == domain(page_url) and not is_skipped(full):
            return full
    return None


def fetch_menu(restaurant: dict, city: str, search_only: bool = False) -> dict:
    """Returns {text, menu_source_url, menu_quality: full|partial|none, n_menu_lines, query}.
    search_only=True skips the Extract fallback, so a restaurant costs exactly one Nimble call."""
    area = restaurant.get("neighborhood") or city
    query = f'{restaurant["name"]} {area} menu'
    website = restaurant.get("website")
    best_partial = {"text": "", "menu_source_url": None, "n_menu_lines": 0}

    # 1. search with full page content
    try:
        results = nimble_client.search(query, max_results=5, full_content=True, exclude_domains=list(EXCLUDE_DOMAINS))
    except nimble_client.NimbleBudgetError:
        raise                                                    # out of budget: let fetch_all stop the run
    except nimble_client.NimbleError as e:                      # not recorded in the index, so it's retried next run
        print(f"  search failed: {e}", file=sys.stderr)
        return {"text": "", "menu_source_url": None, "menu_quality": "error", "n_menu_lines": 0, "query": query}
    candidates = []
    for r in results:
        url = r.get("url") or ""
        if not url or is_skipped(url):
            continue
        content = r.get("content") or r.get("description") or ""
        n = menu_line_count(content)
        candidates.append((url, content, n))
        if n > best_partial["n_menu_lines"]:
            best_partial = {"text": content, "menu_source_url": url, "n_menu_lines": n}
    good = [c for c in candidates if c[2] >= MIN_MENU_LINES]
    if good:
        url, content, n = min(good, key=lambda c: (source_rank(c[0], website), -c[2]))
        return {"text": content, "menu_source_url": url, "menu_quality": "full", "n_menu_lines": n, "query": query}

    # 2. extract the restaurant's own site, then its menu page
    if website and not is_skipped(website) and not search_only:
        pages = [website]
        while pages:
            page = pages.pop(0)
            try:
                got = nimble_client.extract(page, formats=["markdown", "links"])
            except nimble_client.NimbleBudgetError:
                raise
            except nimble_client.NimbleError as e:
                print(f"  extract failed: {e}", file=sys.stderr)
                break
            if got["status"] == "success":
                n = menu_line_count(got["markdown"])
                if n >= MIN_MENU_LINES:
                    return {"text": got["markdown"], "menu_source_url": got["url"], "menu_quality": "full",
                            "n_menu_lines": n, "query": query}
                if n > best_partial["n_menu_lines"]:
                    best_partial = {"text": got["markdown"], "menu_source_url": got["url"], "n_menu_lines": n}
                link = _menu_link(got["links"], got["url"]) if page == website else None
                if link and link != page:
                    pages.append(link)

    # 3. partial, or nothing
    quality = "partial" if best_partial["n_menu_lines"] > 0 else "none"
    return {**best_partial, "menu_quality": quality, "query": query}


KIND_PRIORITY = {"gold": 0, "dish": 1, "self_label": 2, "seed": 3, "sweep": 4}


def priority(restaurant: dict) -> int:
    """Fetch order under a tight budget: gold set first, then dish/region searches, generic sweep results last."""
    kinds = [v.get("kind") for v in restaurant.get("discovered_via", [])]
    return min((KIND_PRIORITY.get(k, 5) for k in kinds), default=5)


def load_index() -> dict:
    return paths.read_json(INDEX_PATH, {})


def fetch_all(restaurants: list[dict], city: str, refresh: bool = False, search_only: bool = False,
              limit: int | None = None) -> dict:
    """Fetch menus for every restaurant not already in the index (all of them with refresh=True).
    limit: fetch at most this many restaurants this run. Stops early if the Nimble budget runs out."""
    MENUS_DIR.mkdir(parents=True, exist_ok=True)
    index = load_index()
    fetched = 0
    for i, r in enumerate(restaurants, 1):
        if r["id"] in index and not refresh:
            continue
        if limit is not None and fetched >= limit:
            break
        try:
            m = fetch_menu(r, city, search_only=search_only)
        except nimble_client.NimbleBudgetError as e:
            print(f"  stopping: {e}", file=sys.stderr)
            break
        fetched += 1
        print(f'  [{i}/{len(restaurants)}] {m["menu_quality"]:<7} {m["n_menu_lines"]:>4} lines  {r["name"]}',
              file=sys.stderr)
        if m["menu_quality"] == "error":                        # not indexed: retried on the next run
            continue
        if m["text"]:
            (MENUS_DIR / f'{r["id"]}.txt').write_text(m["text"], encoding="utf-8")
        index[r["id"]] = {"name": r["name"], **{k: m[k] for k in ("menu_quality", "menu_source_url", "n_menu_lines", "query")}}
        paths.write_json(INDEX_PATH, index)                       # save as we go
    return index


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", required=True)
    ap.add_argument("--restaurants", default=str(paths.RESTAURANTS_PATH))
    ap.add_argument("--id", action="append", help="only these restaurant ids (repeatable)")
    ap.add_argument("--refresh", action="store_true", help="re-fetch menus already in the index")
    ap.add_argument("--search-only", action="store_true", help="1 Nimble call per restaurant: skip the Extract fallback")
    ap.add_argument("--limit", type=int, help="fetch at most this many restaurants this run")
    a = ap.parse_args()
    restaurants = paths.read_json(a.restaurants)
    if a.id:
        restaurants = [r for r in restaurants if r["id"] in a.id]
    restaurants.sort(key=priority)                                # stable: keeps file order within a kind
    index = fetch_all(restaurants, a.city, a.refresh, a.search_only, a.limit)
    counts = {q: sum(1 for v in index.values() if v["menu_quality"] == q) for q in ("full", "partial", "none")}
    print(f"menus: {counts}  -> {MENUS_DIR}", file=sys.stderr)


if __name__ == "__main__":
    main()
