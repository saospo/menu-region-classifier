"""nimble_client.py: the only module that talks to Nimble. Search and Extract over REST, cached on disk.

    from nimble_client import search, extract
    results = search("过桥米线 Flushing", max_results=10)              # -> [{title, description, url, content, metadata, ...}]
    page    = extract("https://example.com/menu", formats=["markdown"])  # -> {"status", "markdown", "html", "links", ...}

Endpoints (docs.nimbleway.com):
    POST https://sdk.nimbleway.com/v2/search    body: query, max_results (1-100), search_depth (lite|standard),
                                                 full_content, focus, country, locale, time_range, output_format,
                                                 include_domains, exclude_domains
    POST https://sdk.nimbleway.com/v2/extract   body: url, formats (html|markdown|links|...), render, country, ...
Auth: Authorization: Bearer <api_key>

Settings come from the [nimble] section of config.toml, with secrets.toml merged on top (paths.get_config):
base_url and timeout in config.toml, api_key and budget in secrets.toml.

Cache: data/nimble_cache/<sha256>.json (paths.NIMBLE_CACHE_DIR; moves with DATA_DIR), keyed on endpoint + request body. A cached response is returned without
calling Nimble, so a second run of the pipeline is free and works offline. Delete the folder to refresh.
Retries: 429 and 5xx back off exponentially (Nimble's rate limit is ~83 requests/second by default).

Budget: every live HTTP attempt (retries included) is recorded in data/nimble_usage.json, which persists across
runs. budget is the total number of live attempts allowed, ever, across all runs; a live call that would go
past it raises NimbleBudgetError before anything is sent. If budget isn't set, live calls are refused
(cached calls still work), so a forgotten setting can't spend queries. budget = "unlimited" turns the cap off.

    python nimble_client.py usage        # calls used, budget, and the log of every live call

pip install requests
"""
from __future__ import annotations
import sys, time
from datetime import datetime, timezone

import requests

import paths

CONFIG = paths.get_config("nimble")
BASE_URL = CONFIG["base_url"]
CACHE_DIR = paths.NIMBLE_CACHE_DIR      # module-level so tests (and callers) can point them elsewhere
USAGE_PATH = paths.NIMBLE_USAGE_PATH
TIMEOUT = CONFIG["timeout"]             # seconds; full_content and render can be slow
RETRIES = 4


class NimbleError(RuntimeError):
    pass


class NimbleBudgetError(NimbleError):
    pass


# ---------------------------------------------------------------- budget ledger
def _ledger() -> dict:
    return paths.read_json(USAGE_PATH, {"live_calls": 0, "log": []})


def budget() -> int | None:
    """The total live-call budget from [nimble] budget, or None for unlimited. Raises if it isn't set."""
    raw = str(CONFIG.get("budget", "")).strip()
    if raw == "":
        raise NimbleBudgetError("[nimble] budget is not set in secrets.toml. Set it to the total number of live Nimble "
                                "calls you allow across all runs (e.g. budget = 20), or budget = \"unlimited\".")
    return None if raw.lower() == "unlimited" else int(raw)


def used() -> int:
    return _ledger()["live_calls"]


def remaining() -> int | None:
    b = budget()
    return None if b is None else max(0, b - used())


def _charge(endpoint: str, body: dict) -> None:
    """Record one live attempt, refusing first if it would exceed the budget."""
    b = budget()
    ledger = _ledger()
    if b is not None and ledger["live_calls"] >= b:
        raise NimbleBudgetError(f"Nimble budget used up: {ledger['live_calls']}/{b} live calls "
                                f"(see `python nimble_client.py usage`). Refusing {endpoint} for "
                                f"{body.get('query') or body.get('url')!r}.")
    ledger["live_calls"] += 1
    ledger["log"].append({"n": ledger["live_calls"], "endpoint": endpoint,
                          "target": body.get("query") or body.get("url"),
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    paths.write_json(USAGE_PATH, ledger)


def _post(endpoint: str, body: dict, use_cache: bool = True) -> dict:
    """POST to Nimble, with disk cache and retries. Returns the parsed JSON response."""
    path = paths.cache_path(CACHE_DIR, endpoint, body)
    cached = paths.cache_get(path) if use_cache else None
    if cached is not None:
        return cached

    api_key = CONFIG.get("api_key")
    if not api_key:
        raise NimbleError("[nimble] api_key is not set in secrets.toml (and this request isn't cached).")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last = None
    for attempt in range(RETRIES):
        _charge(endpoint, body)                                    # counts this attempt, or refuses
        try:
            r = requests.post(f"{BASE_URL}/{endpoint}", json=body, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as e:                     # network hiccup: retry
            last = f"{type(e).__name__}: {e}"
        else:
            if r.status_code == 200:
                data = r.json()
                if use_cache:
                    paths.cache_put(path, data)
                return data
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code not in (429, 500, 502, 503, 504):        # bad request / auth / credits: don't retry
                break
        time.sleep(2 ** attempt)
    raise NimbleError(f"Nimble {endpoint} failed for {body.get('query') or body.get('url')!r}: {last}")


def search(query: str, max_results: int = 10, search_depth: str | None = None, full_content: bool = False,
           focus: str | list[str] | None = None, country: str = "US", time_range: str | None = None,
           include_domains: list[str] | None = None, exclude_domains: list[str] | None = None,
           output_format: str | None = None, use_cache: bool = True) -> list[dict]:
    """Nimble Search. Returns the `results` list. Focus modes other than "general" need search_depth="lite"."""
    body = {"query": query, "max_results": max_results, "country": country}
    optional = {"search_depth": search_depth, "full_content": full_content or None, "focus": focus,
                "time_range": time_range, "include_domains": include_domains,
                "exclude_domains": exclude_domains, "output_format": output_format}
    body.update({k: v for k, v in optional.items() if v is not None})
    return _post("search", body, use_cache).get("results", [])


def extract(url: str, formats: list[str] | None = None, render: bool | str = "auto", country: str = "US",
            use_cache: bool = True) -> dict:
    """Nimble Extract for one URL. Returns {"status", "status_code", "url", "markdown", "html", "links"}
    (formats not requested come back empty). status is "success" when the page was fetched."""
    body = {"url": url, "formats": formats or ["markdown"], "render": render, "country": country}
    resp = _post("extract", body, use_cache)
    data = resp.get("data") or {}
    return {"status": resp.get("status"), "status_code": resp.get("status_code"), "url": resp.get("url", url),
            "markdown": data.get("markdown") or "", "html": data.get("html") or "", "links": data.get("links") or []}


def main():
    if sys.argv[1:] != ["usage"]:
        print("usage: python nimble_client.py usage", file=sys.stderr); sys.exit(2)
    ledger = _ledger()
    try:
        b = budget()
        cap = "unlimited" if b is None else f"{b} (remaining {max(0, b - ledger['live_calls'])})"
    except NimbleBudgetError:
        cap = "not set (live calls refused)"
    print(f"live Nimble calls used: {ledger['live_calls']}   budget: {cap}")
    for e in ledger["log"]:
        print(f"  #{e['n']:<3} {e['at']}  {e['endpoint']:<8} {e['target']}")


if __name__ == "__main__":
    main()
