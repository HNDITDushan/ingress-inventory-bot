"""
Agent stats and goals.

Ingress Prime's "Copy stats" gives two lines: a header row and a value row,
separated by tabs. Each paste is saved as a snapshot. Goals compare snapshots
to see how much a stat grew this day / week / month / year, or (for lifetime
goals) how close the stat is to a target total.

Nothing in here talks to Telegram, so it can be tested on its own.
"""

import json
import math
import re
from datetime import datetime, timedelta

TEXT_FIELDS = ["Time Span", "Agent Name", "Agent Faction", "Date (yyyy-mm-dd)", "Time (hh:mm:ss)"]

STAT_FIELDS = [
    "Level", "Lifetime AP", "Current AP", "Unique Portals Visited", "Unique Portals Drone Visited",
    "Furthest Drone Distance", "Seer Points", "XM Collected", "OPR Agreements",
    "Portal Scans Uploaded", "Uniques Scout Controlled", "Resonators Deployed", "Links Created",
    "Control Fields Created", "Mind Units Captured", "Longest Link Ever Created",
    "Largest Control Field", "XM Recharged", "Portals Captured", "Unique Portals Captured",
    "Mods Deployed", "Hacks", "Drone Hacks", "Glyph Hack Points", "Completed Hackstreaks",
    "Longest Sojourner Streak", "Resonators Destroyed", "Portals Neutralized",
    "Enemy Links Destroyed", "Enemy Fields Destroyed", "Battle Beacon Combatant",
    "Drones Returned", "Machina Links Destroyed", "Machina Resonators Destroyed",
    "Machina Portals Neutralized", "Machina Portals Reclaimed", "Max Time Portal Held",
    "Max Time Link Maintained", "Max Link Length x Days", "Max Time Field Held",
    "Largest Field MUs x Days", "Forced Drone Recalls", "Distance Walked",
    "Kinetic Capsules Completed", "Unique Missions Completed", "Research Bounties Completed",
    "Research Days Completed", "First Saturday Events", "Second Sunday Events", "OPR Live Events",
    "Orion Tokens", "Orion Link And Field Points", "Apollo Tokens", "Apollo Mod Battle Points",
    "Cygnus Tokens", "Cygnus Battle Beacons", "Recursions", "Months Subscribed",
]
STAT_INDEX = {name: i for i, name in enumerate(STAT_FIELDS)}
KNOWN_HEADERS = sorted(TEXT_FIELDS + STAT_FIELDS, key=len, reverse=True)

# Shown first when picking a stat for a goal, and on the stats summary.
POPULAR_STATS = [
    "Lifetime AP", "Unique Portals Visited", "Unique Portals Captured", "Hacks",
    "Glyph Hack Points", "Resonators Deployed", "Links Created", "Control Fields Created",
    "Mind Units Captured", "Distance Walked", "Portals Captured", "Mods Deployed",
    "Resonators Destroyed", "XM Recharged", "Drone Hacks", "Unique Missions Completed",
]
PICK_ORDER = POPULAR_STATS + [s for s in STAT_FIELDS if s not in POPULAR_STATS]

STAT_UNITS = {
    "Distance Walked": "km", "Longest Link Ever Created": "km", "Furthest Drone Distance": "km",
    "Max Time Portal Held": "days", "Max Time Link Maintained": "days",
    "Max Time Field Held": "days", "Months Subscribed": "months",
}


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Short names and badge names people actually use.
STAT_ALIASES = {
    "ap": "Lifetime AP", "uv": "Unique Portals Visited", "upv": "Unique Portals Visited",
    "uniques": "Unique Portals Visited", "explorer": "Unique Portals Visited",
    "uc": "Unique Portals Captured", "upc": "Unique Portals Captured",
    "pioneer": "Unique Portals Captured", "mu": "Mind Units Captured",
    "mus": "Mind Units Captured", "illuminator": "Mind Units Captured",
    "fields": "Control Fields Created", "mindcontroller": "Control Fields Created",
    "links": "Links Created", "connector": "Links Created", "res": "Resonators Deployed",
    "builder": "Resonators Deployed", "purifier": "Resonators Destroyed",
    "hacker": "Hacks", "glyph": "Glyph Hack Points", "glyphs": "Glyph Hack Points",
    "translator": "Glyph Hack Points", "walk": "Distance Walked", "km": "Distance Walked",
    "trekker": "Distance Walked", "xm": "XM Collected", "recharger": "XM Recharged",
    "liberator": "Portals Captured", "engineer": "Mods Deployed", "seer": "Seer Points",
    "missions": "Unique Missions Completed", "specops": "Unique Missions Completed",
    "maverick": "Drone Hacks", "sojourner": "Longest Sojourner Streak",
}


def lookup_stat(raw: str, available):
    """Match typed text like 'hacks', 'UPV' or 'explorer' to a stat name."""
    key = _norm(raw)
    if not key:
        return None
    available = list(available)
    alias = STAT_ALIASES.get(key)
    if alias in available:
        return alias
    for name in available:
        if _norm(name) == key:
            return name
    partial = [name for name in available if key in _norm(name)]
    return partial[0] if len(partial) == 1 else None


# ---------------------------------------------------------------------------
# Parsing the pasted stats
# ---------------------------------------------------------------------------

class StatsError(Exception):
    """The pasted text isn't usable stats; the message says why."""


def looks_like_stats(text: str) -> bool:
    return text.lstrip().lower().startswith("time span")


def _cells(line: str):
    cells = [c.strip() for c in line.split("\t")]
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _match_headers(line: str):
    """Split a header row whose tabs became spaces, using the known stat names."""
    rest = " ".join(line.split())
    headers = []
    while rest:
        for name in KNOWN_HEADERS:
            if rest == name or rest.startswith(name + " "):
                headers.append(name)
                rest = rest[len(name):].lstrip()
                break
        else:
            return None
    return headers


def parse_stats(text: str) -> dict:
    """Returns {"agent", "faction", "taken_at", "values"}; raises StatsError."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        raise StatsError("I need both lines: the header row and the row of numbers under it.")
    header_line, value_line = lines[0], lines[1]

    if "\t" in header_line and "\t" in value_line:
        headers, values = _cells(header_line), _cells(value_line)
    else:
        headers = _match_headers(header_line)
        if headers is None:
            raise StatsError(
                "I couldn't read the stat names. Please paste the stats exactly as Ingress copies them."
            )
        values = value_line.split()
        if values[:2] == ["ALL", "TIME"]:
            values = ["ALL TIME"] + values[2:]
    if len(values) < len(headers):
        raise StatsError("Some numbers are missing. Please copy the stats again and paste all of it.")

    row = dict(zip(headers, values))
    span = row.get("Time Span", "").upper()
    if span != "ALL TIME":
        raise StatsError(
            f"Those are <b>{span or 'unknown'}</b> stats. In Ingress pick <b>ALL TIME</b> before copying."
        )
    agent = row.get("Agent Name", "").strip()
    if not agent:
        raise StatsError("I couldn't find the agent name in that.")
    try:
        taken_at = datetime.strptime(
            f"{row.get('Date (yyyy-mm-dd)', '')} {row.get('Time (hh:mm:ss)', '')}", "%Y-%m-%d %H:%M:%S"
        )
    except ValueError:
        raise StatsError("I couldn't read the date and time in that.") from None

    numbers = {}
    for header in headers:
        if header in TEXT_FIELDS:
            continue
        value = row[header].replace(",", "")
        if re.fullmatch(r"-?\d+", value):
            numbers[header] = int(value)
    if not numbers:
        raise StatsError("I couldn't find any numbers in that.")
    return {"agent": agent, "faction": row.get("Agent Faction", ""), "taken_at": taken_at, "values": numbers}


AMOUNT_RE = re.compile(r"^(\d+(?:\.\d+)?)([kmb]?)$", re.IGNORECASE)
AMOUNT_SCALE = {"": 1, "k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def parse_amount(text: str):
    """'1500', '1,500', '1.5k', '2M' -> int, or None."""
    m = AMOUNT_RE.match(re.sub(r"[\s,_]", "", text))
    if not m:
        return None
    amount = round(float(m[1]) * AMOUNT_SCALE[m[2].lower()])
    return amount if amount > 0 else None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _ts(dt: datetime) -> str:
    return dt.strftime(TS_FORMAT)


def init_stats_db(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            faction TEXT NOT NULL DEFAULT '',
            UNIQUE(owner_id, name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stat_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            taken_at TEXT NOT NULL,
            saved_at TEXT NOT NULL,
            data TEXT NOT NULL,
            UNIQUE(agent_id, taken_at)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            stat TEXT NOT NULL,
            period TEXT NOT NULL,
            target INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(agent_id, stat, period)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS goal_alerts (
            goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
            period_key TEXT NOT NULL,
            kind TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            PRIMARY KEY (goal_id, period_key, kind)
        )
        """
    )
    agent_columns = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
    if "started_on" not in agent_columns:
        # The day the agent started playing (YYYY-MM-DD), for lifetime averages.
        conn.execute("ALTER TABLE agents ADD COLUMN started_on TEXT")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(goals)")}
    if "note" not in columns:
        conn.execute("ALTER TABLE goals ADD COLUMN note TEXT NOT NULL DEFAULT ''")
    if "start_value" not in columns:
        # Lifetime goals measure progress from the value when they were set.
        conn.execute("ALTER TABLE goals ADD COLUMN start_value INTEGER")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS leaderboards (
            agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
            stat TEXT NOT NULL,
            entries TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (agent_id, stat)
        )
        """
    )


def _snapshot(row):
    if row is None:
        return None
    return {"taken_at": datetime.strptime(row["taken_at"], TS_FORMAT), "values": json.loads(row["data"])}


def list_agents(conn, owner_id: int):
    """The owner's agents, most recently updated first."""
    return conn.execute(
        """
        SELECT a.*, MAX(s.taken_at) AS last_taken
        FROM agents a LEFT JOIN stat_snapshots s ON s.agent_id = a.id
        WHERE a.owner_id = ?
        GROUP BY a.id
        ORDER BY last_taken DESC, a.name COLLATE NOCASE
        """,
        (owner_id,),
    ).fetchall()


def get_agent(conn, owner_id: int, agent_id: int):
    return conn.execute(
        "SELECT * FROM agents WHERE id = ? AND owner_id = ?", (agent_id, owner_id)
    ).fetchone()


def latest_snapshot(conn, agent_id: int, before: datetime = None):
    if before is None:
        row = conn.execute(
            "SELECT * FROM stat_snapshots WHERE agent_id = ? ORDER BY taken_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM stat_snapshots WHERE agent_id = ? AND taken_at < ? "
            "ORDER BY taken_at DESC LIMIT 1",
            (agent_id, _ts(before)),
        ).fetchone()
    return _snapshot(row)


def _first_snapshot_between(conn, agent_id: int, start: datetime, end: datetime):
    row = conn.execute(
        "SELECT * FROM stat_snapshots WHERE agent_id = ? AND taken_at >= ? AND taken_at < ? "
        "ORDER BY taken_at LIMIT 1",
        (agent_id, _ts(start), _ts(end)),
    ).fetchone()
    return _snapshot(row)


def save_snapshot(conn, owner_id: int, parsed: dict, now: datetime):
    """Store a parsed paste. Returns (agent, previous latest snapshot, is_new)."""
    conn.execute(
        "INSERT INTO agents (owner_id, name, faction) VALUES (?, ?, ?) "
        "ON CONFLICT(owner_id, name) DO UPDATE SET faction = excluded.faction",
        (owner_id, parsed["agent"], parsed["faction"]),
    )
    agent = conn.execute(
        "SELECT * FROM agents WHERE owner_id = ? AND name = ?", (owner_id, parsed["agent"])
    ).fetchone()
    previous = latest_snapshot(conn, agent["id"])
    cur = conn.execute(
        "INSERT OR IGNORE INTO stat_snapshots (agent_id, taken_at, saved_at, data) VALUES (?, ?, ?, ?)",
        (agent["id"], _ts(parsed["taken_at"]), _ts(now), json.dumps(parsed["values"])),
    )
    return agent, previous, cur.rowcount == 1


def stat_choices(snapshot):
    """Stats that can be picked for a goal, popular ones first."""
    values = snapshot["values"] if snapshot else {}
    known = [s for s in PICK_ORDER if s in values]
    return known + sorted(s for s in values if s not in STAT_INDEX)


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------

PERIODS = ["daily", "weekly", "monthly", "yearly", "lifetime"]
PERIOD_LABEL = {
    "daily": "Daily", "weekly": "Weekly", "monthly": "Monthly", "yearly": "Yearly", "lifetime": "Lifetime",
}
PERIOD_EMOJI = {"daily": "☀️", "weekly": "📅", "monthly": "🗓", "yearly": "🎆", "lifetime": "🏔"}
PERIOD_NOUN = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}

# Remind about an unfinished goal once this little time is left in its period.
REMIND_BEFORE = {
    "daily": timedelta(hours=3),
    "weekly": timedelta(days=1),
    "monthly": timedelta(days=3),
    "yearly": timedelta(days=14),
}
NEAR_PCT = 80


def period_window(period: str, now: datetime):
    """(start, end, key) of the period containing now; start/end are None for lifetime."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "daily":
        return day, day + timedelta(days=1), day.strftime("%Y-%m-%d")
    if period == "weekly":
        start = day - timedelta(days=day.weekday())
        year, week, _ = start.isocalendar()
        return start, start + timedelta(days=7), f"{year}-W{week:02d}"
    if period == "monthly":
        start = day.replace(day=1)
        return start, (start + timedelta(days=32)).replace(day=1), start.strftime("%Y-%m")
    if period == "yearly":
        start = day.replace(month=1, day=1)
        return start, start.replace(year=start.year + 1), str(start.year)
    return None, None, "lifetime"


def previous_window(period: str, now: datetime):
    start, _, _ = period_window(period, now)
    return period_window(period, start - timedelta(seconds=1))


def gain_between(conn, agent_id: int, stat: str, start: datetime, end: datetime):
    """How much stat grew in [start, end), or None without a snapshot in that range.

    The baseline is the last snapshot before start; without one, the first
    snapshot inside the range is used (so growth before it isn't counted).
    """
    last = latest_snapshot(conn, agent_id, before=end)
    if last is None or last["taken_at"] < start or stat not in last["values"]:
        return None
    base = latest_snapshot(conn, agent_id, before=start)
    if base is None or stat not in base["values"]:
        base = _first_snapshot_between(conn, agent_id, start, end)
    gain = last["values"][stat] - base["values"].get(stat, last["values"][stat])
    return {
        "gain": max(0, gain),
        "base_at": base["taken_at"],
        "last_at": last["taken_at"],
        # False when only one snapshot was available, so nothing could be compared.
        "measured": base["taken_at"] < last["taken_at"],
    }


INGRESS_LAUNCH = datetime(2012, 11, 15)


def parse_start_date(text: str):
    """'2017-03-01', '2017/3/1', '01.03.2017' or '2017-03' -> datetime, or None."""
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y", "%d/%m/%Y", "%Y-%m", "%Y/%m"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def set_start_date(conn, agent_id: int, day: datetime):
    conn.execute("UPDATE agents SET started_on = ? WHERE id = ?", (day.strftime("%Y-%m-%d"), agent_id))


def ap_report(conn, agent, now: datetime):
    """AP gained today / this week / this month, and AP averages.

    Averages use the agent's start date when set (whole career); otherwise
    the AP gained between the first and latest pasted stats.
    """
    report = {}
    for period in ("daily", "weekly", "monthly"):
        start, end, _ = period_window(period, now)
        report[period] = gain_between(conn, agent["id"], "Lifetime AP", start, end)

    latest = latest_snapshot(conn, agent["id"])
    total = latest["values"].get("Lifetime AP") if latest else None
    per_day, since, basis = None, None, None
    started = agent["started_on"] if "started_on" in agent.keys() else None
    if started and total:
        since = datetime.strptime(started, "%Y-%m-%d")
        days = max(1.0, (latest["taken_at"] - since).total_seconds() / 86400)
        per_day, basis = total / days, "career"
    else:
        first = conn.execute(
            "SELECT * FROM stat_snapshots WHERE agent_id = ? ORDER BY taken_at LIMIT 1",
            (agent["id"],),
        ).fetchone()
        first = _snapshot(first) if first else None
        if first and latest and "Lifetime AP" in first["values"] and total:
            days = (latest["taken_at"] - first["taken_at"]).total_seconds() / 86400
            if days >= 1:
                per_day = (total - first["values"]["Lifetime AP"]) / days
                since, basis = first["taken_at"], "tracked"
    report["avg"] = None if per_day is None else {
        "day": round(per_day), "month": round(per_day * 30.44), "year": round(per_day * 365.25),
        "since": since, "basis": basis,
    }
    report["best"] = best_ap_periods(conn, agent["id"])
    return report


def _prev_period(key: str, kind: str) -> str:
    if kind == "day":
        return (datetime.strptime(key, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if kind == "month":
        y, m = map(int, key.split("-"))
        return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
    return str(int(key) - 1)


def best_ap_periods(conn, agent_id: int):
    """{"day"|"month"|"year": {"key", "gain", "partial"}} — the period with the most AP gained.

    A period's gain is its last pasted Lifetime AP minus the last paste of the
    period before; with no paste in the period before, the period's own first
    paste is used and "partial" is True (AP before your first paste isn't counted).
    """
    pastes = []
    for row in conn.execute(
        "SELECT taken_at, data FROM stat_snapshots WHERE agent_id = ? ORDER BY taken_at", (agent_id,)
    ):
        ap = json.loads(row["data"]).get("Lifetime AP")
        if ap is not None:
            pastes.append((row["taken_at"], ap))
    out = {}
    for kind, width in (("day", 10), ("month", 7), ("year", 4)):
        periods = {}
        for taken_at, ap in pastes:
            key = taken_at[:width]
            first, _last = periods.get(key, (ap, ap))
            periods[key] = (first, ap)
        best = None
        for key, (first, last) in periods.items():
            prev = _prev_period(key, kind)
            partial = prev not in periods
            gain = last - (first if partial else periods[prev][1])
            if gain > 0 and (best is None or gain > best["gain"]):
                best = {"key": key, "gain": gain, "partial": partial}
        if best:
            out[kind] = best
    return out


# ---------------------------------------------------------------------------
# Levels and medals (from the Ingress Wiki)
# ---------------------------------------------------------------------------

TIERS = ["Bronze", "Silver", "Gold", "Platinum", "Onyx"]

# level: (AP needed, [(tier, how many medals of that tier or better)])
LEVELS = {
    1: (0, []), 2: (2_500, []), 3: (20_000, []), 4: (70_000, []), 5: (150_000, []),
    6: (300_000, []), 7: (600_000, []), 8: (1_200_000, []),
    9: (2_400_000, [("Gold", 1), ("Silver", 4)]),
    10: (4_000_000, [("Gold", 2), ("Silver", 5)]),
    11: (6_000_000, [("Gold", 4), ("Silver", 6)]),
    12: (8_400_000, [("Gold", 6), ("Silver", 7)]),
    13: (12_000_000, [("Platinum", 1), ("Gold", 7)]),
    14: (17_000_000, [("Platinum", 2)]),
    15: (24_000_000, [("Platinum", 3)]),
    16: (40_000_000, [("Onyx", 2), ("Platinum", 4)]),
}

# Medals that can be read from the stats paste: (medal, stat, tier thresholds).
MEDAL_STATS = [
    ("Explorer", "Unique Portals Visited", [100, 1_000, 2_000, 10_000, 30_000]),
    ("Pioneer", "Unique Portals Captured", [20, 200, 1_000, 5_000, 20_000]),
    ("Liberator", "Portals Captured", [100, 1_000, 5_000, 15_000, 40_000]),
    ("Builder", "Resonators Deployed", [2_000, 10_000, 30_000, 100_000, 200_000]),
    ("Connector", "Links Created", [50, 1_000, 5_000, 25_000, 100_000]),
    ("Mind Controller", "Control Fields Created", [100, 500, 2_000, 10_000, 40_000]),
    ("Illuminator", "Mind Units Captured", [2_000, 50_000, 250_000, 1_000_000, 4_000_000]),
    ("Recharger", "XM Recharged", [100_000, 1_000_000, 3_000_000, 10_000_000, 25_000_000]),
    ("Hacker", "Hacks", [2_000, 10_000, 30_000, 100_000, 200_000]),
    ("Translator", "Glyph Hack Points", [200, 2_000, 6_000, 20_000, 50_000]),
    ("Purifier", "Resonators Destroyed", [2_000, 10_000, 30_000, 100_000, 300_000]),
    ("Engineer", "Mods Deployed", [150, 1_500, 5_000, 20_000, 50_000]),
    ("Trekker", "Distance Walked", [10, 100, 300, 1_000, 2_500]),
    ("SpecOps", "Unique Missions Completed", [5, 25, 100, 200, 500]),
    ("Sojourner", "Longest Sojourner Streak", [15, 30, 60, 180, 360]),
    ("Maverick", "Drone Hacks", [250, 1_000, 2_000, 5_000, 10_000]),
    ("Epoch", "Completed Hackstreaks", [2, 4, 8, 30, 60]),
    ("Scout", "Portal Scans Uploaded", [50, 250, 1_000, 3_000, 6_000]),
    ("Recon", "OPR Agreements", [100, 750, 2_500, 5_000, 10_000]),
    ("First Saturday", "First Saturday Events", [1, 6, 12, 24, 36]),
    ("Second Sunday", "Second Sunday Events", [1, 6, 12, 24, 36]),
]


def medal_progress(values: dict):
    """[(medal, stat, value, tier index or -1, next threshold or None)] for stats we have."""
    out = []
    for medal, stat, thresholds in MEDAL_STATS:
        if stat not in values:
            continue
        v = values[stat]
        tier = sum(1 for t in thresholds if v >= t) - 1
        nxt = thresholds[tier + 1] if tier + 1 < len(thresholds) else None
        out.append((medal, stat, v, tier, nxt))
    return out


def next_level(values: dict):
    """What the agent still needs for the next level, from their stats.

    Returns None at level 16 (or without a level). Medal counts are only those
    readable from stats; event, anniversary and retired medals also count in game.
    """
    level = values.get("Level")
    if not level or level >= 16:
        return None
    target = level + 1
    ap_needed, reqs = LEVELS[target]
    # Levels count Current AP (it restarts at 0 after a Recursion; Lifetime AP doesn't).
    ap = values.get("Current AP", values.get("Lifetime AP", 0))
    progress = medal_progress(values)
    have = {t: sum(1 for p in progress if p[3] >= i) for i, t in enumerate(TIERS)}
    missing = [(tier, n - have[tier]) for tier, n in reqs if have[tier] < n]
    closest = []
    for tier, _short in missing:
        ti = TIERS.index(tier)
        candidates = [p for p in progress if p[3] == ti - 1 and p[4]]
        candidates.sort(key=lambda p: p[2] / p[4], reverse=True)
        closest += [(p[0], p[1], p[2], p[4], tier) for p in candidates[:2]]
    return {
        "level": level, "target": target, "ap": ap, "ap_target": ap_needed,
        "ap_left": max(0, ap_needed - ap), "reqs": reqs, "have": have,
        "missing": missing, "closest": closest,
    }


def list_goals(conn, agent_id: int):
    order = " ".join(f"WHEN '{p}' THEN {i}" for i, p in enumerate(PERIODS))
    return conn.execute(
        f"SELECT * FROM goals WHERE agent_id = ? ORDER BY CASE period {order} END, stat",
        (agent_id,),
    ).fetchall()


def get_goal(conn, owner_id: int, goal_id: int):
    return conn.execute(
        "SELECT g.* FROM goals g JOIN agents a ON a.id = g.agent_id WHERE g.id = ? AND a.owner_id = ?",
        (goal_id, owner_id),
    ).fetchone()


def set_goal(conn, agent_id: int, stat: str, period: str, target: int, now: datetime,
             note: str = "", start_value: int = None):
    """Create the goal, or change the target of the same stat + period. Returns its id."""
    conn.execute(
        "INSERT INTO goals (agent_id, stat, period, target, created_at, note, start_value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(agent_id, stat, period) DO UPDATE SET "
        "target = excluded.target, note = excluded.note, start_value = excluded.start_value",
        (agent_id, stat, period, target, _ts(now), note, start_value),
    )
    goal_id = conn.execute(
        "SELECT id FROM goals WHERE agent_id = ? AND stat = ? AND period = ?", (agent_id, stat, period)
    ).fetchone()["id"]
    # A new target deserves fresh reminders.
    conn.execute("DELETE FROM goal_alerts WHERE goal_id = ?", (goal_id,))
    return goal_id


def delete_goal(conn, goal_id: int):
    conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))


def periodic_goals(conn):
    """Every non-lifetime goal of every user, with the owner and agent name."""
    return conn.execute(
        "SELECT g.*, a.owner_id, a.name AS agent_name FROM goals g JOIN agents a ON a.id = g.agent_id "
        "WHERE g.period != 'lifetime'"
    ).fetchall()


def goal_progress(conn, goal, now: datetime) -> dict:
    start, end, key = period_window(goal["period"], now)
    latest = latest_snapshot(conn, goal["agent_id"])
    base_at = None
    if goal["period"] == "lifetime":
        done = latest["values"].get(goal["stat"], 0) if latest else 0
    else:
        gained = gain_between(conn, goal["agent_id"], goal["stat"], start, end)
        done = gained["gain"] if gained else 0
        base_at = gained["base_at"] if gained else None
    target = goal["target"]
    start_value = goal["start_value"] if goal["period"] == "lifetime" else None
    if start_value is not None and target > start_value:
        pct = max(0, math.floor((done - start_value) * 100 / (target - start_value)))
    else:
        pct = math.floor(done * 100 / target) if target > 0 else 100
    return {
        "key": key,
        "start": start,
        "end": end,
        "done": done,
        "target": target,
        "pct": pct,
        "remaining": max(0, target - done),
        "complete": done >= target,
        "latest_at": latest["taken_at"] if latest else None,
        "base_at": base_at,
    }


def mark_alert(conn, goal_id: int, period_key: str, kind: str, now: datetime) -> bool:
    """Record that an alert was sent; False if it already had been this period."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO goal_alerts (goal_id, period_key, kind, sent_at) VALUES (?, ?, ?, ?)",
        (goal_id, period_key, kind, _ts(now)),
    )
    return cur.rowcount == 1


# ---------------------------------------------------------------------------
# Leaderboards (pasted by the user, e.g. from the-grid.blue)
# ---------------------------------------------------------------------------

_NAME = r"[A-Za-z0-9_]*[A-Za-z][A-Za-z0-9_]*"
_NUMBER = r"\d[\d,]*(?:\.\d+)?\s*[kKmM]?"
# "#4 SRajaneththi 244,060", "🥇 mistybeast - 391642", "cristy92 163477"
ENTRY_LINE_RE = re.compile(
    rf"^\s*(?:#?\d{{1,4}}[.)]?\s+)?(?:[^\w\s]+\s*)?({_NAME})(?:\s*[-–—:|]\s*|\s+)({_NUMBER})\s*$"
)
# The leaderboard page copied as text: name, location and score on separate lines.
NAME_LINE_RE = re.compile(rf"^\s*(?:[^\w\s]+\s*)?({_NAME})\s*$")
NUMBER_LINE_RE = re.compile(rf"^\s*({_NUMBER})\s*$")


def parse_leaderboard(text: str):
    """Pasted leaderboard -> [(name, value), ...] (highest first), or []."""
    found = {}
    pending = None
    for line in text.splitlines():
        m = ENTRY_LINE_RE.match(line)
        if m:
            value = parse_amount(m[2])
            if value is not None:
                found[m[1]] = value
            pending = None
            continue
        m = NUMBER_LINE_RE.match(line)
        if m and pending:
            value = parse_amount(m[1])
            if value is not None:
                found[pending] = value
            pending = None
            continue
        m = NAME_LINE_RE.match(line)
        if m and pending is None:
            pending = m[1]
    return sorted(found.items(), key=lambda kv: -kv[1])


def save_leaderboard(conn, agent_id: int, stat: str, entries, now: datetime):
    conn.execute(
        "INSERT INTO leaderboards (agent_id, stat, entries, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(agent_id, stat) DO UPDATE SET entries = excluded.entries, updated_at = excluded.updated_at",
        (agent_id, stat, json.dumps(entries), _ts(now)),
    )


def _board(row):
    if row is None:
        return None
    return {
        "stat": row["stat"],
        "entries": [tuple(e) for e in json.loads(row["entries"])],
        "updated_at": datetime.strptime(row["updated_at"], TS_FORMAT),
    }


def get_leaderboard(conn, agent_id: int, stat: str):
    return _board(conn.execute(
        "SELECT * FROM leaderboards WHERE agent_id = ? AND stat = ?", (agent_id, stat)
    ).fetchone())


def list_leaderboards(conn, agent_id: int):
    rows = conn.execute("SELECT * FROM leaderboards WHERE agent_id = ? ORDER BY stat", (agent_id,))
    return [_board(row) for row in rows]


def delete_leaderboard(conn, agent_id: int, stat: str):
    conn.execute("DELETE FROM leaderboards WHERE agent_id = ? AND stat = ?", (agent_id, stat))


# ---------------------------------------------------------------------------
# Excel leaderboard exports (the-grid.blue "Rankings" sheet)
# ---------------------------------------------------------------------------

# the-grid's category names that differ from the Ingress stat names.
GRID_CATEGORIES = {
    "topagents": "Lifetime AP", "portalsvisited": "Unique Portals Visited",
    "mucaptured": "Mind Units Captured", "fieldscreated": "Control Fields Created",
    "fieldsdestroyed": "Enemy Fields Destroyed", "linksdestroyed": "Enemy Links Destroyed",
    "longestlink": "Longest Link Ever Created", "timeportalheld": "Max Time Portal Held",
    "timelinkmaintained": "Max Time Link Maintained", "timefieldheld": "Max Time Field Held",
    "linklengthdays": "Max Link Length x Days", "musdays": "Largest Field MUs x Days",
}
_STAT_BY_NORM = {_norm(name): name for name in STAT_FIELDS}
MAX_SHEET_ROWS = 50_000


def grid_stat(category: str):
    """the-grid category -> Ingress stat name, or None for event-only categories."""
    key = _norm(category or "")
    stat = GRID_CATEGORIES.get(key) or _STAT_BY_NORM.get(key)
    return stat if stat in STAT_INDEX and stat not in ("Level", "Current AP") else None


def parse_grid_workbook(data: bytes):
    """Excel export -> ({stat: [(name, value), ...]}, [skipped categories]).

    Uses the first sheet with Category, Agent and Value columns. Raises
    StatsError when there's no such sheet.
    """
    import io
    import openpyxl  # only needed for uploads

    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise StatsError("I couldn't open that file as an Excel workbook.") from exc
    try:
        for ws in wb.worksheets:
            rows = ws.iter_rows(values_only=True)
            header = [_norm(str(c or "")) for c in next(rows, ())]
            try:
                cat_i, name_i, val_i = (header.index(h) for h in ("category", "agent", "value"))
            except ValueError:
                continue
            boards, skipped = {}, []
            for n, row in enumerate(rows):
                if n >= MAX_SHEET_ROWS:
                    break
                if len(row) <= max(cat_i, name_i, val_i):
                    continue
                category, name, value = row[cat_i], row[name_i], row[val_i]
                if not category or not name or value is None:
                    continue
                stat = grid_stat(str(category))
                if stat is None:
                    if category not in skipped:
                        skipped.append(str(category))
                    continue
                amount = value if isinstance(value, (int, float)) else parse_amount(str(value))
                if amount is None:
                    continue
                board = boards.setdefault(stat, {})
                board[str(name).strip()] = int(amount)
            boards = {
                stat: sorted(entries.items(), key=lambda kv: -kv[1])
                for stat, entries in boards.items() if entries
            }
            return boards, skipped
    finally:
        wb.close()
    raise StatsError("I couldn't find a sheet with Category, Agent and Value columns.")


def rivals(board, me: str):
    """The board's entries other than the agent themself."""
    return [(name, value) for name, value in board["entries"] if name.lower() != me.lower()]


def ranking(board, me: str, my_value: int):
    """[(rank, name, value, is_me), ...] with the agent placed by their own latest value."""
    others = rivals(board, me)
    rows = sorted(
        [(name, value, False) for name, value in others] + [(me, my_value, True)],
        key=lambda r: (-r[1], not r[2]),  # on a tie the rival stays ahead
    )
    return [(i + 1, name, value, is_me) for i, (name, value, is_me) in enumerate(rows)]


def my_rank(board, me: str, my_value: int) -> int:
    return 1 + sum(1 for _, value in rivals(board, me) if value >= my_value)


def next_rival(board, me: str, my_value: int):
    """The lowest-scoring rival still at or above my_value, or None if you're top."""
    ahead = [(name, value) for name, value in rivals(board, me) if value >= my_value]
    return min(ahead, key=lambda kv: kv[1]) if ahead else None


def passed_rivals(board, me: str, old_value: int, new_value: int):
    """Rivals overtaken by going from old_value to new_value, highest first."""
    return sorted(
        ((name, value) for name, value in rivals(board, me) if old_value <= value < new_value),
        key=lambda kv: -kv[1],
    )
