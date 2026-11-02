"""text.py: shared text normalization for menus, dish names and restaurant names.

Every module that compares Chinese or English text goes through these functions, so the rules live in one place:
    CJK              the one regex for Chinese characters
    to_simplified    traditional -> simplified (opencc)
    norm             simplified + lowercase + punctuation to spaces
    cjk_of / cjk_runs / strip_cjk   split a normalized line into its Chinese and Latin parts
    is_cjk / is_single_cjk           "is this all Chinese" / "is this one Chinese character"
    is_short_name    the "≤2-character names only match a whole item exactly" rule
    find_terms       which of a list of (term, tag) pairs appear in a text (markers, self-labels)
    PRICE / has_price  the menu-price pattern ($12.95, 12.95)
    name_keys        a restaurant's names, normalized, for "is this the same restaurant?" comparisons
    strip_names      remove a restaurant's own name from its menu text (unpriced lines only)

pip install opencc-python-reimplemented   (pure Python, no build step)
"""
from __future__ import annotations
import re
import warnings

CJK_CLASS = r"㐀-鿿\U00020000-\U0003134f"     # CJK Unified Ideographs + Ext A + Ext B–G
CJK = re.compile(f"[{CJK_CLASS}]+")
SHORT_NAME_MAX = 2

try:
    from opencc import OpenCC
    _cc = OpenCC("t2s")

    def to_simplified(text: str) -> str:
        return _cc.convert(text)
except ImportError:                                   # still runs; traditional-only menus will match worse
    warnings.warn("opencc not installed: traditional characters won't be converted. "
                  "pip install opencc-python-reimplemented")

    def to_simplified(text: str) -> str:
        return text


def norm(text: str) -> str:
    """Simplified Chinese, lowercase, apostrophes dropped, other punctuation turned into single spaces."""
    text = to_simplified(text).lower()
    text = re.sub(r"[’'`´]", "", text)
    text = re.sub(rf"[^\w{CJK_CLASS}]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def cjk_runs(text: str) -> list[str]:
    """Each unbroken run of Chinese characters, e.g. '乳饼炒火腿 goat cheese' -> ['乳饼炒火腿']."""
    return CJK.findall(text)


def cjk_of(text: str) -> str:
    """All Chinese characters in the text, joined."""
    return "".join(CJK.findall(text))


def strip_cjk(text: str) -> str:
    """The text with Chinese characters removed and whitespace collapsed (the English/pinyin part)."""
    return re.sub(r"\s+", " ", CJK.sub(" ", text)).strip()


def is_cjk(text: str) -> bool:
    return bool(CJK.fullmatch(text))


def is_single_cjk(text: str) -> bool:
    """One Chinese character (酸, 辣, 藏…). Too common to use as a marker or a self-label."""
    return is_cjk(text) and len(text) == 1


def is_short_name(name: str) -> bool:
    """Names this short (烧鹅, 撒) only count when they're the whole menu item, never inside a longer name."""
    return len(name) <= SHORT_NAME_MAX


def find_terms(text: str, terms) -> dict[str, list[str]]:
    """Which terms appear in the text, grouped by tag.

    text:  already normalized with norm().
    terms: iterable of (term, tag) pairs, each term already normalized with norm(). The tag is returned as-is.
    Returns {tag: [terms found, in input order, no duplicates]}; tags with nothing found are left out.

    Matching:
      all-Chinese term -> substring of a single run of Chinese characters (so 云之南 is found in
                          "south of the clouds 云之南", but "云 garden 南" does not contain 云南)
      any other term   -> whole-word match, words separated by spaces ("nan" is not found in "nanjing")
    """
    padded = f" {text} "
    runs = cjk_runs(text)
    found: dict[str, list[str]] = {}
    for term, tag in terms:
        hit = any(term in run for run in runs) if is_cjk(term) else f" {term} " in padded
        if hit and term not in found.get(tag, []):
            found.setdefault(tag, []).append(term)
    return found


PRICE = re.compile(r"\$\s?\d{1,3}(?:\.\d{2})?|\b\d{1,3}\.\d{2}\b")


def has_price(line: str) -> bool:
    return bool(PRICE.search(line))


def name_keys(*names: str | None) -> set[str]:
    """Normalized, non-empty forms of a restaurant's names (English, Chinese…). Two records with any key in common
    share a name."""
    return {norm(n) for n in names if n} - {""}


def strip_names(text: str, names) -> str:
    """Remove a restaurant's own name(s) from its menu text, so the name can't count as menu evidence.

    Only lines WITHOUT a price are touched: page titles, headers and banners repeat the name, while priced lines are
    menu items. A restaurant named after its signature dish (桂林米粉) keeps that dish where it's sold with a price.
    English words match case-insensitively with any punctuation or spacing between them (Xi'an = Xian = XI AN);
    Chinese matches after traditional -> simplified conversion. Chinese names shorter than 2 characters are ignored.
    The text is otherwise returned unchanged (raw, unnormalized)."""
    patterns = []
    for name in names:
        if not name:
            continue
        latin = [w for w in re.findall(r"[^\W_]+", name) if not CJK.fullmatch(w)]
        if latin:
            patterns.append(("latin", re.compile(r"(?<![^\W_])" + r"[\W_]*".join(map(re.escape, latin)) + r"(?![^\W_])",
                                                  re.IGNORECASE)))
        zh = to_simplified("".join(CJK.findall(name)))
        if len(zh) >= 2:
            patterns.append(("zh", zh))
    if not patterns:
        return text
    out = []
    for line in text.split("\n"):
        if not has_price(line):
            for kind, p in patterns:
                if kind == "latin":
                    line = p.sub(" ", line)
                else:
                    simp = to_simplified(line)
                    if len(simp) == len(line):                       # same positions: blank the original characters
                        start = simp.find(p)
                        while start >= 0:
                            line = line[:start] + " " * len(p) + line[start + len(p):]
                            simp = simp[:start] + " " * len(p) + simp[start + len(p):]
                            start = simp.find(p)
                    else:
                        line = line.replace(p, " ")
        out.append(line)
    return "\n".join(out)
