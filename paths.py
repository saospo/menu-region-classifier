"""paths.py: where pipeline data lives on disk, and how it's read and written.

Every module takes its paths from here, so DATA_DIR moves everything (caches and the Nimble budget ledger included).

    DATA_DIR            ./data (git-ignored)
    RESTAURANTS_PATH    data/restaurants.json          discover.py writes, menus.py and pipeline.py read
    MENUS_DIR           data/menus/{id}.txt            menus.py writes, pipeline.py reads
    MENU_INDEX_PATH     data/menus/index.json          per-restaurant menu_quality, source, line count
    RESULTS_PATH        data/results.json              pipeline.py writes, build_map.py reads
    GEOCODE_CACHE       data/geocode_cache.json        discover.py --geocode
    NIMBLE_CACHE_DIR    data/nimble_cache/             ($NIMBLE_CACHE_DIR overrides)
    NIMBLE_USAGE_PATH   data/nimble_usage.json         the Nimble budget ledger ($NIMBLE_USAGE_PATH overrides)
    LLM_CACHE_DIR       data/llm_cache/                ($LLM_CACHE_DIR overrides)
    GOLD_PATH           gold_set.json                  hand labels (in the repo, not in data/)
    SITE_PATH           site/index.html                build_map.py output

    read_json(path, default)   write_json(path, obj)   load_gold(path)
    cache_path(dir, *parts)    cache_get(path)          cache_put(path, obj)    the shared disk cache for llm.py and nimble_client.py
"""
from __future__ import annotations
import hashlib, json
from pathlib import Path
import tomllib
from functools import lru_cache

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
RESTAURANTS_PATH = DATA_DIR / "restaurants.json"
MENUS_DIR = DATA_DIR / "menus"
MENU_INDEX_PATH = MENUS_DIR / "index.json"
RESULTS_PATH = DATA_DIR / "results.json"
GEOCODE_CACHE = DATA_DIR / "geocode_cache.json"
NIMBLE_CACHE_DIR = DATA_DIR / "nimble_cache"
NIMBLE_USAGE_PATH = DATA_DIR / "nimble_usage.json"
LLM_CACHE_DIR = DATA_DIR / "llm_cache"
GOLD_PATH = ROOT / "gold_set.json"
SITE_PATH = ROOT / "site" / "index.html"
CONFIG_PATH = ROOT / "config.toml"     # committed defaults
SECRETS_PATH = ROOT / "secrets.toml"   # git-ignored: API keys + Nimble budget

_MISSING = object()


def read_json(path: str | Path, default=_MISSING):
    """Parsed JSON from path. If the file doesn't exist, return `default` (or raise if none was given)."""
    path = Path(path)
    if not path.exists() and default is not _MISSING:
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: str | Path, obj, indent: int | None = 1) -> None:
    """Write JSON (UTF-8, Chinese kept readable), creating parent folders."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=indent), encoding="utf-8")


def load_gold(path: str | Path = GOLD_PATH) -> list[dict]:
    """The hand-labeled restaurants in gold_set.json (empty if the file doesn't exist)."""
    return read_json(path, {"restaurants": []})["restaurants"]


# ---------------------------------------------------------------- shared disk cache
def cache_path(cache_dir: str | Path, *parts) -> Path:
    """The cache file for a request. parts identify it (e.g. provider, model, prompt, schema); same parts, same file."""
    key = hashlib.sha256(json.dumps(list(parts), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return Path(cache_dir) / f"{key}.json"


def cache_get(path: Path):
    """The cached object, or None if there's no cache file."""
    return read_json(path, None)


def cache_put(path: Path, obj) -> None:
    write_json(path, obj, indent=None)


# ---------------------------------------------------------------- config file
@lru_cache(maxsize=None)
def _read(path):
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

def get_config(section: str) -> dict:
    """One section of config.toml, with the same section of secrets.toml merged on top."""
    return {**_read(CONFIG_PATH).get(section, {}), **_read(SECRETS_PATH).get(section, {})}
