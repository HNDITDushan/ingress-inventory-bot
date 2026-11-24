"""Estimate the MU of an Ingress field with the Ingress MU API.

POST https://api.investigatexm.com/getMU      {"coordinates": ["lat,lng", x3]}
POST https://api.investigatexm.com/getMUbulk  {"fields": [[...3 points], ...]}  (1-50 fields)

No key is needed. The API is free and asks for moderate use, so several fields
go in one bulk call. It's an independent reconstruction, so results are estimates.
Docs: https://mu.investigatexm.com/api.html
"""

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

import certifi

API_URL = os.environ.get("MU_API_URL", "https://api.investigatexm.com")
USER_AGENT = "ingress-inventory-bot/1.0 (MU calculator)"
TIMEOUT = 30
MAX_FIELDS = 50
_SSL = ssl.create_default_context(cafile=certifi.where())


class MUAPIError(Exception):
    """The calculation couldn't be done; the message says why, in plain words."""


# ---------------------------------------------------------------------------
# Reading points from what the user sends
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://\S+")
_PAIR_RE = re.compile(r"(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)")
_DECIMAL_PAIR_RE = re.compile(r"(-?\d{1,2}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")


def _valid(lat: float, lng: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lng <= 180


def _from_url(url: str):
    """The point a map link is about: Intel's pll, then ll / q / query / @lat,lng."""
    parts = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parts.query)
    for name in ("pll", "ll", "q", "query", "destination"):
        for value in query.get(name, []):
            m = _PAIR_RE.search(value)
            if m:
                return float(m.group(1)), float(m.group(2))
    lat, lng = query.get("mlat", [None])[0], query.get("mlon", [None])[0]  # OpenStreetMap marker
    if lat and lng:
        try:
            return float(lat), float(lng)
        except ValueError:
            pass
    m = (re.search(r"!3d(-?\d{1,2}\.\d+)!4d(-?\d{1,3}\.\d+)", url)  # Google place pin
         or re.search(r"@(-?\d{1,2}\.\d+),(-?\d{1,3}\.\d+)", url)  # Google map centre
         or re.search(r"#map=\d+/(-?\d{1,2}\.\d+)/(-?\d{1,3}\.\d+)", url)  # OpenStreetMap view
         or _DECIMAL_PAIR_RE.search(urllib.parse.unquote(url)))
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def parse_points(text: str):
    """([(lat, lng), ...] in the order sent, number of map links I couldn't read).

    Understands Intel portal links, Google / Apple / OSM map links with
    coordinates in them, and plain "6.927079, 79.861244" pairs.
    """
    points, unread = [], 0
    for url in _URL_RE.findall(text):
        point = _from_url(url)
        if point and _valid(*point):
            points.append(point)
        else:
            unread += 1
    rest = _URL_RE.sub(" ", text)
    for m in _DECIMAL_PAIR_RE.finditer(rest):
        point = float(m.group(1)), float(m.group(2))
        if _valid(*point):
            points.append(point)
    return points, unread


def has_portal_links(text: str) -> bool:
    """True when the message holds Intel portal links (pll=), the only map links
    clear enough to calculate from without being asked."""
    return "pll=" in text and "intel.ingress.com" in text


def fmt_point(point) -> str:
    return f"{point[0]:.6f},{point[1]:.6f}"


# ---------------------------------------------------------------------------
# Calling the API
# ---------------------------------------------------------------------------

def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{API_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_SSL) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("error", "")
        except ValueError:
            message = ""
        if e.code == 504:
            raise MUAPIError("the MU calculator took too long — try again in a minute.") from None
        if e.code == 400 and message:
            raise MUAPIError(message) from None
        raise MUAPIError(f"the MU calculator answered with error {e.code}. {message}".strip()) from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise MUAPIError(f"I couldn't reach the MU calculator ({getattr(e, 'reason', e)}).") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise MUAPIError("the MU calculator sent something that isn't JSON.") from None


def calculate(fields):
    """[{"points", "ok", "mu", "area_km2", "error"}] for each field of 3 points, in order."""
    if not fields:
        return []
    if len(fields) > MAX_FIELDS:
        raise MUAPIError(f"I can calculate up to {MAX_FIELDS} fields at once.")
    body = {"fields": [[fmt_point(p) for p in f] for f in fields]}
    data = _post("/getMUbulk", body)
    results = []
    by_index = {r.get("index"): r for r in data.get("results") or []}
    for i, field in enumerate(fields):
        r = by_index.get(i) or {"ok": False, "error": "no answer for this field"}
        results.append({
            "points": field,
            "ok": bool(r.get("ok")),
            "mu": r.get("mu"),
            "area_km2": r.get("area_km2"),
            "error": r.get("error", ""),
        })
    return results
