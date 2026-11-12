"""Fetch your stats uploads from the-grid.blue's public API.

GET https://the-grid.blue/api_public.php?action=uploads&from=YYYY-MM-DD&to=YYYY-MM-DD
with header X-API-Key. The key is your own and lives in .env as STATS_API_KEY.

The API returns at most 100 uploads per call, newest first, so older uploads
are fetched by moving the "to" date back to the oldest day returned.
"""

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

import certifi

import stats

API_URL = os.environ.get("STATS_API_URL", "https://the-grid.blue/api_public.php")
USER_AGENT = "ingress-inventory-bot (personal stats sync)"
PAGE_SIZE = 100          # the API's cap per call
MAX_PAGES = 60           # 6,000 uploads per sync at most
PAUSE_SECONDS = 1.0      # between calls, to be gentle on the server
TIMEOUT = 30
_SSL = ssl.create_default_context(cafile=certifi.where())

_STAT_BY_KEY = {stats._norm(name): name for name in stats.STAT_FIELDS}


class GridAPIError(Exception):
    """The API couldn't be used; the message says why, in plain words."""


def env_key():
    """The bot owner's key from .env (used for admins who haven't added one in the bot)."""
    return os.environ.get("STATS_API_KEY", "").strip()


def _get(key: str, start: date, end: date) -> dict:
    query = urllib.parse.urlencode({"action": "uploads", "from": start.isoformat(), "to": end.isoformat()})
    req = urllib.request.Request(f"{API_URL}?{query}", headers={"X-API-Key": key, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("message", "")
        except ValueError:
            message = ""
        if e.code == 401:
            raise GridAPIError("the-grid says this API key is not valid.") from None
        if e.code == 429:
            raise GridAPIError("the-grid's rate limit is reached — try again in a few minutes.") from None
        raise GridAPIError(f"the-grid answered with error {e.code}. {message}".strip()) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise GridAPIError(f"I couldn't reach the-grid ({getattr(e, 'reason', e)}).") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise GridAPIError("the-grid sent something that isn't JSON.") from None
    if data.get("status") != "success":
        raise GridAPIError(f"the-grid said: {data.get('message') or 'unknown error'}")
    return data


KEY_RE = re.compile(r"[A-Za-z0-9_\-]{16,128}")


def looks_like_key(text: str) -> bool:
    return bool(KEY_RE.fullmatch(text.strip()))


def check_key(key: str) -> str:
    """The agent name the key belongs to; raises GridAPIError if it doesn't work."""
    today = date.today()
    data = _get(key, date.fromordinal(today.toordinal() - 1), date.fromordinal(today.toordinal() + 1))
    agent = (data.get("meta") or {}).get("agent_name")
    if not agent:
        raise GridAPIError("the-grid didn't say which agent the key belongs to.")
    return agent


def mask(key: str) -> str:
    return f"••••{key[-4:]}" if len(key) > 8 else "••••"


def to_snapshot(row: dict):
    """One API row -> {"taken_at", "values"} using the bot's stat names, or None."""
    try:
        taken_at = datetime.strptime(f"{row['stat_date']} {row['stat_time']}", stats.TS_FORMAT)
    except (KeyError, TypeError, ValueError):
        return None
    values = {}
    for key, value in row.items():
        name = _STAT_BY_KEY.get(stats._norm(key))
        if name and isinstance(value, int) and not isinstance(value, bool):
            values[name] = value
    return {"taken_at": taken_at, "values": values} if values else None


def fetch_uploads(key: str, start: date, end: date):
    """(agent name, [snapshots oldest first], complete?) for uploads between two days.

    complete is False when MAX_PAGES was hit, or one day had more uploads than a page holds.
    """
    if not key:
        raise GridAPIError("No API key — add one first.")
    seen, agent, complete = {}, None, True
    to_day = end
    for page in range(MAX_PAGES):
        if page:
            time.sleep(PAUSE_SECONDS)
        data = _get(key, start, to_day)
        meta = data.get("meta") or {}
        agent = agent or meta.get("agent_name")
        rows = data.get("data") or []
        for row in rows:
            snap = to_snapshot(row)
            if snap:
                seen[snap["taken_at"]] = snap
        if len(rows) < PAGE_SIZE:
            break
        oldest = min(r.get("stat_date", "") for r in rows)
        oldest_day = date.fromisoformat(oldest)
        if oldest_day >= to_day:
            # A whole page from one day: the API can't page within a day.
            if oldest_day <= start:
                complete = False
                break
            to_day = date.fromordinal(oldest_day.toordinal() - 1)
            complete = False
        else:
            to_day = oldest_day  # this day may have more uploads; duplicates are dropped
    else:
        complete = False
    if not agent:
        raise GridAPIError("the-grid didn't say which agent the key belongs to.")
    return agent, [seen[t] for t in sorted(seen)], complete


# ---------------------------------------------------------------------------
# The old grid: the-grid.org export
# GET https://the-grid.org/r/export/?key=KEY&t0=UNIX&t1=UNIX&dir=asc
# -> [{"id", "timestamp" (unix, upload time), "stats": {...}}]; a bad key gives null.
# It has no agent name or level.
# ---------------------------------------------------------------------------

OLD_API_URL = os.environ.get("OLD_GRID_URL", "https://the-grid.org/r/export/")
OLD_CHUNK_DAYS = 365   # one request per year of history

# Old stat names that differ from the Ingress names.
OLD_GRID_KEYS = {
    "ap": "Current AP", "portals_visited": "Unique Portals Visited",
    "furthest_drone_flight_distance": "Furthest Drone Distance", "seer": "Seer Points",
    "ar_videos_uploaded": "Portal Scans Uploaded",
    "scout_controller_on_unique_portals": "Uniques Scout Controlled",
    "fields_created": "Control Fields Created", "mu_captured": "Mind Units Captured",
    "longest_link": "Longest Link Ever Created", "largest_field": "Largest Control Field",
    "links_destroyed": "Enemy Links Destroyed", "fields_destroyed": "Enemy Fields Destroyed",
    "link_maintained_max_days": "Max Time Link Maintained",
    "link_length_x_days_max": "Max Link Length x Days", "field_held_max_days": "Max Time Field Held",
    "largest_field_x_days_max": "Largest Field MUs x Days", "glyph_points": "Glyph Hack Points",
    "hacking_streak_max_days": "Longest Sojourner Streak",
}


def _get_old(key: str, t0: int, t1: int):
    query = urllib.parse.urlencode({"key": key, "t0": t0, "t1": t1, "dir": "asc"})
    req = urllib.request.Request(f"{OLD_API_URL}?{query}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=90, context=_SSL) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise GridAPIError("the-grid.org says this key is not valid.") from None
        if e.code == 429:
            raise GridAPIError("the-grid.org's rate limit is reached — try again in a few minutes.") from None
        raise GridAPIError(f"the-grid.org answered with error {e.code}.") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise GridAPIError(f"I couldn't reach the-grid.org ({getattr(e, 'reason', e)}).") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise GridAPIError("the-grid.org sent something that isn't JSON.") from None
    if data is None:
        raise GridAPIError("the-grid.org says this key is not valid.")
    if not isinstance(data, list):
        raise GridAPIError("the-grid.org sent an answer I don't understand.")
    return data


def old_to_snapshot(row: dict):
    try:
        taken_at = datetime.fromtimestamp(int(row["timestamp"])).replace(microsecond=0)
        raw = row["stats"]
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    values = {}
    for key, value in (raw or {}).items():
        name = OLD_GRID_KEYS.get(key) or _STAT_BY_KEY.get(stats._norm(key))
        if name and isinstance(value, int) and not isinstance(value, bool):
            values[name] = value
    return {"taken_at": taken_at, "values": values} if values else None


def check_old_key(key: str) -> int:
    """How many uploads the key has in the last 30 days; raises GridAPIError if it doesn't work."""
    now = int(time.time())
    return len(_get_old(key, now - 30 * 86400, now + 86400))


def fetch_old_uploads(key: str, start: datetime, end: datetime):
    """[snapshots oldest first] from the-grid.org between two times."""
    if not key:
        raise GridAPIError("No the-grid.org key — add one first.")
    t0, t1 = int(start.timestamp()), int(end.timestamp())
    seen = {}
    first = True
    while t0 < t1:
        chunk_end = min(t1, t0 + OLD_CHUNK_DAYS * 86400)
        if not first:
            time.sleep(PAUSE_SECONDS)
        first = False
        for row in _get_old(key, t0, chunk_end):
            snap = old_to_snapshot(row)
            if snap:
                seen[snap["taken_at"]] = snap
        t0 = chunk_end
    return [seen[t] for t in sorted(seen)]
