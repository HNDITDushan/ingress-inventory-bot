"""
Static Sinhala content for Ingress Wiki pages (no AI needed).

Each JSON file in wiki_si/ holds a list of entries written from the wiki text:

    {
      "title": "Heat Sink",              # exact wiki page title
      "revid": 18611,                    # wiki revision it was written from
      "kw": ["හීට් සින්ක්", "heatsink"],    # extra words people use (Sinhala, slang)
      "h": "🔥 Heat Sink කියන්නේ මොකක්ද?",  # heading
      "s": "Sinhala summary paragraph",
      "p": ["key point", ...],          # important facts
      "sec": [["section heading", "Sinhala text"], ...]
    }

When the wiki page has a newer revision than "revid", the bot still shows the
Sinhala text but says the wiki page has changed since.
"""

import glob
import json
import logging
import os
import re

import wiki

logger = logging.getLogger(__name__)

_entries = {}       # title -> entry
_keywords = []      # (normalized keyword, title), longest first


def load(directory: str) -> int:
    """(Re)load every wiki_si/*.json file. Returns the number of pages."""
    entries = {}
    for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                for e in json.load(f):
                    if e.get("title") and e.get("s"):
                        entries[e["title"]] = e
        except (OSError, ValueError) as exc:
            logger.error("Sinhala content file %s not loaded: %s", os.path.basename(path), exc)
    keywords = []
    for title, e in entries.items():
        for k in [title, *e.get("kw", [])]:
            n = wiki.norm(k)
            if len(n) >= 2:
                keywords.append((n, title))
    keywords.sort(key=lambda kt: -len(kt[0]))
    _entries.clear()
    _entries.update(entries)
    _keywords[:] = keywords
    return len(entries)


def count() -> int:
    return len(_entries)


def get(title: str):
    return _entries.get(title)


def find_title(question: str, store: wiki.WikiStore):
    """The wiki page title a (Sinhala/English/mixed) question is about, preferring
    pages that have Sinhala content. None if nothing fits."""
    q = wiki.norm(question)
    if not q:
        return None
    # 1. Whole question is a title or redirect ("Heat Sink", "aegis shield").
    page = store.get_page(question)
    if page is not None:
        return page["title"]
    # 2. The longest known keyword inside the question ("ADA virus එකෙන් මොකද...").
    for key, title in _keywords:
        if key in q and (len(key) >= 4 or re.search(rf"(^|[^0-9a-z]){re.escape(key)}([^0-9a-z]|$)",
                                                     question.lower())):
            return title
    # 3. English words through full-text search.
    hits = store.search(question, limit=3)
    for h in hits:
        if h["title"] in _entries:
            return h["title"]
    return hits[0]["title"] if hits else None


def split_compare(text: str):
    """'A vs B' / 'A සහ B' / 'A and B' -> (A, B) or None."""
    parts = re.split(r"\s+(?:vs\.?|versus|and|or|සහ|හා|සමඟ|එක්ක)\s+|\s*/\s*|,\s*", text.strip(), maxsplit=1,
                     flags=re.IGNORECASE)
    if len(parts) == 2 and all(p.strip() for p in parts):
        return parts[0].strip(" ?"), parts[1].strip(" ?")
    return None
