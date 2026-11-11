"""
Ingress Wiki knowledge base: fetching, storage and search.

Pages come from the Ingress Wiki (https://ingress.fandom.com) through the
official MediaWiki API only, at most one request per second, with caching:
an update first asks for every article's latest revision id (a few cheap
requests) and then downloads only pages that are new or changed. Deleted
pages are marked as such and drop out of search.

Wiki content is CC-BY-SA; every answer keeps the source page URL.

Nothing in here talks to Telegram or to an AI model, so it can be tested
on its own.
"""

import asyncio
import difflib
import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import quote, unquote

import httpx

logger = logging.getLogger(__name__)

API_URL = "https://ingress.fandom.com/api.php"
PAGE_URL = "https://ingress.fandom.com/wiki/"
USER_AGENT = "IngressInventoryBot/1.0 (personal Telegram bot; Sinhala wiki assistant)"
REQUEST_INTERVAL = 1.0      # seconds between API requests (politeness)
REQUEST_TIMEOUT = 20.0
MAX_RETRIES = 3
CHUNK_CHARS = 1400          # search chunk size
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def page_url(title: str) -> str:
    return PAGE_URL + quote(title.replace(" ", "_"), safe="_:/()'!,.-")


def norm(text: str) -> str:
    """Lowercase letters and digits only, for title/alias matching."""
    return re.sub(r"[^0-9a-z඀-෿]", "", (text or "").lower())


class WikiUnavailable(Exception):
    """The wiki API couldn't be reached or returned something unusable."""


# ---------------------------------------------------------------------------
# HTML -> clean text
# ---------------------------------------------------------------------------

SKIP_TAGS = {"script", "style", "noscript", "figure", "img", "sup", "math", "svg", "audio", "video"}
SKIP_CLASSES = {
    "toc", "navbox", "mw-editsection", "printfooter", "reference", "references",
    "wds-tabs__wrapper", "gallery", "mw-references-wrap", "noprint", "metadata",
    "pi-image-collection", "pi-image", "thumbcaption", "mbox", "ambox",
}
BLOCK_TAGS = {"p", "div", "br", "ul", "ol", "dl", "dd", "dt", "table", "aside", "section", "blockquote"}
VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "wbr", "source", "col"}


class _TextExtractor(HTMLParser):
    """Turns rendered wiki HTML into readable text with '## ' section headings."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.links = []
        self._stack = []        # (tag, skipping) for open elements
        self._skip = 0
        self._cell_open = False

    def _in_cell(self):
        return any(t in ("td", "th") for t, _, _ in self._stack)

    def _emit(self, text):
        if not self._skip:
            self.out.append(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set((attrs.get("class") or "").split())
        skipping = tag in SKIP_TAGS or bool(classes & SKIP_CLASSES)
        label = "pi-data-label" in classes
        if tag not in VOID_TAGS:
            self._stack.append((tag, skipping, ": " if label else ""))
        if skipping:
            self._skip += 1
            return
        if self._skip:
            return
        if attrs.get("title") and any("icon" in c for c in classes):
            self._emit(f" {attrs['title']} ")
        if label:
            self._emit("\n")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n\n## " if tag in ("h2", "h3") else "\n\n### ")
        elif tag == "li":
            self._emit("\n• ")
        elif tag == "tr":
            self._emit("\n")
            self._cell_open = False
        elif tag in ("td", "th"):
            self._emit(" | " if self._cell_open else "")
            self._cell_open = True
        elif tag in BLOCK_TAGS and "pi-data-value" not in classes:
            self._emit(" " if self._in_cell() else "\n")
        if tag == "a":
            href = attrs.get("href") or ""
            m = re.match(r"^(?:https?://ingress\.fandom\.com)?/wiki/([^?#]+)", href)
            if m:
                title = unquote(m[1]).replace("_", " ")
                if ":" not in title:
                    self.links.append(title)

    def handle_endtag(self, tag):
        # Pop back to the matching open tag (HTML in the wild isn't always tidy).
        after = ""
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                after = self._stack[i][2]
                for _, skipping, _a in self._stack[i:]:
                    if skipping:
                        self._skip -= 1
                del self._stack[i:]
                break
        else:
            return
        if self._skip:
            return
        if after:
            self._emit(after)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit("\n")
        elif tag in BLOCK_TAGS:
            self._emit(" " if self._in_cell() else "\n")

    def handle_data(self, data):
        if self._skip:
            return
        text = re.sub(r"\s+", " ", data)
        if text.strip() or (text and self.out and not self.out[-1].endswith((" ", "\n"))):
            self._emit(text)

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._emit("\n")


def html_to_text(html_text: str):
    """(clean text, linked page titles)."""
    parser = _TextExtractor()
    parser.feed(html_text)
    parser.close()
    text = "".join(parser.out)
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        line = re.sub(r"^\|\s*|\s*\|$", "", line).strip()
        if line in ("•", "|"):
            continue
        lines.append(line)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    seen, links = set(), []
    for title in parser.links:
        if title not in seen:
            seen.add(title)
            links.append(title)
    return text, links


def split_sections(text: str):
    """[(section heading, body)], the lead section first with heading ''."""
    sections, heading, body = [], "", []
    for line in text.split("\n"):
        m = re.match(r"^#{2,3} (.+)$", line)
        if m:
            if "\n".join(body).strip():
                sections.append((heading, "\n".join(body).strip()))
            heading, body = m[1].strip(), []
        else:
            body.append(line)
    if "\n".join(body).strip():
        sections.append((heading, "\n".join(body).strip()))
    return sections


def chunk_sections(text: str):
    """[(section, chunk text)] small enough for search results and prompts."""
    chunks = []
    for heading, body in split_sections(text):
        if heading.lower() in ("references", "see also", "external links", "gallery", "trivia"):
            if heading.lower() != "see also":
                continue
        current = ""
        for para in body.split("\n"):
            if current and len(current) + len(para) + 1 > CHUNK_CHARS:
                chunks.append((heading, current.strip()))
                current = ""
            if len(para) > CHUNK_CHARS:
                for i in range(0, len(para), CHUNK_CHARS):
                    chunks.append((heading, para[i:i + CHUNK_CHARS]))
                continue
            current += para + "\n"
        if current.strip():
            chunks.append((heading, current.strip()))
    return chunks


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

# (our category, wiki categories / title words that put a page in it)
CATEGORY_RULES = [
    ("Items and Inventory", {"items", "portal mods", "weapons", "power-ups", "powerups", "resonators",
                             "viruses", "capsules", "keys", "media", "beacons", "mods", "flip cards"}),
    ("Portal Information", {"portals", "portal", "opr", "wayfarer", "portal submission"}),
    ("Links and Control Fields", {"links", "link", "control fields", "control field", "fields", "mind units"}),
    ("Missions and Medals", {"missions", "mission", "medals", "medal", "badges", "achievements"}),
    ("Events", {"events", "event", "anomalies", "anomaly", "campaigns", "first saturday"}),
    ("Game Updates", {"updates", "update", "releases"}),
    ("Factions and Story", {"factions", "resistance", "enlightened", "characters", "story", "lore", "niantic project"}),
    ("Advanced Gameplay", {"gameplay", "glyph hacking", "glyphs", "strategy", "defense", "attack", "mechanics", "xm"}),
    ("Game Basics", {"basics", "agents", "agent", "levels", "ap", "scanner", "device", "ops", "inventory"}),
]


def categorize(title: str, wiki_categories) -> str:
    cats = {c.replace("_", " ").lower() for c in wiki_categories}
    lower = title.lower()
    if re.match(r"^update \d", lower):
        return "Game Updates"
    if "items" not in cats and re.search(r"\b(links?|control fields?|fields?|mind units?|mu)\b", lower):
        return "Links and Control Fields"
    for name, words in CATEGORY_RULES:
        if cats & words:
            return name
    for name, words in CATEGORY_RULES:
        if any(re.search(rf"\b{re.escape(w)}\b", lower) for w in words):
            return name
    return "Other"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

class WikiStore:
    """SQLite knowledge base (a separate wiki.db so the inventory DB is untouched)."""

    def __init__(self, path: str):
        self.path = path
        with closing(self.connect()) as conn, conn:
            self._init(conn)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init(self, conn):
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS pages (
                pageid INTEGER PRIMARY KEY,
                title TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                revid INTEGER,
                wiki_categories TEXT NOT NULL DEFAULT '[]',
                category TEXT NOT NULL DEFAULT 'Other',
                content TEXT NOT NULL DEFAULT '',
                terms TEXT NOT NULL DEFAULT '[]',
                links TEXT NOT NULL DEFAULT '[]',
                language TEXT NOT NULL DEFAULT 'en',
                status TEXT NOT NULL DEFAULT 'ok',       -- ok | deleted
                fetched_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS aliases (
                alias_norm TEXT PRIMARY KEY,
                alias TEXT NOT NULL,
                pageid INTEGER NOT NULL,
                fragment TEXT NOT NULL DEFAULT ''
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
                title, section, body, pageid UNINDEXED, tokenize = 'porter unicode61'
            );
            CREATE TABLE IF NOT EXISTS answers (
                qkey TEXT PRIMARY KEY,
                answer TEXT NOT NULL,
                sources TEXT NOT NULL,
                kb_version TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS failed_searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(pages)")}
        if "images" not in columns:
            # Wiki image URLs for the page, main image first.
            conn.execute("ALTER TABLE pages ADD COLUMN images TEXT NOT NULL DEFAULT '[]'")

    # -- meta ---------------------------------------------------------------
    def get_meta(self, key, default=None):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, conn, key, value):
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def category(self, name: str):
        """Titles of a wiki category saved at the last update (pages we have only)."""
        titles = json.loads(self.get_meta(f"category:{name}", "[]"))
        return [t for t in titles if self.get_page(t) is not None]

    def kb_version(self) -> str:
        return self.get_meta("kb_version", "0")

    def page_count(self) -> int:
        with closing(self.connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM pages WHERE status = 'ok'").fetchone()[0]

    # -- pages --------------------------------------------------------------
    def known_revisions(self):
        with closing(self.connect()) as conn:
            return {r["pageid"]: (r["revid"], r["status"])
                    for r in conn.execute("SELECT pageid, revid, status FROM pages")}

    def save_page(self, conn, pageid, title, revid, wiki_categories, text, links):
        terms = sorted({title, *links})[:200]
        conn.execute(
            "INSERT INTO pages (pageid, title, url, revid, wiki_categories, category, content, terms, "
            "links, status, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?) "
            "ON CONFLICT(pageid) DO UPDATE SET title = excluded.title, url = excluded.url, "
            "revid = excluded.revid, wiki_categories = excluded.wiki_categories, "
            "category = excluded.category, content = excluded.content, terms = excluded.terms, "
            "links = excluded.links, status = 'ok', fetched_at = excluded.fetched_at",
            (pageid, title, page_url(title), revid, json.dumps(wiki_categories),
             categorize(title, wiki_categories), text, json.dumps(terms), json.dumps(links),
             datetime.now().strftime(TS_FORMAT)),
        )
        conn.execute("DELETE FROM chunks WHERE pageid = ?", (pageid,))
        conn.executemany(
            "INSERT INTO chunks (title, section, body, pageid) VALUES (?, ?, ?, ?)",
            [(title, section, body, pageid) for section, body in chunk_sections(text)],
        )
        conn.execute(
            "INSERT OR REPLACE INTO aliases (alias_norm, alias, pageid, fragment) VALUES (?, ?, ?, '')",
            (norm(title), title, pageid),
        )

    def set_images(self, conn, pageid, urls):
        conn.execute("UPDATE pages SET images = ? WHERE pageid = ?", (json.dumps(urls), pageid))

    def images(self, pageid: int):
        page = self.get_page_by_id(pageid)
        return json.loads(page["images"]) if page is not None else []

    def mark_deleted(self, conn, pageid):
        conn.execute("UPDATE pages SET status = 'deleted' WHERE pageid = ?", (pageid,))
        conn.execute("DELETE FROM chunks WHERE pageid = ?", (pageid,))
        conn.execute("DELETE FROM aliases WHERE pageid = ?", (pageid,))

    def get_page(self, title_or_alias: str):
        """A page by exact title or redirect alias (case/punctuation-insensitive)."""
        key = norm(title_or_alias)
        if not key:
            return None
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT p.*, a.fragment FROM aliases a JOIN pages p ON p.pageid = a.pageid "
                "WHERE a.alias_norm = ? AND p.status = 'ok'", (key,),
            ).fetchone()
        return row

    def get_page_by_id(self, pageid: int):
        with closing(self.connect()) as conn:
            return conn.execute("SELECT * FROM pages WHERE pageid = ? AND status = 'ok'",
                                (pageid,)).fetchone()

    def related(self, pageid: int, limit: int = 8):
        """Pages this page links to that exist in the knowledge base."""
        page = self.get_page_by_id(pageid)
        if page is None:
            return []
        out, seen = [], {pageid}
        for title in json.loads(page["links"]):
            row = self.get_page(title)
            if row and row["pageid"] not in seen and row["category"] != "Game Updates":
                seen.add(row["pageid"])
                out.append(row)
                if len(out) >= limit:
                    break
        return out

    # -- search -------------------------------------------------------------
    def _fts_query(self, query: str):
        words = [w for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 1]
        stop = {"what", "is", "the", "a", "an", "how", "does", "do", "of", "to", "in", "and", "or",
                "vs", "versus", "compare", "difference", "between", "about", "are", "can", "i", "me",
                "tell", "explain", "why", "when", "which", "with", "for", "on", "it", "its", "my"}
        words = [w for w in words if w.lower() not in stop] or words
        if not words:
            return None
        return " OR ".join(f'"{w}"' for w in words[:12])

    def search(self, query: str, limit: int = 6):
        """Best matching chunks: [{title, section, body, url, pageid, category}]."""
        query = (query or "").strip()[:300]
        results, seen = [], set()

        def add(row):
            key = (row["pageid"], row["section"], row["body"][:50])
            if key in seen:
                return
            seen.add(key)
            results.append({
                "title": row["title"], "section": row["section"], "body": row["body"],
                "pageid": row["pageid"], "url": page_url(row["title"]),
            })

        with closing(self.connect()) as conn:
            # 1. An exact title or redirect alias match leads.
            direct = self.get_page(query) or self._fuzzy_page(conn, query)
            if direct is not None:
                for row in conn.execute(
                    "SELECT title, section, body, pageid FROM chunks WHERE pageid = ? LIMIT 3",
                    (direct["pageid"],),
                ):
                    add(row)
            # 2. Full-text search, titles weighted most.
            fts = self._fts_query(query)
            if fts:
                try:
                    rows = conn.execute(
                        "SELECT title, section, body, pageid FROM chunks WHERE chunks MATCH ? "
                        "ORDER BY bm25(chunks, 8.0, 3.0, 1.0) LIMIT ?",
                        (fts, limit * 3),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                per_page = {}
                for row in rows:
                    if per_page.get(row["pageid"], 0) >= 2:
                        continue
                    per_page[row["pageid"]] = per_page.get(row["pageid"], 0) + 1
                    add(row)
        return results[:limit]

    def _fuzzy_page(self, conn, query: str):
        """A close title/alias match for typos ('Jarvis Viros', 'heatsink')."""
        key = norm(query)
        if len(key) < 3:
            return None
        aliases = {r["alias_norm"]: r["pageid"] for r in conn.execute("SELECT alias_norm, pageid FROM aliases")}
        match = difflib.get_close_matches(key, list(aliases), n=1, cutoff=0.8)
        if not match:
            return None
        return conn.execute("SELECT * FROM pages WHERE pageid = ? AND status = 'ok'",
                            (aliases[match[0]],)).fetchone()

    def suggest_titles(self, query: str, n: int = 5):
        with closing(self.connect()) as conn:
            aliases = {r["alias_norm"]: r["alias"] for r in conn.execute("SELECT alias_norm, alias FROM aliases")}
        keys = difflib.get_close_matches(norm(query), list(aliases), n=n, cutoff=0.6)
        return [aliases[k] for k in keys]

    def log_failed(self, query: str, reason: str):
        with closing(self.connect()) as conn, conn:
            conn.execute("INSERT INTO failed_searches (query, reason, created_at) VALUES (?, ?, ?)",
                         (query[:300], reason[:100], datetime.now().strftime(TS_FORMAT)))
        logger.info("Wiki search failed (%s): %r", reason, query[:120])

    # -- answer cache -------------------------------------------------------
    def cached_answer(self, qkey: str, max_age_days: int = 14):
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM answers WHERE qkey = ?", (qkey,)).fetchone()
        if row is None or row["kb_version"] != self.kb_version():
            return None
        age = datetime.now() - datetime.strptime(row["created_at"], TS_FORMAT)
        if age.days > max_age_days:
            return None
        return {"text": row["answer"], "sources": [tuple(s) for s in json.loads(row["sources"])]}

    def cache_answer(self, qkey: str, text: str, sources):
        with closing(self.connect()) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO answers (qkey, answer, sources, kb_version, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (qkey, text, json.dumps(sources), self.kb_version(), datetime.now().strftime(TS_FORMAT)),
            )


# ---------------------------------------------------------------------------
# Fetching (MediaWiki API)
# ---------------------------------------------------------------------------

class WikiFetcher:
    """Polite MediaWiki API client: one request per second, timeouts, retries."""

    def __init__(self, api_url: str = API_URL):
        self.api_url = api_url
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def _get(self, client, params):
        params = {**params, "format": "json", "formatversion": "2"}
        delay = 2.0
        for attempt in range(MAX_RETRIES + 1):
            async with self._lock:
                wait = self._last + REQUEST_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            try:
                resp = await client.get(self.api_url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt == MAX_RETRIES:
                    raise WikiUnavailable(f"network error: {type(exc).__name__}") from exc
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_RETRIES:
                    raise WikiUnavailable(f"HTTP {resp.status_code}")
                retry_after = resp.headers.get("retry-after", "")
                await asyncio.sleep(float(retry_after) if retry_after.isdigit() else delay)
                delay *= 2
                continue
            if resp.status_code != 200:
                raise WikiUnavailable(f"HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                # e.g. a bot-check page instead of JSON - don't try to get around it.
                raise WikiUnavailable("non-JSON response") from exc
            if "error" in data:
                raise WikiUnavailable(f"API error: {data['error'].get('code')}")
            return data
        raise WikiUnavailable("gave up")

    async def _list(self, client, params, key="pages"):
        """All results of a generator/list query, following 'continue'."""
        out, cont = [], {}
        while True:
            data = await self._get(client, {**params, **cont})
            out.extend(data.get("query", {}).get(key, []))
            if "continue" not in data:
                return out, data
            cont = data["continue"]

    async def list_articles(self, client):
        """{pageid: (title, lastrevid)} for every non-redirect article."""
        pages, _ = await self._list(client, {
            "action": "query", "generator": "allpages", "gapnamespace": 0,
            "gapfilterredir": "nonredirects", "gaplimit": "max", "prop": "info",
        })
        result = {}
        for p in pages:
            if "lastrevid" in p:
                result[p["pageid"]] = (p["title"], p["lastrevid"])
        return result

    async def list_redirects(self, client):
        """[(alias, target title, fragment)] for every redirect to a main-namespace article."""
        pages, _ = await self._list(client, {
            "action": "query", "generator": "allpages", "gapnamespace": 0,
            "gapfilterredir": "nonredirects", "gaplimit": "max",
            "prop": "redirects", "rdprop": "title|fragment", "rdnamespace": 0, "rdlimit": "max",
        })
        out = []
        for p in pages:
            for r in p.get("redirects", []):
                out.append((r["title"], p["title"], r.get("fragment", "")))
        return out

    async def list_page_images(self, client):
        """{pageid: (main image URL or None, [file names on the page])}."""
        out, cont = {}, {}
        while True:
            data = await self._get(client, {
                "action": "query", "generator": "allpages", "gapnamespace": 0,
                "gapfilterredir": "nonredirects", "gaplimit": "max",
                "prop": "pageimages|images", "piprop": "original", "imlimit": "max", **cont,
            })
            for p in data.get("query", {}).get("pages", []):
                main, files = out.get(p["pageid"], (None, []))
                if p.get("original"):
                    main = p["original"]["source"]
                files = files + [i["title"] for i in p.get("images", [])]
                out[p["pageid"]] = (main, files)
            if "continue" not in data:
                return out
            cont = data["continue"]

    async def image_urls(self, client, files, width: int = 600):
        """{'File:X.png': resized image URL} for the given file titles (50 per request)."""
        out = {}
        files = list(dict.fromkeys(files))
        for i in range(0, len(files), 50):
            data = await self._get(client, {
                "action": "query", "titles": "|".join(files[i:i + 50]), "prop": "imageinfo",
                "iiprop": "url|mime", "iiurlwidth": width,
            })
            for p in data.get("query", {}).get("pages", []):
                info = (p.get("imageinfo") or [{}])[0]
                if info.get("mime", "").startswith("image/") and "svg" not in info.get("mime", ""):
                    out[p["title"]] = info.get("thumburl") or info.get("url")
        return out

    async def category_members(self, client, category: str):
        """Main-namespace page titles in a wiki category (e.g. 'Items')."""
        members, _ = await self._list(client, {
            "action": "query", "list": "categorymembers", "cmtitle": f"Category:{category}",
            "cmnamespace": 0, "cmlimit": "max",
        }, key="categorymembers")
        return [m["title"] for m in members]

    async def fetch_page(self, client, pageid: int):
        data = await self._get(client, {
            "action": "parse", "pageid": pageid, "prop": "text|revid|categories",
            "disablelimitreport": 1, "disableeditsection": 1,
        })
        parse = data["parse"]
        text, links = html_to_text(parse["text"])
        cats = [c["category"].replace("_", " ") for c in parse.get("categories", [])
                if not c.get("hidden")]
        return parse["title"], parse["revid"], cats, text, links


def resize_url(url: str, width: int = 600) -> str:
    """Fandom image URL -> a smaller rendition (Telegram-friendly)."""
    if url and "/revision/latest" in url and "scale-to-width" not in url:
        return url.replace("/revision/latest", f"/revision/latest/scale-to-width-down/{width}", 1)
    return url


def original_format(url: str) -> str:
    """Ask the image CDN for the real PNG/JPEG (it serves WebP by default, which
    Telegram doesn't accept for photos sent by URL)."""
    if not url or "format=original" in url:
        return url
    return url + ("&" if "?" in url else "?") + "format=original"


def pick_images(title: str, main, files, max_images: int = 4):
    """File titles worth showing for a page: ones named after the page (e.g. rarity variants)."""
    key = norm(title)
    picked = []
    for f in files:
        name = f.split(":", 1)[-1].rsplit(".", 1)[0]
        n = norm(name)
        if not n.startswith(key) or not f.lower().endswith((".png", ".jpg", ".jpeg")):
            continue
        if any(w in n for w in ("icon", "badge", "medal", "deployed", "slot")):
            continue
        if main and norm(main.split("/images/")[-1].split("/")[2] if "/images/" in main else "") == norm(f.split(":", 1)[-1]):
            continue
        picked.append(f)
    return picked[: max_images - (1 if main else 0)]


async def refresh_images(store: "WikiStore", fetcher: "WikiFetcher", client):
    """Update every page's image list (a handful of requests, no page downloads)."""
    listing = await fetcher.list_page_images(client)
    with closing(store.connect()) as conn:
        titles = {r["pageid"]: r["title"] for r in conn.execute("SELECT pageid, title FROM pages WHERE status = 'ok'")}
    wanted, per_page = [], {}
    for pageid, (main, files) in listing.items():
        if pageid not in titles:
            continue
        if main and not main.split("/revision")[0].split("?")[0].lower().endswith((".png", ".jpg", ".jpeg")):
            main = None  # skip animated GIFs / SVGs
        extra = pick_images(titles[pageid], main, files)
        per_page[pageid] = (main, extra)
        wanted += extra
    urls = await fetcher.image_urls(client, wanted) if wanted else {}
    count = 0
    with closing(store.connect()) as conn, conn:
        for pageid, (main, extra) in per_page.items():
            images = ([resize_url(main)] if main else []) + [urls[f] for f in extra if f in urls]
            images = [original_format(u) for u in images]
            images = list(dict.fromkeys(images))[:4]
            store.set_images(conn, pageid, images)
            count += bool(images)
    return count


TOPIC_CATEGORIES = ["Items"]   # wiki categories offered as browsable topics

_update_lock = asyncio.Lock()


async def update_knowledge(store: WikiStore, fetcher: WikiFetcher = None, force: bool = False,
                           max_pages: int = None, progress=None):
    """Bring the knowledge base up to date. Only new/changed pages are downloaded.

    Returns {"new", "changed", "deleted", "unchanged", "aliases", "failed"}. Raises
    WikiUnavailable if the article list can't be read. Only one update runs at a time.
    """
    if _update_lock.locked():
        raise RuntimeError("An update is already running.")
    fetcher = fetcher or WikiFetcher()
    async with _update_lock:
        stats = {"new": 0, "changed": 0, "deleted": 0, "unchanged": 0, "aliases": 0, "failed": 0}
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT,
                                     headers={"User-Agent": USER_AGENT}) as client:
            remote = await fetcher.list_articles(client)
            if not remote:
                raise WikiUnavailable("empty article list")
            known = store.known_revisions()
            todo = []
            for pageid, (title, revid) in remote.items():
                old = known.get(pageid)
                if old is None or old[1] != "ok":
                    todo.append((pageid, "new"))
                elif force or old[0] != revid:
                    todo.append((pageid, "changed"))
                else:
                    stats["unchanged"] += 1
            if max_pages is not None:
                todo = todo[:max_pages]

            for i, (pageid, kind) in enumerate(todo, 1):
                try:
                    title, revid, cats, text, links = await fetcher.fetch_page(client, pageid)
                except WikiUnavailable as exc:
                    stats["failed"] += 1
                    logger.warning("Wiki page %s failed: %s", pageid, exc)
                    continue
                with closing(store.connect()) as conn, conn:
                    store.save_page(conn, pageid, title, revid, cats, text, links)
                stats[kind] += 1
                if progress and i % 25 == 0:
                    await progress(i, len(todo))

            with closing(store.connect()) as conn, conn:
                if max_pages is None:
                    for pageid, (revid, status) in known.items():
                        if pageid not in remote and status == "ok":
                            store.mark_deleted(conn, pageid)
                            stats["deleted"] += 1

            try:
                stats["images"] = await refresh_images(store, fetcher, client)
            except WikiUnavailable as exc:
                logger.warning("Wiki images not refreshed: %s", exc)

            for cat in TOPIC_CATEGORIES:
                try:
                    titles = await fetcher.category_members(client, cat)
                    with closing(store.connect()) as conn, conn:
                        store.set_meta(conn, f"category:{cat}", json.dumps(titles))
                except WikiUnavailable as exc:
                    logger.warning("Wiki category %s not refreshed: %s", cat, exc)

            redirects = await fetcher.list_redirects(client)
            with closing(store.connect()) as conn, conn:
                titles = {r["title"]: r["pageid"] for r in conn.execute(
                    "SELECT title, pageid FROM pages WHERE status = 'ok'")}
                conn.execute("DELETE FROM aliases")
                for title, pageid in titles.items():
                    conn.execute("INSERT OR REPLACE INTO aliases VALUES (?, ?, ?, '')",
                                 (norm(title), title, pageid))
                for alias, target, fragment in redirects:
                    if target in titles and norm(alias):
                        conn.execute("INSERT OR IGNORE INTO aliases VALUES (?, ?, ?, ?)",
                                     (norm(alias), alias, titles[target], fragment))
                        stats["aliases"] += 1
                first = conn.execute("SELECT 1 FROM meta WHERE key = 'kb_version'").fetchone() is None
                if first or stats["new"] or stats["changed"] or stats["deleted"]:
                    store.set_meta(conn, "kb_version", datetime.now().strftime("%Y%m%d%H%M%S"))
                store.set_meta(conn, "last_update", datetime.now().strftime(TS_FORMAT))
        logger.info("Wiki update done: %s", stats)
        return stats
