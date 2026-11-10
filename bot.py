"""
Telegram bot for managing inventory across multiple Ingress Prime accounts.

Everything can be done with buttons from /menu:
  - create accounts (one per agent you play) and switch the "active" one
  - add / remove gear with tap-to-pick menus and +1/+5/+10... buttons
  - view the active account, or every account plus combined totals

Power users can also just type lines like "R8 10", "-3 VRPS" or "Keys =120"
to change the active account's inventory directly.

Data is stored per Telegram user, so each person who talks to the bot
has their own set of accounts/items (isolated by telegram_id).

Setup:
  pip install -r requirements.txt
  set TELEGRAM_BOT_TOKEN env var (see .env.example), then:
  python bot.py
"""

import asyncio
import difflib
import html
import logging
import math
import os
import random
import re
import sqlite3
from contextlib import closing
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

import stats
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "inventory.db")
STATE_PATH = os.path.join(BASE_DIR, "bot_state.pickle")

load_dotenv(os.path.join(BASE_DIR, ".env"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

MAX_ACCOUNT_NAME = 64
MAX_DESCRIPTION = 500
MAX_MESSAGE_LEN = 4000  # Telegram's hard limit is 4096


# ---------------------------------------------------------------------------
# Fixed Ingress Prime item catalog
# ---------------------------------------------------------------------------

CATEGORIES = [
    ("Resonators", ["R1", "R2", "R3", "R4", "R5", "R6", "R7", "R8"]),
    ("XMP Bursters", ["X1", "X2", "X3", "X4", "X5", "X6", "X7", "X8"]),
    ("Ultra Strikes", ["U1", "U2", "U3", "U4", "U5", "U6", "U7", "U8"]),
    (
        "Portal Shields",
        [
            "Common Portal Shield",
            "Rare Portal Shield",
            "Very Rare Portal Shield",
            "Aegis Portal Shield",
        ],
    ),
    ("Mods", ["Force Amp", "Turret"]),
    (
        "Power Cubes",
        ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "Hyper Cube"],
    ),
    ("Heat Sinks", ["Common Heat Sink", "Rare Heat Sink", "Very Rare Heat Sink"]),
    (
        "Multi-Hacks",
        ["Common Multi-Hack", "Rare Multi-Hack", "Very Rare Multi-Hack"],
    ),
    ("Link Amps", ["Common Link Amp", "Rare Link Amp", "Soft Bank Ultra Link"]),
    ("Ops & Keys", ["ITO +", "ITO -", "Keys"]),
    # Keep new categories last: buttons refer to items by their position.
    ("Viruses", ["ADA Refactor", "JARVIS Virus"]),
]

ITEMS = [item for _, items in CATEGORIES for item in items]
ITEM_INDEX = {item: i for i, item in enumerate(ITEMS)}
ITEM_CATEGORY = {item: cat for cat, items in CATEGORIES for item in items}
CATEGORY_INDEX = {cat: i for i, (cat, _) in enumerate(CATEGORIES)}

CATEGORY_EMOJI = {
    "Resonators": "🔵",
    "XMP Bursters": "💥",
    "Ultra Strikes": "⚡",
    "Portal Shields": "🛡️",
    "Mods": "🔧",
    "Power Cubes": "🔋",
    "Heat Sinks": "❄️",
    "Multi-Hacks": "🔓",
    "Link Amps": "📡",
    "Ops & Keys": "🔑",
    "Viruses": "🦠",
}

# Shorter labels used where the category is already shown (buttons, lists).
SHORT_NAME = {
    "Common Portal Shield": "Common",
    "Rare Portal Shield": "Rare",
    "Very Rare Portal Shield": "Very Rare",
    "Aegis Portal Shield": "Aegis",
    "Common Heat Sink": "Common",
    "Rare Heat Sink": "Rare",
    "Very Rare Heat Sink": "Very Rare",
    "Common Multi-Hack": "Common",
    "Rare Multi-Hack": "Rare",
    "Very Rare Multi-Hack": "Very Rare",
    "Common Link Amp": "Common",
    "Rare Link Amp": "Rare",
    "Soft Bank Ultra Link": "SBUL",
    "ADA Refactor": "ADA",
    "JARVIS Virus": "JARVIS",
}


def _norm(name: str) -> str:
    return re.sub(r"[\s_]+", "", name.lower())


# Common Ingress abbreviations, so people can type "VRPS 3" instead of
# "Very Rare Portal Shield 3". Keys are already normalized.
ALIASES = {
    "cps": "Common Portal Shield",
    "commonshield": "Common Portal Shield",
    "rps": "Rare Portal Shield",
    "rareshield": "Rare Portal Shield",
    "vrps": "Very Rare Portal Shield",
    "vrshield": "Very Rare Portal Shield",
    "veryrareshield": "Very Rare Portal Shield",
    "aegis": "Aegis Portal Shield",
    "axa": "Aegis Portal Shield",
    "aegisshield": "Aegis Portal Shield",
    "fa": "Force Amp",
    "hc": "Hyper Cube",
    "hyper": "Hyper Cube",
    "chs": "Common Heat Sink",
    "rhs": "Rare Heat Sink",
    "vrhs": "Very Rare Heat Sink",
    "cmh": "Common Multi-Hack",
    "rmh": "Rare Multi-Hack",
    "vrmh": "Very Rare Multi-Hack",
    "cla": "Common Link Amp",
    "rla": "Rare Link Amp",
    "sbul": "Soft Bank Ultra Link",
    "softbank": "Soft Bank Ultra Link",
    "softbankultralink": "Soft Bank Ultra Link",
    "itoplus": "ITO +",
    "itominus": "ITO -",
    "key": "Keys",
    "portalkey": "Keys",
    "portalkeys": "Keys",
    "ada": "ADA Refactor",
    "jarvis": "JARVIS Virus",
    "javis": "JARVIS Virus",
    "javisvirus": "JARVIS Virus",
}
for _lvl in range(1, 9):
    for _prefix, _letter in (
        ("res", "R"), ("reso", "R"), ("resonator", "R"),
        ("xmp", "X"), ("burster", "X"),
        ("us", "U"), ("ultra", "U"), ("ultrastrike", "U"),
        ("pc", "P"), ("cube", "P"), ("powercube", "P"),
    ):
        ALIASES[f"{_prefix}{_lvl}"] = f"{_letter}{_lvl}"

ITEM_LOOKUP = {}
for _item in ITEMS:
    ITEM_LOOKUP[_norm(_item)] = _item
    if not _item.endswith("-"):
        ITEM_LOOKUP[_norm(_item).replace("-", "")] = _item
ITEM_LOOKUP.update(ALIASES)


def lookup_item(raw: str):
    return ITEM_LOOKUP.get(_norm(raw))


def suggest_item(raw: str):
    """Closest catalog item for a misspelt name ("vrsp" -> Very Rare Portal Shield), or None."""
    words = [w for w in raw.replace(",", " ").replace(":", " ").split() if not QTY_RE.match(w)]
    key = _norm(" ".join(words)).replace("-", "")
    if len(key) < 2:
        return None
    match = difflib.get_close_matches(key, list(ITEM_LOOKUP), n=1, cutoff=0.6)
    return ITEM_LOOKUP[match[0]] if match else None


# Short words that mean a whole family of items when searching.
SEARCH_FAMILIES = {
    "r": "Resonators", "res": "Resonators", "reso": "Resonators",
    "x": "XMP Bursters", "xmp": "XMP Bursters", "burster": "XMP Bursters",
    "u": "Ultra Strikes", "us": "Ultra Strikes", "ultra": "Ultra Strikes",
    "p": "Power Cubes", "pc": "Power Cubes", "cube": "Power Cubes",
}


def search_items(query: str):
    """Items a search means: one exact item, a family, or every name containing the text."""
    exact = lookup_item(query)
    if exact:
        return [exact]
    q = _norm(query).replace("-", "")
    family = SEARCH_FAMILIES.get(q)
    if family:
        return list(dict(CATEGORIES)[family])
    if len(q) < 2:
        return []
    return [
        item
        for item in ITEMS
        if q in _norm(item).replace("-", "") or q in _norm(ITEM_CATEGORY[item]).replace("-", "")
    ]


# Quantity tokens: "5", "+5", "-5", "=20", "5x", "x5".
QTY_RE = re.compile(r"^([+\-=]?)x?(\d{1,6})x?$", re.IGNORECASE)


def parse_qty(token: str, default_op: str = "add"):
    """Return (op, amount) where op is "add", "remove" or "set", or None."""
    m = QTY_RE.match(token.strip())
    if not m:
        return None
    sign, amount = m.group(1), int(m.group(2))
    op = {"+": "add", "-": "remove", "=": "set"}.get(sign, default_op)
    return op, amount


def parse_line(line: str, default_op: str = "add"):
    """Parse "R8 10", "10 R8", "-3 VRPS", "Keys =120" -> (item, op, amount)."""
    tokens = line.replace(",", " ").replace(":", " ").split()
    if len(tokens) < 2:
        return None
    for qty_token, rest in ((tokens[-1], tokens[:-1]), (tokens[0], tokens[1:])):
        qty = parse_qty(qty_token, default_op)
        if qty is None:
            continue
        item = lookup_item(" ".join(rest))
        if item:
            return item, qty[0], qty[1]
    return None


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(get_conn()) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                current_account_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                UNIQUE(owner_id, name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 0,
                UNIQUE(account_id, name)
            )
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
        if "description" not in columns:
            conn.execute("ALTER TABLE accounts ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        stats.init_stats_db(conn)


def ensure_user(conn, telegram_id: int):
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO users (telegram_id, current_account_id) VALUES (?, NULL)",
            (telegram_id,),
        )
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row


def list_accounts(conn, owner_id: int):
    return conn.execute(
        "SELECT * FROM accounts WHERE owner_id = ? ORDER BY name COLLATE NOCASE",
        (owner_id,),
    ).fetchall()


def get_account_by_name(conn, owner_id: int, name: str):
    return conn.execute(
        "SELECT * FROM accounts WHERE owner_id = ? AND name = ? COLLATE NOCASE",
        (owner_id, name),
    ).fetchone()


def get_owned_account(conn, owner_id: int, account_id: int):
    return conn.execute(
        "SELECT * FROM accounts WHERE id = ? AND owner_id = ?", (account_id, owner_id)
    ).fetchone()


def get_current_account(conn, telegram_id: int):
    user = ensure_user(conn, telegram_id)
    if user["current_account_id"] is None:
        return None
    return get_owned_account(conn, telegram_id, user["current_account_id"])


def set_active(conn, telegram_id: int, account_id):
    conn.execute(
        "UPDATE users SET current_account_id = ? WHERE telegram_id = ?",
        (account_id, telegram_id),
    )


def clean_account_name(raw: str):
    """Return (name, error)."""
    name = " ".join(raw.split())
    if not name:
        return None, "The name can't be empty."
    if name.startswith("/"):
        return None, "The name can't start with /."
    if len(name) > MAX_ACCOUNT_NAME:
        return None, f"Please keep the name under {MAX_ACCOUNT_NAME} characters."
    return name, None


def create_account(conn, owner_id: int, raw_name: str):
    """Create an account and make it active. Returns (account, error)."""
    name, error = clean_account_name(raw_name)
    if error:
        return None, error
    if get_account_by_name(conn, owner_id, name):
        return None, f"You already have an account called <b>{esc(name)}</b>."
    cur = conn.execute(
        "INSERT INTO accounts (owner_id, name) VALUES (?, ?)", (owner_id, name)
    )
    set_active(conn, owner_id, cur.lastrowid)
    return get_owned_account(conn, owner_id, cur.lastrowid), None


def rename_account(conn, owner_id: int, account, raw_name: str):
    """Returns (new_name, error)."""
    name, error = clean_account_name(raw_name)
    if error:
        return None, error
    existing = get_account_by_name(conn, owner_id, name)
    if existing and existing["id"] != account["id"]:
        return None, f"You already have an account called <b>{esc(name)}</b>."
    conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name, account["id"]))
    return name, None


def delete_account(conn, owner_id: int, account):
    """Delete an account; if it was active, fall back to another account."""
    conn.execute("DELETE FROM accounts WHERE id = ?", (account["id"],))
    user = ensure_user(conn, owner_id)
    if user["current_account_id"] == account["id"]:
        remaining = list_accounts(conn, owner_id)
        set_active(conn, owner_id, remaining[0]["id"] if remaining else None)


def get_counts(conn, account_id: int) -> dict:
    rows = conn.execute(
        "SELECT name, quantity FROM items WHERE account_id = ? AND quantity > 0",
        (account_id,),
    ).fetchall()
    return {r["name"]: r["quantity"] for r in rows}


def get_item_qty(conn, account_id: int, item_name: str) -> int:
    row = conn.execute(
        "SELECT quantity FROM items WHERE account_id = ? AND name = ?",
        (account_id, item_name),
    ).fetchone()
    return row["quantity"] if row else 0


def apply_change(conn, account_id: int, item_name: str, op: str, amount: int):
    """Apply add/remove/set to one item, never going below zero. Returns (old, new)."""
    old = get_item_qty(conn, account_id, item_name)
    if op == "add":
        new = old + amount
    elif op == "remove":
        new = max(old - amount, 0)
    else:
        new = amount
    if new <= 0:
        conn.execute(
            "DELETE FROM items WHERE account_id = ? AND name = ?", (account_id, item_name)
        )
    elif old == 0:
        conn.execute(
            "INSERT OR REPLACE INTO items (account_id, name, quantity) VALUES (?, ?, ?)",
            (account_id, item_name, new),
        )
    else:
        conn.execute(
            "UPDATE items SET quantity = ? WHERE account_id = ? AND name = ?",
            (new, account_id, item_name),
        )
    return old, new


def replace_inventory(conn, account_id: int, counts: dict):
    """Make the account hold exactly `counts`; anything not listed becomes 0."""
    conn.execute("DELETE FROM items WHERE account_id = ?", (account_id,))
    conn.executemany(
        "INSERT INTO items (account_id, name, quantity) VALUES (?, ?, ?)",
        [(account_id, name, qty) for name, qty in counts.items() if qty > 0],
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def esc(text) -> str:
    return html.escape(str(text), quote=False)


def plural(n: int, word: str = "item") -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def keyboard(*rows):
    """keyboard([("Label", "data"), ...], ...) -> InlineKeyboardMarkup; empty rows skipped."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows if row]
    )


def chunked(buttons, per_row):
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


HOME = ("🏠 Menu", "menu")
UNDO = ("↩️ Undo", "undo")


def with_row(markup, row, first: bool = True):
    """Return `markup` with an extra row of (label, data) buttons on top (or bottom)."""
    extra = (tuple(InlineKeyboardButton(label, callback_data=data) for label, data in row),)
    rows = tuple(markup.inline_keyboard) if markup else ()
    return InlineKeyboardMarkup(extra + rows if first else rows + extra)


# Quick buttons that stay at the bottom of the chat (Telegram's reply keyboard).
QUICK_KEYS = {
    "📦 Inventory": "inv",
    "🔍 Search": "search",
    "📊 Stats": "stats",
    "🏠 Menu": "menu",
}
QUICK_KB_VERSION = 1


def quick_keyboard():
    labels = list(QUICK_KEYS)
    return ReplyKeyboardMarkup(
        [labels[:2], labels[2:]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Type e.g. R8 10, or tap a button",
    )


# ---------------------------------------------------------------------------
# Undo: the last inventory change can be taken back with one tap
# ---------------------------------------------------------------------------

def snapshot(conn, account_ids):
    return {aid: get_counts(conn, aid) for aid in account_ids}


def remember_undo(ud, before: dict, label: str, merge_key=None, created=()):
    """Remember how accounts looked before a change.

    Repeated taps on the same item screen (same merge_key) keep the first
    snapshot, so one Undo takes back the whole run of +1/+5 taps.
    """
    prev = ud.get("undo")
    if merge_key and prev and prev.get("merge_key") == merge_key:
        return
    ud["undo"] = {"before": before, "label": label, "merge_key": merge_key, "created": list(created)}


def undo_last(conn, uid: int, ud):
    """Restore the remembered state. Returns (label, account ids restored) or None."""
    undo = ud.pop("undo", None)
    if not undo:
        return None
    restored = []
    for aid, counts in undo["before"].items():
        if get_owned_account(conn, uid, aid):
            replace_inventory(conn, aid, counts)
            restored.append(aid)
    for aid in undo["created"]:
        account = get_owned_account(conn, uid, aid)
        if account:
            delete_account(conn, uid, account)
    return undo["label"], restored


def inventory_lines(counts: dict):
    lines = []
    for cat, cat_items in CATEGORIES:
        present = [(it, counts[it]) for it in cat_items if counts.get(it)]
        if not present:
            continue
        total = sum(q for _, q in present)
        lines.append(f"{CATEGORY_EMOJI[cat]} <b>{esc(cat)}</b> ({total})")
        lines.append(
            "└ " + "  ·  ".join(f"{esc(SHORT_NAME.get(it, it))}: <b>{q}</b>" for it, q in present)
        )
    extras = sorted((n, q) for n, q in counts.items() if n not in ITEM_CATEGORY)
    if extras:
        lines.append("❔ <b>Other</b>")
        lines.append("└ " + "  ·  ".join(f"{esc(n)}: <b>{q}</b>" for n, q in extras))
    return lines


def account_block(conn, account, active: bool) -> str:
    counts = get_counts(conn, account["id"])
    total = sum(counts.values())
    header = f"👤 <b>{esc(account['name'])}</b>{'  ✅ active' if active else ''}"
    if account["description"]:
        header += f"\n📝 {esc(account['description'])}"
    header += f"\n📦 {plural(total)}"
    if not counts:
        return header + "\n<i>Empty — tap 📥 Add items to fill it.</i>"
    return header + "\n\n" + "\n".join(inventory_lines(counts))


def change_note(item: str, op: str, amount: int, old: int, new: int) -> str:
    if new == old:
        return f"ℹ️ No change — {esc(item)} is still <b>{new}</b>."
    diff = new - old
    sign = "+" if diff > 0 else "−"
    note = f"✅ {sign}{abs(diff)} {esc(item)} → now <b>{new}</b>"
    if op == "remove" and amount > old:
        note += f" (you only had {old})"
    return note


def visible_len(text: str) -> int:
    """Length as Telegram counts it: without HTML tags, entities as one character."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def split_message(text: str):
    if visible_len(text) <= MAX_MESSAGE_LEN:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        if current and visible_len(current) + visible_len(block) + 2 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    if all(visible_len(c) <= MAX_MESSAGE_LEN for c in chunks):
        return chunks
    return split_lines(text)


def split_lines(text: str):
    chunks, current = [], ""
    for part in text.split("\n"):
        if current and len(current) + len(part) + 1 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = part
        else:
            current = f"{current}\n{part}" if current else part
    if current:
        chunks.append(current)
    return chunks


async def respond(update: Update, text: str, markup=None):
    """Edit the tapped message (for buttons) or reply (for typed messages)."""
    chunks = split_message(text)
    query = update.callback_query
    if query:
        first_markup = markup if len(chunks) == 1 else None
        try:
            await query.edit_message_text(
                chunks[0], parse_mode=ParseMode.HTML, reply_markup=first_markup
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                await query.message.reply_text(
                    chunks[0], parse_mode=ParseMode.HTML, reply_markup=first_markup
                )
        rest = chunks[1:]
        message = query.message
    else:
        rest = chunks
        message = update.effective_message
    for i, chunk in enumerate(rest):
        await message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            reply_markup=markup if i == len(rest) - 1 else None,
        )


# ---------------------------------------------------------------------------
# Screens: each returns (text, keyboard)
# ---------------------------------------------------------------------------

def welcome_screen():
    text = (
        "👋 <b>Welcome, Agent!</b>\n\n"
        "I keep track of the gear in each of your Ingress accounts — "
        "resonators, bursters, cubes, keys and more.\n\n"
        "Let's start by creating your first account. Your agent name works great."
    )
    return text, keyboard(
        [("➕ Create my first account", "new")],
        [("📥 Import all accounts", "import")],
        [("❓ How it works", "help")],
    )


TIPS = [
    "Just send me <code>R8 10</code> to add 10 R8 to the active account.",
    "Send <code>-3 VRPS</code> to remove 3 Very Rare Portal Shields.",
    "Send <code>Keys =120</code> to set Keys to exactly 120.",
    "Send several lines at once, like <code>R8 10</code> and <code>X8 20</code>, to update many items.",
    "Made a mistake? Tap ↩️ Undo right after a change to take it back.",
    "Paste your ALL TIME stats from Ingress any time — I'll show what grew and track your goals.",
    "Send just an item name, like <code>HC</code>, to open its +/− buttons.",
    "Use the quick buttons at the bottom of the chat to jump to your inventory, search or stats.",
]


def menu_screen(conn, uid: int):
    accounts = list_accounts(conn, uid)
    if not accounts:
        return welcome_screen()
    active = get_current_account(conn, uid)
    lines = ["🏠 <b>Ingress Inventory</b>", ""]
    if active:
        total = sum(get_counts(conn, active["id"]).values())
        lines.append(f"👤 Active account: <b>{esc(active['name'])}</b> · {plural(total)}")
        if active["description"]:
            lines.append(f"📝 {esc(active['description'])}")
    else:
        lines.append("👤 No active account — tap 🔄 Switch account to pick one.")
    lines.append(f"🗂 You have {plural(len(accounts), 'account')}")
    agents = stats.list_agents(conn, uid)
    if agents and agents[0]["last_taken"]:
        snap = stats.latest_snapshot(conn, agents[0]["id"])
        lines.append(f"📊 Last stats: {fmt_ago(snap['taken_at'], datetime.now())}")
    lines += ["", f"💡 <i>Tip:</i> {random.choice(TIPS)}"]

    switch_label = f"🔄 Switch ({active['name']})" if active else "🔄 Switch account"
    markup = keyboard(
        [("📥 Add items", "cats:a"), ("📤 Remove items", "cats:r")],
        [("📦 My inventory", "inv"), ("🗂 All accounts", "all")],
        [("🔍 Search", "search"), ("📊 Stats & goals", "stats")],
        [(switch_label, "switch"), ("➕ New account", "new")],
        [("🧰 More tools", "more"), ("❓ Help", "help")],
    )
    return "\n".join(lines), markup


def more_screen(conn, uid: int):
    active = get_current_account(conn, uid)
    name = f" ({esc(active['name'])})" if active else ""
    text = (
        "🧰 <b>More tools</b>\n\n"
        f"🧾 <b>Sync this account</b>{name} — paste your full list and the account is set to match it.\n"
        "📥 <b>Import all accounts</b> — paste every account in one message.\n"
        "⚙️ <b>Manage accounts</b> — rename, describe or delete accounts.\n"
        "📋 <b>Item names</b> — every item and the shortcuts you can type."
    )
    return text, keyboard(
        [("🧾 Sync this account", "sync"), ("📥 Import all accounts", "import")],
        [("⚙️ Manage accounts", "manage"), ("📋 Item names", "catalog")],
        [HOME],
    )


def no_active_screen(conn, uid: int):
    if not list_accounts(conn, uid):
        return welcome_screen()
    return (
        "👤 You don't have an active account right now. Pick the one you're playing:",
        keyboard([("🔄 Pick an account", "switch")], [HOME]),
    )


def categories_screen(conn, account, mode: str):
    counts = get_counts(conn, account["id"])
    name = esc(account["name"])
    if mode == "a":
        header = f"📥 <b>Add items</b> to {name}"
    else:
        header = f"📤 <b>Remove items</b> from {name}"
        if not counts:
            return (
                f"{header}\n\nThis account is empty — nothing to remove yet.",
                keyboard([("📥 Add items instead", f"cats:a:{account['id']}")], [HOME]),
            )

    buttons = []
    for i, (cat, cat_items) in enumerate(CATEGORIES):
        total = sum(counts.get(it, 0) for it in cat_items)
        if mode == "r" and total == 0:
            continue
        label = f"{CATEGORY_EMOJI[cat]} {cat}" + (f" · {total}" if total else "")
        buttons.append((label, f"cat:{mode}:{account['id']}:{i}"))

    other_mode, other_label = ("r", "🔁 Switch to Remove") if mode == "a" else ("a", "🔁 Switch to Add")
    return (
        f"{header}\n\nPick a category:",
        keyboard(
            *chunked(buttons, 2),
            [(other_label, f"cats:{other_mode}:{account['id']}"), ("✅ Done", f"inv:{account['id']}")],
        ),
    )


def items_screen(conn, account, mode: str, cat_idx: int):
    cat, cat_items = CATEGORIES[cat_idx]
    counts = get_counts(conn, account["id"])
    shown = cat_items if mode == "a" else [it for it in cat_items if counts.get(it)]
    back_row = [("⬅️ Categories", f"cats:{mode}:{account['id']}"), ("✅ Done", f"inv:{account['id']}")]
    if not shown:
        return (
            f"{CATEGORY_EMOJI[cat]} No {esc(cat)} left in {esc(account['name'])}.",
            keyboard(back_row),
        )

    buttons = []
    for it in shown:
        short = SHORT_NAME.get(it, it)
        qty = counts.get(it, 0)
        buttons.append((f"{short} · {qty}" if qty else short, f"it:{mode}:{account['id']}:{ITEM_INDEX[it]}"))
    per_row = 4 if all(len(SHORT_NAME.get(it, it)) <= 3 for it in shown) else 2
    verb = "add" if mode == "a" else "remove"
    return (
        f"{CATEGORY_EMOJI[cat]} <b>{esc(cat)}</b> · {esc(account['name'])}\n\nTap an item to {verb}:",
        keyboard(*chunked(buttons, per_row), back_row),
    )


QUICK_STEPS = [1, 5, 10, 25, 50, 100]


def quantity_screen(conn, account, mode: str, item: str, note: str = ""):
    have = get_item_qty(conn, account["id"], item)
    cat = ITEM_CATEGORY[item]
    base = f"q:{mode}:{account['id']}:{ITEM_INDEX[item]}"

    plus = [(f"+{n}", f"{base}:{n}") for n in QUICK_STEPS]
    plus_rows = chunked(plus, 3)
    minus_rows = []
    if have > 0:
        minus = [(f"−{n}", f"{base}:-{n}") for n in QUICK_STEPS[:5]] + [("🗑 All", f"{base}:all")]
        minus_rows = chunked(minus, 3)
    rows = plus_rows + minus_rows if mode == "a" else minus_rows + plus_rows

    verb = "add" if mode == "a" else "remove"
    text = (
        f"{CATEGORY_EMOJI[cat]} <b>{esc(item)}</b> · {esc(account['name'])}\n\n"
        f"You have: <b>{have}</b>\n"
        + (f"\n{note}\n" if note else "")
        + f"\nTap a button, or type a number to {verb}.\n"
        "<i>Type</i> <code>=20</code> <i>to set the exact count.</i>"
    )
    nav = [("⬅️ Back", f"cat:{mode}:{account['id']}:{CATEGORY_INDEX[cat]}"), ("✅ Done", f"inv:{account['id']}")]
    return text, keyboard(*rows, [UNDO] if note else [], nav)


def inventory_screen(conn, uid: int, account, note: str = "", undo: bool = False):
    active = get_current_account(conn, uid)
    is_active = active is not None and active["id"] == account["id"]
    return (
        (f"{note}\n\n" if note else "") + account_block(conn, account, is_active),
        keyboard(
            [UNDO] if undo else [],
            [] if is_active else [("✅ Make this account active", f"use:{account['id']}")],
            [("📥 Add", f"cats:a:{account['id']}"), ("📤 Remove", f"cats:r:{account['id']}")],
            [("🧾 Sync full inventory", f"sync:{account['id']}")],
            [("🔄 Switch account", "switch"), HOME],
        ),
    )


def all_accounts_screen(conn, uid: int):
    accounts = list_accounts(conn, uid)
    if not accounts:
        return welcome_screen()
    active = get_current_account(conn, uid)
    active_id = active["id"] if active else None
    blocks = [account_block(conn, a, a["id"] == active_id) for a in accounts]

    if len(accounts) > 1:
        combined = {}
        for a in accounts:
            for name, qty in get_counts(conn, a["id"]).items():
                combined[name] = combined.get(name, 0) + qty
        total_block = f"Σ <b>All accounts combined</b>\n📦 {plural(sum(combined.values()))}"
        if combined:
            total_block += "\n\n" + "\n".join(inventory_lines(combined))
        blocks.append(total_block)

    return "\n\n━━━━━━━━━━\n\n".join(blocks), keyboard([("🔄 Switch account", "switch"), HOME])


def switch_screen(conn, uid: int):
    accounts = list_accounts(conn, uid)
    if not accounts:
        return welcome_screen()
    active = get_current_account(conn, uid)
    active_id = active["id"] if active else None
    rows = []
    for a in accounts:
        total = sum(get_counts(conn, a["id"]).values())
        mark = "✅" if a["id"] == active_id else "👤"
        rows.append([(f"{mark} {a['name']} · {total}", f"use:{a['id']}")])
    return (
        "🔄 <b>Switch account</b>\n\nWhich account are you playing now?",
        keyboard(*rows, [("➕ New account", "new"), HOME]),
    )


def manage_screen(conn, uid: int):
    accounts = list_accounts(conn, uid)
    if not accounts:
        return welcome_screen()
    rows = [[(f"👤 {a['name']}", f"acct:{a['id']}")] for a in accounts]
    return (
        "⚙️ <b>Manage accounts</b>\n\nTap an account to view, rename or delete it.",
        keyboard(*rows, [("➕ New account", "new"), HOME]),
    )


def account_screen(conn, uid: int, account):
    active = get_current_account(conn, uid)
    is_active = active is not None and active["id"] == account["id"]
    total = sum(get_counts(conn, account["id"]).values())
    description = f"📝 {esc(account['description'])}\n" if account["description"] else ""
    text = (
        f"👤 <b>{esc(account['name'])}</b>{'  ✅ active' if is_active else ''}\n"
        f"{description}📦 {plural(total)}\n\nWhat would you like to do?"
    )
    return text, keyboard(
        [] if is_active else [("✅ Make active", f"use:{account['id']}")],
        [("📦 View items", f"inv:{account['id']}")],
        [("📝 Edit description" if account["description"] else "📝 Add description", f"desc:{account['id']}")],
        [("✏️ Rename", f"ren:{account['id']}"), ("🗑 Delete", f"del:{account['id']}")],
        [("⬅️ Accounts", "manage"), HOME],
    )


def delete_confirm_screen(conn, account):
    total = sum(get_counts(conn, account["id"]).values())
    return (
        f"⚠️ Delete <b>{esc(account['name'])}</b> and all its {plural(total)}?\n\n"
        "This can't be undone.",
        keyboard(
            [("🗑 Yes, delete it", f"delok:{account['id']}")],
            [("✖️ No, keep it", f"acct:{account['id']}")],
        ),
    )


HELP_TOPICS = [
    ("start", "🚀 Getting started", (
        "Make one <b>account</b> for each Ingress agent you play — your agent name works great. "
        "New items go into the <i>active</i> account, shown at the top of the menu. "
        "Tap 🔄 Switch to change it.\n\n"
        "The quick buttons at the bottom of the chat (📦 Inventory, 🔍 Search, 📊 Stats, 🏠 Menu) "
        "work from anywhere."
    )),
    ("add", "📥 Adding & removing", (
        "Tap 📥 Add items → pick a category → pick an item → tap +1, +5, +10…\n"
        "Or type a number, e.g. <code>15</code>. Type <code>=20</code> to set the exact count.\n\n"
        "📤 Remove items works the same way and only shows what you have.\n\n"
        "Made a mistake? Tap <b>↩️ Undo</b> right after the change."
    )),
    ("type", "⌨️ Typing shortcuts", (
        "Just send me a message — no menu needed:\n"
        "<code>R8 10</code>  → add 10 R8\n"
        "<code>-3 VRPS</code>  → remove 3 Very Rare Shields\n"
        "<code>Keys =120</code>  → set Keys to exactly 120\n"
        "<code>HC</code>  → open Hyper Cube's +/− buttons\n\n"
        "Send several lines at once to update many items. Short names like res8, xmp8, "
        "VRPS, HC and SBUL work, in any case. If I don't recognise a name, I'll suggest the closest one."
    )),
    ("sync", "🧾 Sync & import", (
        "<b>🧾 Sync this account</b> (in 🧰 More tools): send your complete list, one item per line. "
        "The account is set to match it exactly — items you leave out become 0.\n\n"
        "<b>📥 Import all accounts</b>: paste every account in one message, each starting with "
        "<code>[Account name]</code>. Missing accounts are created.\n\n"
        "Both show a preview first, and both can be undone."
    )),
    ("search", "🔍 Search", (
        "Tap 🔍 Search and send an item (<code>R8</code>) to see which accounts hold it and the total, "
        "or a wider word (<code>shield</code>, <code>res</code>) for a whole family.\n\n"
        "It also searches account names and descriptions, so a note like "
        "<i>Ambalangoda keys</i> is easy to find. Add descriptions in ⚙️ Manage accounts."
    )),
    ("stats", "📊 Stats & goals", (
        "In Ingress open your agent profile, choose <b>ALL TIME</b>, copy your stats and paste them "
        "here — any time, no button needed. I'll show what grew since last time.\n\n"
        "Tap 📊 Stats & goals → ➕ New goal for daily, weekly, monthly, yearly or lifetime targets. "
        "I'll cheer when you're close (80%), and remind you when time is running out.\n\n"
        "Paste stats at least at the start and end of each day or week so progress is accurate."
    )),
    ("lb", "🏁 Leaderboards", (
        "In 📊 Stats & goals tap 🏁 Leaderboards → ✏️ Edit leaderboards, then ➕ Add leaderboard: pick a stat and paste it, one agent per "
        "line (<code>cristy92 163,477</code>). Text copied from the-grid.blue works too.\n\n"
        "📎 Faster: in ✏️ Edit leaderboards tap 📎 Upload Excel and send the-grid's export (.xlsx) to update every board "
        "in one go.\n\n"
        "I'll show your rank and who's next, and 🎯 can set a goal to pass them. When you pass "
        "them, the goal moves on to the next agent by itself. "
        "When new stats move you past someone, you get a 🚀 note."
    )),
    ("cmds", "⌘ Commands", (
        "/menu – main menu\n"
        "/add 5 U3 – quick add\n"
        "/remove 2 R8 – quick remove\n"
        "/list – active account's items\n"
        "/listall – all accounts\n"
        "/search <i>item</i> – find an item across all accounts\n"
        "/stats – your agent stats (or paste stats after it)\n"
        "/goals – your goals and progress\n"
        "/use <i>name</i> – switch account\n"
        "/newaccount <i>name</i> – create an account\n"
        "/describe <i>text</i> – describe the active account\n"
        "/sync – replace the whole inventory with a pasted list\n"
        "/import – import many accounts at once\n"
        "/items – item names and shortcuts\n"
        "/cancel – stop what you're doing"
    )),
]
HELP_BY_KEY = {key: (title, body) for key, title, body in HELP_TOPICS}


def help_screen(topic: str = None):
    if topic in HELP_BY_KEY:
        title, body = HELP_BY_KEY[topic]
        keys = [k for k, _, _ in HELP_TOPICS]
        nxt = keys[(keys.index(topic) + 1) % len(keys)]
        return f"<b>{title}</b>\n\n{body}", keyboard(
            [(f"Next: {HELP_BY_KEY[nxt][0]}", f"help:{nxt}")],
            [("⬅️ Help topics", "help"), HOME],
        )
    text = (
        "❓ <b>Help</b>\n\n"
        "I keep track of the gear in each of your Ingress accounts, and your agent stats and goals.\n\n"
        "<b>The fastest way:</b> just send me <code>R8 10</code> to add 10 R8 to the active account.\n\n"
        "Pick a topic:"
    )
    buttons = [(title, f"help:{key}") for key, title, _ in HELP_TOPICS]
    return text, keyboard(*chunked(buttons, 2), [("📋 Item names & shortcuts", "catalog")], [HOME])


def catalog_screen():
    lines = ["📋 <b>Item names</b>\n"]
    for cat, cat_items in CATEGORIES:
        lines.append(f"{CATEGORY_EMOJI[cat]} <b>{esc(cat)}</b>: " + ", ".join(esc(i) for i in cat_items))
    lines.append(
        "\n⌨️ <b>Shortcuts you can type</b>\n"
        "res8 · xmp8 · us8 · pc8\n"
        "CPS · RPS · VRPS · Aegis\n"
        "FA · HC (Hyper Cube)\n"
        "CHS · RHS · VRHS (Heat Sinks)\n"
        "CMH · RMH · VRMH (Multi-Hacks)\n"
        "CLA · RLA · SBUL (Link Amps)\n"
        "ITO+ · ITO- · key\n"
        "ADA · JARVIS\n\n"
        "<i>Upper or lower case doesn't matter.</i>"
    )
    return "\n".join(lines), keyboard([("❓ Help", "help"), HOME])


def new_account_prompt():
    return (
        "➕ <b>New account</b>\n\nSend me a name for it (your agent name works great).",
        keyboard([("✖️ Cancel", "menu")]),
    )


def apply_text_changes(conn, account, text: str, default_op: str = "add"):
    """Apply one change per line. Returns a report, or None if nothing was understood."""
    done, bad = [], []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        parsed = parse_line(line, default_op)
        if parsed is None:
            bad.append(line)
            continue
        item, op, amount = parsed
        old, new = apply_change(conn, account["id"], item, op, amount)
        done.append(f"{CATEGORY_EMOJI[ITEM_CATEGORY[item]]} {esc(item)}: {old} → <b>{new}</b>")
    if not done:
        return None
    report = f"✅ <b>{esc(account['name'])}</b> updated:\n\n" + "\n".join(done)
    if bad:
        report += "\n\n⚠️ I couldn't read:\n" + "\n".join(unreadable_line(b) for b in bad)
        report += "\nSee /items for item names."
    return report


def unreadable_line(line: str) -> str:
    """'• <code>line</code>', plus a "did you mean" hint when an item name looks misspelt."""
    text = f"• <code>{esc(line)}</code>"
    guess = suggest_item(line)
    if guess:
        text += f" — did you mean <b>{esc(guess)}</b>?"
    return text


def parse_sync_text(text: str):
    """Parse a full inventory list. Returns ({item: qty}, [unreadable lines])."""
    counts, bad = {}, []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        parsed = parse_line(line, "set")
        if parsed is None:
            bad.append(line)
            continue
        item, _, amount = parsed
        counts[item] = counts.get(item, 0) + amount
    return counts, bad


def sync_prompt_screen(conn, account):
    counts = get_counts(conn, account["id"])
    ordered = sorted(counts.items(), key=lambda kv: ITEM_INDEX.get(kv[0], len(ITEMS)))
    template = "\n".join(f"{name} {qty}" for name, qty in ordered) or "R8 120\nX8 300\nVRPS 12\nKeys 450"
    text = (
        f"🧾 <b>Sync full inventory</b> · {esc(account['name'])}\n\n"
        "Send me <b>everything</b> you have in this account, one item per line. "
        "I'll make the account match your list exactly — <b>items you leave out "
        "become 0</b>. You'll see a preview before anything changes.\n\n"
        + ("Your current list (tap to copy, edit the numbers, send it back):"
           if counts else "Example:")
        + f"\n<pre>{esc(template)}</pre>\n"
        "<i>Shortcuts like res8, xmp8, VRPS, HC, SBUL work too — see /items.</i>"
    )
    return text, keyboard([("✖️ Cancel", f"inv:{account['id']}")])


def sync_preview_screen(conn, account, new_counts: dict, bad):
    old_counts = get_counts(conn, account["id"])
    names = sorted(set(old_counts) | set(new_counts), key=lambda n: (ITEM_INDEX.get(n, len(ITEMS)), n))
    changes, unchanged = [], 0
    for name in names:
        old, new = old_counts.get(name, 0), new_counts.get(name, 0)
        if old == new:
            unchanged += 1
            continue
        emoji = CATEGORY_EMOJI.get(ITEM_CATEGORY.get(name), "❔")
        suffix = "  <i>(not in your list)</i>" if name not in new_counts else ""
        changes.append(f"{emoji} {esc(name)}: {old} → <b>{new}</b>{suffix}")

    old_total, new_total = sum(old_counts.values()), sum(new_counts.values())
    text = f"🧾 <b>Sync preview</b> · {esc(account['name'])}\n\n"
    if changes:
        text += "\n".join(changes)
    else:
        text += "Nothing would change — your list matches the account already."
    if unchanged:
        text += f"\n\n<i>{plural(unchanged)} unchanged.</i>"
    text += f"\n\n📦 Total: {old_total} → <b>{new_total}</b>"
    if bad:
        text += "\n\n⚠️ I couldn't read these lines (they'll be ignored):\n"
        text += "\n".join(unreadable_line(b) for b in bad)
    text += "\n\nApply this? You can also send a corrected list instead."

    rows = [[("✅ Apply sync", f"syncok:{account['id']}")]] if changes else []
    return text, keyboard(*rows, [("✖️ Cancel", f"inv:{account['id']}")])


IMPORT_HEADER_RE = re.compile(r"^\s*\[(.+)\]\s*$")


def looks_like_import(text: str) -> bool:
    return any(IMPORT_HEADER_RE.match(line) for line in text.splitlines())


def parse_import_text(text: str):
    """Parse several accounts, each "[Name]" followed by "- Item: qty" lines.

    Returns ([(name, {item: qty})], [unreadable lines]).
    """
    accounts, bad = [], []
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        header = IMPORT_HEADER_RE.match(line)
        if header:
            accounts.append((" ".join(header.group(1).split()), {}))
            continue
        line = line.lstrip("-•*· ").strip()
        if line.lower() in ("(empty)", "empty"):
            continue
        parsed = parse_line(line, "set") if accounts else None
        if parsed is None:
            bad.append(line)
            continue
        item, _, amount = parsed
        counts = accounts[-1][1]
        counts[item] = counts.get(item, 0) + amount
    return accounts, bad


def find_import_account(conn, owner_id: int, name: str):
    """Match by exact name, else by the agent name before "(" (e.g. "Agent (email)")."""
    account = get_account_by_name(conn, owner_id, name)
    if account:
        return account
    agent = name.split("(")[0].strip().lower()
    matches = [a for a in list_accounts(conn, owner_id) if a["name"].split("(")[0].strip().lower() == agent]
    return matches[0] if len(matches) == 1 else None


def import_prompt_screen():
    example = "[AgentOne]\n  - R8: 120\n  - X8: 300\n\n[AgentTwo]\n  - Hyper Cube: 14\n  - VRPS: 12"
    text = (
        "📥 <b>Import all accounts</b>\n\n"
        "Paste the inventory of all your accounts in one message. Start each account "
        "with its name in square brackets, then one item per line:\n"
        f"<pre>{esc(example)}</pre>\n"
        "Accounts that don't exist yet are created. Each listed account is set to match "
        "your list exactly (items left out become 0). Accounts you don't list are not "
        "touched. You'll see a preview first."
    )
    return text, keyboard([("✖️ Cancel", "menu")])


def import_preview_screen(conn, uid: int, parsed, bad):
    lines = []
    for name, counts in parsed:
        total = sum(counts.values())
        account = find_import_account(conn, uid, name)
        if account is None:
            lines.append(f"🆕 <b>{esc(name)}</b> — new account, {plural(total)}")
            continue
        old = get_counts(conn, account["id"])
        changed = sum(1 for n in set(old) | set(counts) if old.get(n, 0) != counts.get(n, 0))
        label = esc(account["name"])
        if account["name"].lower() != name.lower():
            label += f" <i>(matched “{esc(name)}”)</i>"
        if changed:
            lines.append(
                f"♻️ <b>{label}</b> — {sum(old.values())} → <b>{total}</b> items, {plural(changed, 'change')}"
            )
        else:
            lines.append(f"✔️ <b>{label}</b> — already up to date ({plural(total)})")

    grand = sum(sum(c.values()) for _, c in parsed)
    text = (
        f"📥 <b>Import preview</b> — {plural(len(parsed), 'account')}, {plural(grand)}\n\n"
        + "\n".join(lines)
    )
    if bad:
        text += "\n\n⚠️ I couldn't read these lines (they'll be ignored):\n"
        text += "\n".join(unreadable_line(b) for b in bad[:20])
        if len(bad) > 20:
            text += f"\n…and {len(bad) - 20} more"
    text += "\n\nApply this? You can also send a corrected list instead."
    return text, keyboard([("✅ Apply import", "importok")], [("✖️ Cancel", "menu")])


def apply_import(conn, uid: int, parsed):
    """Create/replace each account. Returns (created ids, updated ids)."""
    created, updated = [], []
    for name, counts in parsed:
        account = find_import_account(conn, uid, name)
        if account is None:
            clean, error = clean_account_name(name)
            if error:
                continue
            cur = conn.execute("INSERT INTO accounts (owner_id, name) VALUES (?, ?)", (uid, clean))
            account_id = cur.lastrowid
            created.append(account_id)
        else:
            account_id = account["id"]
            updated.append(account_id)
        replace_inventory(conn, account_id, counts)
    if get_current_account(conn, uid) is None:
        remaining = list_accounts(conn, uid)
        if remaining:
            set_active(conn, uid, remaining[0]["id"])
    return created, updated


def description_prompt_screen(account):
    current = account["description"]
    text = f"📝 <b>Description</b> · {esc(account['name'])}\n\n"
    if current:
        text += f"Now: {esc(current)}\n\nSend a new description to replace it."
    else:
        text += "Send a short note about this account, e.g. <i>Ambalangoda keys, 120 portal keys</i>."
    return text, keyboard(
        [("🗑 Remove description", f"descdel:{account['id']}")] if current else [],
        [("✖️ Cancel", f"acct:{account['id']}")],
    )


def save_description(conn, account, raw: str):
    """Returns (description, error)."""
    description = raw.strip()
    if not description:
        return None, "The description can't be empty."
    if len(description) > MAX_DESCRIPTION:
        return None, f"Please keep the description under {MAX_DESCRIPTION} characters."
    conn.execute("UPDATE accounts SET description = ? WHERE id = ?", (description, account["id"]))
    return description, None


def search_prompt_screen():
    return (
        "🔍 <b>Search all accounts</b>\n\n"
        "What are you looking for? For example:\n"
        "<code>R8</code>  → one item\n"
        "<code>shield</code> or <code>res</code>  → a whole family\n"
        "<code>Ambalangoda</code>  → account names and descriptions",
        keyboard([("✖️ Cancel", "menu")]),
    )


def _holders_text(rows) -> str:
    return "\n".join(f"└ {esc(r['account'])}: <b>{r['quantity']}</b>" for r in rows)


def search_results_screen(conn, uid: int, query: str):
    items = search_items(query)
    rows = []
    if items:
        marks = ",".join("?" * len(items))
        rows = conn.execute(
            f"""
            SELECT i.name, i.quantity, a.id AS account_id, a.name AS account
            FROM items i JOIN accounts a ON a.id = i.account_id
            WHERE a.owner_id = ? AND i.quantity > 0 AND i.name IN ({marks})
            ORDER BY i.quantity DESC, a.name COLLATE NOCASE
            """,
            (uid, *items),
        ).fetchall()

    needle = query.casefold()
    noted = [
        a for a in list_accounts(conn, uid)
        if needle in a["name"].casefold() or needle in a["description"].casefold()
    ]

    blocks = []
    if len(items) == 1:
        item = items[0]
        emoji = CATEGORY_EMOJI[ITEM_CATEGORY[item]]
        if rows:
            total = sum(r["quantity"] for r in rows)
            blocks.append(
                f"{emoji} <b>{esc(item)}</b>\n"
                f"Total: <b>{total}</b> in {plural(len(rows), 'account')}\n\n{_holders_text(rows)}"
            )
        else:
            blocks.append(f"{emoji} <b>{esc(item)}</b>\nNone in any of your accounts.")
    elif rows:
        total = sum(r["quantity"] for r in rows)
        per_account = {}
        for r in rows:
            per_account[r["account"]] = per_account.get(r["account"], 0) + r["quantity"]
        by_account = sorted(per_account.items(), key=lambda kv: (-kv[1], kv[0].lower()))
        blocks.append(
            f"📊 Total: <b>{total}</b> in {plural(len(by_account), 'account')}\n"
            + "\n".join(f"└ {esc(name)}: <b>{qty}</b>" for name, qty in by_account)
        )
        for item in items:
            held = [r for r in rows if r["name"] == item]
            if held:
                emoji = CATEGORY_EMOJI[ITEM_CATEGORY[item]]
                subtotal = sum(r["quantity"] for r in held)
                blocks.append(f"{emoji} <b>{esc(item)}</b>: <b>{subtotal}</b>\n{_holders_text(held)}")
    elif items:
        blocks.append(f"None of the matching items are in your accounts ({plural(len(items))} checked).")

    if noted:
        lines = []
        for a in noted:
            lines.append(f"👤 <b>{esc(a['name'])}</b>")
            if a["description"]:
                lines.append(f"└ 📝 {esc(a['description'])}")
        blocks.append("🗂 <b>Matching accounts</b>\n" + "\n".join(lines))

    if not blocks:
        blocks.append("Nothing found. Try an item like <code>R8</code>, or see /items for all the names.")

    header = f"🔍 <b>Search:</b> {esc(query)}"
    account_rows = [[(f"👤 {a['name']}", f"acct:{a['id']}")] for a in noted[:10]]
    return (
        header + "\n\n" + "\n\n".join(blocks),
        keyboard(*account_rows, [("🔍 Search again", "search"), HOME]),
    )


# ---------------------------------------------------------------------------
# Agent stats & goals screens
# ---------------------------------------------------------------------------

STATS_PER_PAGE = 16
FACTION_EMOJI = {"Resistance": "🔵", "Enlightened": "🟢"}


def fmt_num(n: int) -> str:
    return f"{n:,}"


def fmt_stat(stat: str, n: int) -> str:
    unit = stats.STAT_UNITS.get(stat)
    if unit and n == 1 and unit.endswith("s"):
        unit = unit[:-1]
    return f"{n:,} {unit}" if unit else f"{n:,}"


def fmt_duration(delta) -> str:
    secs = int(delta.total_seconds())
    if secs < 60:
        return "under a minute"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days >= 2:
        return f"{days} days"
    if days == 1:
        return f"1 day {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def fmt_ago(when: datetime, now: datetime) -> str:
    if (now - when).total_seconds() < 60:
        return "just now"
    return f"{fmt_duration(now - when)} ago"


def progress_bar(pct: int, width: int = 10) -> str:
    filled = max(0, min(width, pct * width // 100))
    return "▰" * filled + "▱" * (width - filled)


def pace_text(p, now: datetime) -> str:
    """' (≈ 120/day)' when more than a day is left on an unfinished goal."""
    if p["complete"] or p["end"] is None:
        return ""
    days_left = (p["end"] - now).total_seconds() / 86400
    if days_left <= 1:
        return ""
    return f" (≈ {fmt_num(math.ceil(p['remaining'] / days_left))}/day)"


def goals_by_progress(conn, agent_id: int, now: datetime):
    """[(goal, progress), ...] with the highest percentage first.

    Ties (e.g. freshly set goals at 0%) go closest-to-target first.
    """
    pairs = [(g, stats.goal_progress(conn, g, now)) for g in stats.list_goals(conn, agent_id)]
    return sorted(pairs, key=lambda gp: (-gp[1]["pct"], -gp[1]["done"] / max(1, gp[1]["target"])))


def goal_label(goal) -> str:
    return f"{stats.PERIOD_LABEL[goal['period']].lower()} {esc(goal['stat'])} goal"


def fmt_short(n: int) -> str:
    """1234 -> 1.2k, 2500000 -> 2.5M (for tight spots like buttons)."""
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if abs(n) >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def goal_line(goal, p) -> str:
    """One short line for summaries: '🏔 Hacks — need 1,200 (45%)'."""
    head = f"{stats.PERIOD_EMOJI[goal['period']]} {esc(goal['stat'])}"
    if p["complete"]:
        return f"{head} — 🏆 done"
    return f"{head} — need <b>{fmt_stat(goal['stat'], p['remaining'])}</b> ({p['pct']}%)"


def goal_button(goal, p) -> str:
    head = f"{stats.PERIOD_EMOJI[goal['period']]} {goal['stat']}"
    return f"{head} · 🏆" if p["complete"] else f"{head} · {fmt_short(p['remaining'])} to go"


def stats_prompt_screen(note: str = ""):
    return (
        f"{note}📊 <b>Agent stats</b>\n\n"
        "1️⃣ In Ingress, open your agent profile.\n"
        "2️⃣ Choose <b>ALL TIME</b>.\n"
        "3️⃣ Copy your stats.\n"
        "4️⃣ Paste them here as one message.\n\n"
        "<i>The paste starts with</i> <code>Time Span  Agent Name  …</code>\n"
        "<i>You can paste stats any time — you don't need to open this screen first.</i>",
        keyboard([("✖️ Cancel", "menu")]),
    )


# Friendly short names for the stats overview.
SUMMARY_LABELS = [
    ("Lifetime AP", "⭐", "AP"),
    ("Unique Portals Visited", "🗺", "Portals visited"),
    ("Unique Portals Captured", "🚩", "Portals captured"),
    ("Hacks", "🔓", "Hacks"),
    ("Links Created", "🔗", "Links"),
    ("Control Fields Created", "🔺", "Fields"),
    ("Mind Units Captured", "🧠", "Mind units"),
    ("Distance Walked", "🚶", "Walked"),
]
STATS_SCREEN_GOALS = 5


def ap_section(conn, agent, now: datetime):
    """The ⚡ AP block on the stats page."""
    report = stats.ap_report(conn, agent, now)
    rows, partial = [], None
    for period, emoji, label in (("daily", "☀️", "Today"), ("weekly", "📅", "This week"),
                                 ("monthly", "🗓", "This month")):
        g = report[period]
        if g is None or not g["measured"]:
            rows.append(f"{emoji} {label}: <i>—</i>")
            continue
        start, _, _ = stats.period_window(period, now)
        mark = ""
        if g["base_at"] > start:
            mark, partial = "*", min(partial or g["base_at"], g["base_at"])
        rows.append(f"{emoji} {label}: <b>{fmt_num(g['gain'])}</b>{mark}")
    avg = report["avg"]
    if avg:
        rows += [
            f"📊 Avg / day: <b>{fmt_num(avg['day'])}</b>",
            f"📊 Avg / month: <b>{fmt_num(avg['month'])}</b>",
            f"📊 Avg / year: <b>{fmt_num(avg['year'])}</b>",
        ]
    notes = []
    if partial:
        notes.append(f"* counted from your first paste, {partial:%Y-%m-%d %H:%M}")
    if any(report[p] is None or not report[p]["measured"] for p in ("daily", "weekly", "monthly")):
        notes.append("— paste stats at least twice in a period to measure it")
    if avg and avg["basis"] == "career":
        notes.append(f"averages over your whole career, since {avg['since']:%Y-%m-%d}")
    elif avg:
        notes.append(f"averages since your first paste ({avg['since']:%Y-%m-%d}) — "
                     "tap 📅 Start date for career averages")
    else:
        notes.append("averages need a day of pasted stats, or tap 📅 Start date")
    return ("⚡ <b>AP</b>\n<blockquote>" + "\n".join(rows) + "</blockquote>\n"
            + "\n".join(f"<i>{esc(n)}</i>" for n in notes))


def start_date_prompt_screen(agent):
    current = agent["started_on"]
    lines = [f"📅 <b>Agent start date</b> · {esc(agent['name'])}", ""]
    if current:
        lines += [f"Now set to <b>{current}</b>.", ""]
    lines += [
        "When did you start playing Ingress? Send the date, like <code>2017-03-01</code> "
        "(or just <code>2017-03</code>).",
        "",
        "<i>I use it for your AP averages per day, month and year.</i>",
    ]
    return "\n".join(lines), keyboard([("✖️ Cancel", f"stats:{agent['id']}")])


def stats_screen(conn, uid: int, agent=None, note: str = ""):
    agents = stats.list_agents(conn, uid)
    agent = agent or agents[0]
    now = datetime.now()
    snap = stats.latest_snapshot(conn, agent["id"])
    values = snap["values"]
    faction = agent["faction"]
    who = f"{FACTION_EMOJI.get(faction, '⚪')} {esc(faction)}"
    if "Level" in values:
        who += f" · Level {values['Level']}"
    age = now - snap["taken_at"]
    fresh = "🕒" if age.total_seconds() < 86400 else "⚠️"
    lines = [note] if note else []
    lines += [
        f"📊 <b>{esc(agent['name'])}</b>",
        who,
        f"{fresh} Updated {fmt_ago(snap['taken_at'], now)}",
    ]
    if age.total_seconds() >= 86400:
        lines.append("<i>Paste fresh stats to update your goals.</i>")
    lines.append("")
    lines += [
        f"{emoji} {label}: <b>{fmt_stat(stat, values[stat])}</b>"
        for stat, emoji, label in SUMMARY_LABELS if stat in values
    ]
    lines += ["", ap_section(conn, agent, now)]

    goals, completed = split_goals(goals_by_progress(conn, agent["id"], now))
    lines.append("")
    if goals:
        rows = [f"{stats.PERIOD_EMOJI[g['period']]} {esc(g['stat'])}: "
                f"<b>{fmt_stat(g['stat'], p['remaining'])}</b> to go · {p['pct']}%"
                for g, p in goals[:STATS_SCREEN_GOALS]]
        notes = ["closest to done first"]
        if len(goals) > STATS_SCREEN_GOALS:
            notes.append(f"…and {len(goals) - STATS_SCREEN_GOALS} more — tap 🎯 Goals")
        if completed:
            notes.append(f"🏆 {plural(len(completed), 'goal')} completed — tap 🏆 Completed")
        lines.append(f"🎯 <b>GOALS</b> · {len(goals)} active\n<blockquote>" + "\n".join(rows)
                     + "</blockquote>\n" + "\n".join(f"<i>{n}</i>" for n in notes))
    else:
        lines.append("🎯 <i>No active goals — tap ➕ New goal.</i>")
        if completed:
            lines.append(f"<i>🏆 {plural(len(completed), 'goal')} completed — tap 🏆 Completed</i>")

    others = [[(f"👤 {a['name']}", f"stats:{a['id']}")] for a in agents if a["id"] != agent["id"]]
    goals_label = f"🎯 Goals ({len(goals)})" if goals else "🎯 Goals"
    return "\n".join(lines), keyboard(
        [("📋 Paste new stats", "statsin")],
        [(goals_label, f"goals:{agent['id']}"), ("➕ New goal", f"gnew:{agent['id']}")],
        [(f"🏆 Completed ({len(completed)})", f"gdone:{agent['id']}")] if completed else [],
        [("🏁 Leaderboards", f"lbs:{agent['id']}"), ("📈 All stats", f"statsall:{agent['id']}")],
        [("📅 Start date", f"astart:{agent['id']}")],
        *others[:5],
        [HOME],
    )


def all_stats_screen(conn, agent):
    snap = stats.latest_snapshot(conn, agent["id"])
    values = snap["values"]
    order = stats.STAT_FIELDS + sorted(s for s in values if s not in stats.STAT_INDEX)
    lines = [f"📈 <b>All stats</b> · {esc(agent['name'])}", f"🕒 {snap['taken_at']:%Y-%m-%d %H:%M}", ""]
    lines += [f"{esc(s)}: <b>{fmt_stat(s, values[s])}</b>" for s in order if s in values]
    return "\n".join(lines), keyboard([("⬅️ Stats", f"stats:{agent['id']}"), HOME])


def stats_saved_screen(conn, uid: int, text: str):
    try:
        parsed = stats.parse_stats(text)
    except stats.StatsError as e:
        return f"⚠️ {e}", keyboard([("📋 Try again", "statsin"), HOME])
    now = datetime.now()
    agent, previous, is_new = stats.save_snapshot(conn, uid, parsed, now)
    aid = agent["id"]
    values = parsed["values"]

    lines = [f"✅ <b>STATS SAVED</b> · {esc(agent['name'])}"]
    card = []
    if "Level" in values:
        card.append(f"🎖 L{values['Level']}")
    if "Lifetime AP" in values:
        card.append(f"⚡ {fmt_num(values['Lifetime AP'])} AP")
    if card:
        lines.append("  ·  ".join(card))
    gained = {}
    if not is_new:
        lines.append("ℹ️ I already had stats from that exact time, so nothing changed.")
    elif previous and previous["taken_at"] > parsed["taken_at"]:
        lines.append("ℹ️ These are older than your latest stats, so I kept them as history.")
    elif previous:
        gains = [
            (s, values[s] - previous["values"][s])
            for s in stats.PICK_ORDER
            if s in values and s in previous["values"] and values[s] > previous["values"][s]
        ]
        gained = dict(gains)
        if gains:
            lines += ["", f"📈 <b>SINCE LAST STATS</b> · <i>{fmt_ago(previous['taken_at'], now)}</i>\n"
                          + fold_quote([f"➕ {esc(s)}  <b>+{fmt_stat(s, d)}</b>" for s, d in gains], 6)]
        else:
            lines += ["", "😴 <i>No changes since your last stats.</i>"]

    chase_row = []
    moved = {}
    if is_new and (previous is None or previous["taken_at"] < parsed["taken_at"]):
        moved = refresh_rival_goals(conn, agent)
    if is_new and previous and previous["taken_at"] < parsed["taken_at"]:
        rival_lines = []
        for board in stats.list_leaderboards(conn, aid):
            stat = board["stat"]
            if stat not in values or stat not in previous["values"]:
                continue
            mine = values[stat]
            passed = stats.passed_rivals(board, agent["name"], previous["values"][stat], mine)
            if not passed:
                continue
            names = ", ".join(esc(name) for name, _ in passed)
            rank = stats.my_rank(board, agent["name"], mine)
            rival_lines.append(f"🚀 You passed {names} in <b>{esc(stat)}</b> — now #{rank}!")
            nxt = stats.next_rival(board, agent["name"], mine)
            if stat in moved:
                rival_lines.append(moved.pop(stat))
            elif nxt:
                rival_lines.append(f"      ╰ Next: <i>{esc(nxt[0])}</i>, <b>{fmt_num(nxt[1] - mine + 1)}</b> to go")
                if not chase_row and stat in stats.STAT_INDEX:
                    chase_row = [(f"🎯 Chase {nxt[0]}", f"lbgoal:{aid}:{stats.STAT_INDEX[stat]}")]
    else:
        rival_lines = []
    highlights = rival_lines + list(moved.values())

    goals = goals_by_progress(conn, aid, now)
    if goals:
        cheers = []
        for g, p in goals:
            if p["complete"]:
                if stats.mark_alert(conn, g["id"], p["key"], "done", now):
                    cheers.append(
                        f"🏆 You hit your {goal_label(g)}! {fmt_num(p['done'])} / {fmt_num(p['target'])}"
                    )
            elif p["pct"] >= stats.NEAR_PCT and stats.mark_alert(conn, g["id"], p["key"], "near", now):
                cheers.append(
                    f"🔥 Almost there: {p['pct']}% of your {goal_label(g)} — "
                    f"just {fmt_num(p['remaining'])} to go!"
                )
        highlights += cheers
        if highlights:
            lines += ["", "🎉 <b>HIGHLIGHTS</b>\n<blockquote>" + "\n".join(highlights) + "</blockquote>"]
        active, completed = split_goals(goals)
        moved_goals = [gp for gp in active if gp[0]["stat"] in gained]
        other_goals = [gp for gp in active if gp[0]["stat"] not in gained]
        if moved_goals:
            lines += ["", "📈 <b>GOALS THAT MOVED</b>\n"
                          + fold_quote([goal_entry(g, p, now) for g, p in moved_goals], 5)]
        if other_goals:
            title = "🎯 <b>OTHER GOALS</b>" if moved_goals else "🎯 <b>GOALS</b>"
            lines += ["", f"{title}\n<i>closest first</i>\n"
                          + fold_quote([goal_entry(g, p, now) for g, p in other_goals], 3)]
        if completed:
            lines += ["", f"🏆 <i>{plural(len(completed), 'goal')} completed — see 🎯 Goals → 🏆 Completed.</i>"]
    else:
        if highlights:
            lines += ["", "🎉 <b>HIGHLIGHTS</b>\n<blockquote>" + "\n".join(highlights) + "</blockquote>"]
        lines += ["", "💡 Tap ➕ New goal to set daily, weekly, monthly, yearly or lifetime targets."]

    return "\n".join(lines), keyboard(
        chase_row,
        [("🎯 Goals", f"goals:{aid}"), ("➕ New goal", f"gnew:{aid}")],
        [("📊 Stats", f"stats:{aid}"), HOME],
    )


def split_goals(goals):
    """(active, completed) from goals_by_progress()."""
    return [gp for gp in goals if not gp[1]["complete"]], [gp for gp in goals if gp[1]["complete"]]


HALFWAY_PCT = 50   # lifetime goals at or past this are "almost there"


def goal_entry(goal, p, now: datetime) -> str:
    """Two lines for the goals list: title, then a progress bar with what's left."""
    period = goal["period"]
    title = f"{stats.PERIOD_EMOJI[period]} <b>{esc(goal['stat'])}</b>"
    if goal["note"]:
        title += f" · <i>{esc(goal['note'])}</i>"
    if period != "lifetime":
        title += f" · <i>{stats.PERIOD_LABEL[period].lower()}</i>"
    if p["complete"]:
        return (f"{title}\n      ✅ <b>Done</b> · target {fmt_stat(goal['stat'], p['target'])}"
                f" · now {fmt_stat(goal['stat'], p['done'])}")
    status = (f"<code>{progress_bar(p['pct'], 8)}</code> {p['pct']}% · need "
              f"<b>{fmt_stat(goal['stat'], p['remaining'])}</b>")
    if p["end"]:
        status += f" · ⏳ {fmt_duration(p['end'] - now)}"
    return f"{title}\n      {status}"


def goals_screen(conn, uid: int, agent):
    now = datetime.now()
    aid = agent["id"]
    active, completed = split_goals(goals_by_progress(conn, aid, now))
    lines = [f"🎯 <b>GOALS</b> · {esc(agent['name'])}"]
    if active:
        timed = sorted((gp for gp in active if gp[0]["period"] != "lifetime"),
                       key=lambda gp: gp[1]["end"])
        lifetime = [gp for gp in active if gp[0]["period"] == "lifetime"]
        almost = [gp for gp in lifetime if gp[1]["pct"] >= HALFWAY_PCT]
        longer = [gp for gp in lifetime if gp[1]["pct"] < HALFWAY_PCT]

        summary = []
        if timed:
            summary.append(f"⏰ {len(timed)} timed")
        if almost:
            summary.append(f"🔥 {len(almost)} close")
        if longer:
            summary.append(f"🏔 {len(longer)} long-term")
        if completed:
            summary.append(f"🏆 {len(completed)} done")
        lines.append("  ·  ".join(summary))

        def section(head, hint, items, shown):
            text = f"{head}\n<i>{hint}</i>\n" if hint else f"{head}\n"
            return text + fold_quote([goal_entry(g, p, now) for g, p in items], shown)

        if timed:
            lines += ["", section("⏰ <b>ON THE CLOCK</b>", "daily, weekly, monthly and yearly — ending soonest first", timed, 5)]
        if almost:
            lines += ["", section("🔥 <b>ALMOST THERE</b>", f"{HALFWAY_PCT}% or more of the way", almost, 5)]
        if longer:
            lines += ["", section("🏔 <b>LONG-TERM</b>", "closest first", longer, 3)]

        latest = stats.latest_snapshot(conn, aid)
        lines += ["", f"🕒 <i>Based on stats from {fmt_ago(latest['taken_at'], now)} · paste fresh "
                      "stats to update.</i>\n"
                      "✏️ <i>To change or delete a goal, tap Edit goals.</i>"]
    elif completed:
        lines += ["", "🎉 All your goals are done! Tap ➕ New goal to set another."]
    else:
        lines += ["", "No goals yet. Tap ➕ New goal to set one."]
    done_row = [(f"🏆 Completed ({len(completed)})", f"gdone:{aid}")] if completed else []
    edit_row = [("✏️ Edit goals", f"gedit:{aid}")] if active or completed else []
    return "\n".join(lines), keyboard(
        done_row,
        edit_row + [("➕ New goal", f"gnew:{aid}")],
        [("⬅️ Stats", f"stats:{aid}"), HOME],
    )


def completed_goals_screen(conn, uid: int, agent):
    now = datetime.now()
    _, completed = split_goals(goals_by_progress(conn, agent["id"], now))
    lines = [f"🏆 <b>COMPLETED GOALS</b> · {esc(agent['name'])}"]
    if completed:
        lines.append(f"🎉 {plural(len(completed), 'goal')} reached — nice work!")
        lines += ["", fold_quote([goal_entry(g, p, now) for g, p in completed], 8)]
        lines += ["", "✏️ <i>To set a new target or delete one, tap Edit goals.</i>"]
    else:
        lines += ["", "No completed goals yet — keep going! 💪"]
    edit_row = [("✏️ Edit goals", f"gedit:{agent['id']}")] if completed else []
    return "\n".join(lines), keyboard(edit_row, [("⬅️ Goals", f"goals:{agent['id']}"), HOME])


def edit_goals_screen(conn, uid: int, agent):
    aid = agent["id"]
    now = datetime.now()
    active, completed = split_goals(goals_by_progress(conn, aid, now))
    lines = [f"✏️ <b>Edit goals</b> · {esc(agent['name'])}", ""]
    if active or completed:
        lines.append("Tap a goal to change its target or delete it.")
    else:
        lines.append("No goals yet. Tap ➕ New goal to set one.")
    rows = [[(goal_button(g, p), f"goal:{g['id']}")] for g, p in active]
    rows += [[(f"🏆 {g['stat']} · {stats.PERIOD_LABEL[g['period']]}", f"goal:{g['id']}")]
             for g, _ in completed]
    return "\n".join(lines), keyboard(
        *rows,
        [("➕ New goal", f"gnew:{aid}")],
        [("⬅️ Goals", f"goals:{aid}"), HOME],
    )


def goal_screen(conn, uid: int, goal, note: str = ""):
    now = datetime.now()
    agent = stats.get_agent(conn, uid, goal["agent_id"])
    period = goal["period"]
    p = stats.goal_progress(conn, goal, now)
    lines = [note] if note else []
    lines += [
        f"{stats.PERIOD_EMOJI[period]} <b>{stats.PERIOD_LABEL[period]} goal</b> · {esc(goal['stat'])}",
        f"👤 {esc(agent['name'])}",
    ]
    if goal["note"]:
        lines.append(f"🏁 {esc(goal['note'])}")
    lines += ["", f"{progress_bar(p['pct'])} {p['pct']}%"]
    if p["complete"]:
        lines.append(f"🏆 <b>Done!</b> {fmt_stat(goal['stat'], p['done'])} / {fmt_stat(goal['stat'], p['target'])}")
    else:
        lines.append(f"🎯 Need <b>{fmt_stat(goal['stat'], p['remaining'])}</b> more{pace_text(p, now)}")
        if period == "lifetime":
            lines.append(f"📍 Now: {fmt_stat(goal['stat'], p['done'])}")
            lines.append(f"🏁 Target: {fmt_stat(goal['stat'], p['target'])}")
            if goal["start_value"] is not None:
                lines.append(f"<i>% counts from {fmt_num(goal['start_value'])}, where you were when you set it.</i>")
        else:
            lines.append(f"📍 Done so far: {fmt_num(p['done'])} / {fmt_num(p['target'])}")
    if period != "lifetime":
        noun = stats.PERIOD_NOUN[period]
        lines.append(f"⏳ Ends in {fmt_duration(p['end'] - now)} ({p['end']:%Y-%m-%d %H:%M})")
        if p["base_at"]:
            lines.append(f"📏 Counting from your stats at {p['base_at']:%Y-%m-%d %H:%M}")
        else:
            lines.append(f"📏 No stats pasted this {noun} yet — paste some to start counting.")
        prev_start, prev_end, _ = stats.previous_window(period, now)
        last = stats.gain_between(conn, goal["agent_id"], goal["stat"], prev_start, prev_end)
        if last and last["measured"]:
            lines.append(f"🔙 Last {noun}: {fmt_num(last['gain'])}")

    change, chase = [], []
    if goal["stat"] in stats.STAT_INDEX:
        stat_idx = stats.STAT_INDEX[goal["stat"]]
        change = [("✏️ Change target", f"gsel:{agent['id']}:{period}:{stat_idx}")]
        board = stats.get_leaderboard(conn, agent["id"], goal["stat"])
        if p["complete"] and period == "lifetime" and board:
            nxt = stats.next_rival(board, agent["name"], p["done"])
            if nxt:
                chase = [(f"🎯 Chase next: {nxt[0]}", f"lbgoal:{agent['id']}:{stat_idx}")]
    back = ("⬅️ Edit goals", f"gedit:{agent['id']}")
    return "\n".join(lines), keyboard(
        chase,
        change,
        [("🗑 Delete goal", f"gdel:{goal['id']}")],
        [back, HOME],
    )


def new_goal_screen(agent):
    aid = agent["id"]
    text = (
        f"🎯 <b>New goal</b> · {esc(agent['name'])}\n\n"
        "What kind of goal?\n\n"
        "☀️ <b>Daily</b> — gain an amount every day\n"
        "📅 <b>Weekly</b> — every week (Monday to Sunday)\n"
        "🗓 <b>Monthly</b> — every month\n"
        "🎆 <b>Yearly</b> — every year\n"
        "🏔 <b>Lifetime</b> — reach a total, like 200M AP"
    )
    buttons = [(f"{stats.PERIOD_EMOJI[p]} {stats.PERIOD_LABEL[p]}", f"gper:{aid}:{p}") for p in stats.PERIODS]
    return text, keyboard(*chunked(buttons, 2), [("✖️ Cancel", f"stats:{aid}")])


def _stat_picker(conn, agent, title: str, pick_data, page_data, page: int):
    """A paged list of stat buttons; pick_data(index) / page_data(page) build callback data."""
    aid = agent["id"]
    snap = stats.latest_snapshot(conn, aid)
    choices = [s for s in stats.stat_choices(snap) if s in stats.STAT_INDEX]
    pages = max(1, math.ceil(len(choices) / STATS_PER_PAGE))
    page = max(0, min(page, pages - 1))
    shown = choices[page * STATS_PER_PAGE:(page + 1) * STATS_PER_PAGE]
    buttons = [(s, pick_data(stats.STAT_INDEX[s])) for s in shown]
    nav = []
    if page > 0:
        nav.append(("◀️ Back", page_data(page - 1)))
    if page < pages - 1:
        nav.append(("More ▶️", page_data(page + 1)))
    text = (
        f"{title} · {esc(agent['name'])}\n\n"
        "Which stat? Tap one, or type its name (e.g. <code>hacks</code>, <code>UPV</code>, "
        f"<code>glyph</code>).\n<i>Page {page + 1} of {pages}</i>"
    )
    return text, keyboard(*chunked(buttons, 2), nav, [("✖️ Cancel", f"stats:{aid}")])


def stat_pick_screen(conn, agent, period: str, page: int):
    aid = agent["id"]
    return _stat_picker(
        conn, agent,
        f"{stats.PERIOD_EMOJI[period]} <b>{stats.PERIOD_LABEL[period]} goal</b>",
        lambda i: f"gsel:{aid}:{period}:{i}",
        lambda p: f"gper:{aid}:{period}:{p}",
        page,
    )


def leaderboard_stat_screen(conn, agent, page: int):
    aid = agent["id"]
    return _stat_picker(
        conn, agent, "🏁 <b>New leaderboard</b>",
        lambda i: f"lbstat:{aid}:{i}",
        lambda p: f"lbnew:{aid}:{p}",
        page,
    )


def leaderboard_prompt_screen(conn, agent, stat: str):
    aid = agent["id"]
    lines = [f"🏁 <b>Leaderboard</b> · {esc(stat)}", ""]
    if stats.get_leaderboard(conn, aid, stat):
        lines += ["Send the new list to replace the one I have.", ""]
    lines += [
        "Paste the leaderboard, one agent per line with their score:",
        "<code>mistybeast 391,642</code>",
        "<code>JuGGmA 378,091</code>",
        "<code>cristy92 163,477</code>",
        "",
        "Ranks (<code>#7</code>) and commas are fine, and so is text copied straight from "
        "the leaderboard page. You can leave yourself out — I use your latest stats.",
    ]
    return "\n".join(lines), keyboard([("✖️ Cancel", f"lbs:{aid}")])


def _my_value(conn, agent, stat: str) -> int:
    snap = stats.latest_snapshot(conn, agent["id"])
    return snap["values"].get(stat, 0)


ALMOST_PCT = 5        # within 5% of the next agent's score counts as "almost there"
OLD_LIST_DAYS = 7     # flag lists older than this


def fold_quote(entries, shown: int) -> str:
    """The first `shown` entries in a quote block, the rest folded away until tapped."""
    if len(entries) - shown <= 2:  # not worth folding one or two away
        shown = len(entries)
    out = []
    if entries[:shown]:
        out.append("<blockquote>" + "\n".join(entries[:shown]) + "</blockquote>")
    rest = entries[shown:]
    if rest:
        out.append("<blockquote expandable>"
                   + f"👇 <i>{len(rest)} more — tap to open</i>\n" + "\n".join(rest)
                   + "</blockquote>")
    return "\n".join(out)


def leaderboards_screen(conn, agent, note: str = ""):
    aid = agent["id"]
    now = datetime.now()
    boards = stats.list_leaderboards(conn, aid)
    lines = [note] if note else []
    lines.append(f"🏁 <b>LEADERBOARDS</b> · {esc(agent['name'])}")
    if not boards:
        lines += ["", (
            "Paste a leaderboard (for example Glyph Hack Points from the-grid.blue) to see your "
            "rank, who's next and how far ahead they are. Tap ➕ Add leaderboard.\n\n"
            "📎 Or tap Upload Excel to add them all at once from the-grid's export."
        )]
        return "\n".join(lines), keyboard(
            [("➕ Add leaderboard", f"lbnew:{aid}"), ("📎 Upload Excel", f"lbfile:{aid}")],
            [("⬅️ Stats", f"stats:{aid}"), HOME],
        )

    chasing, top = [], []
    for board in boards:
        stat = board["stat"]
        mine = _my_value(conn, agent, stat)
        info = {
            "stat": stat,
            "rank": stats.my_rank(board, agent["name"], mine),
            "total": len(stats.rivals(board, agent["name"])) + 1,
            "age": now - board["updated_at"],
        }
        nxt = stats.next_rival(board, agent["name"], mine)
        if nxt is None:
            top.append(info)
            continue
        info["rival"], info["gap"] = nxt[0], nxt[1] - mine + 1
        info["gap_pct"] = info["gap"] * 100 / max(1, nxt[1])  # how far behind, relative to them
        chasing.append(info)
    chasing.sort(key=lambda b: b["gap_pct"])
    top.sort(key=lambda b: b["stat"])
    almost = [b for b in chasing if b["gap_pct"] <= ALMOST_PCT]
    later = [b for b in chasing if b["gap_pct"] > ALMOST_PCT]

    def badge(rank: int) -> str:
        return {1: "🥇", 2: "🥈", 3: "🥉"}.get(rank, "🏅" if rank <= 10 else "🔹")

    def entry(b, chase: bool) -> str:
        # Exports stop at you, so "/total" only means something when people are listed below you.
        place = f"#{b['rank']}/{b['total']}" if b["rank"] < b["total"] else f"#{b['rank']}"
        head = f"{badge(b['rank'])} <b>{esc(b['stat'])}</b>  <code>{place}</code>"
        if b["age"].days >= OLD_LIST_DAYS:
            head += f" ⚠️ {b['age'].days}d old"
        if not chase:
            return head
        return f"{head}\n      ╰ <b>{fmt_stat(b['stat'], b['gap'])}</b> to pass <i>{esc(b['rival'])}</i>"

    quote = fold_quote

    summary = []
    if almost:
        summary.append(f"🔥 {len(almost)} close")
    if later:
        summary.append(f"🎯 {len(later)} chasing")
    if top:
        summary.append(f"🥇 {len(top)} on top")
    lines += ["  ·  ".join(summary)]

    if almost:
        lines += ["", f"🔥 <b>ALMOST THERE</b>\n<i>within {ALMOST_PCT}% of the next agent</i>\n"
                      + quote([entry(b, True) for b in almost], 5)]
    if later:
        lines += ["", "🎯 <b>KEEP CHASING</b>\n<i>closest first</i>\n"
                      + quote([entry(b, True) for b in later], 3)]
    if top:
        lines += ["", "🥇 <b>YOU'RE ON TOP</b>\n" + quote([entry(b, False) for b in top], 5)]

    oldest = max(b["age"] for b in chasing + top)
    lines += ["", f"🕒 <i>Oldest list: {fmt_ago(now - oldest, now)} · your scores come from your "
                  "latest stats.</i>\n"
                  "✏️ <i>Full lists, Excel upload, add or delete: tap Edit leaderboards.</i>"]
    return "\n".join(lines), keyboard(
        [("✏️ Edit leaderboards", f"lbedit:{aid}")],
        [("⬅️ Stats", f"stats:{aid}"), HOME],
    )


def edit_leaderboards_screen(conn, agent, note: str = ""):
    aid = agent["id"]
    boards = [b for b in stats.list_leaderboards(conn, aid) if b["stat"] in stats.STAT_INDEX]
    lines = [note] if note else []
    lines += [f"✏️ <b>Edit leaderboards</b> · {esc(agent['name'])}", ""]
    lines.append("📎 <b>Upload Excel</b> updates every board at once from the-grid's export.")
    lines.append("➕ <b>Add leaderboard</b> pastes one list by hand.")
    if boards:
        lines += ["", "Tap a board to see everyone, paste a newer list or delete it."]
    rows = [[(f"🏁 {b['stat']}", f"lb:{aid}:{stats.STAT_INDEX[b['stat']]}")] for b in boards]
    return "\n".join(lines), keyboard(
        [("📎 Upload Excel", f"lbfile:{aid}"), ("➕ Add leaderboard", f"lbnew:{aid}")],
        *rows,
        [("⬅️ Leaderboards", f"lbs:{aid}"), HOME],
    )


def leaderboard_screen(conn, agent, stat: str, note: str = ""):
    aid = agent["id"]
    idx = stats.STAT_INDEX[stat]
    now = datetime.now()
    board = stats.get_leaderboard(conn, aid, stat)
    mine = _my_value(conn, agent, stat)
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [note] if note else []
    lines += [f"🏁 <b>{esc(stat)}</b>", ""]
    for rank, name, value, is_me in stats.ranking(board, agent["name"], mine):
        place = medals.get(rank, f"#{rank}")
        if is_me:
            lines.append(f"<b>{place} ➤ {esc(name)} — {fmt_num(value)}</b>")
        else:
            lines.append(f"{place} {esc(name)} — {fmt_num(value)}")
    nxt = stats.next_rival(board, agent["name"], mine)
    goal_row = []
    lines.append("")
    if nxt:
        lines.append(f"🎯 Next: pass <b>{esc(nxt[0])}</b> — {fmt_num(nxt[1] - mine + 1)} to go")
        goal_row = [(f"🎯 Set goal to pass {nxt[0]}", f"lbgoal:{aid}:{idx}")]
    else:
        lines.append("🥇 You're at the top of this list!")
    lines += [
        "",
        f"<i>List from {fmt_ago(board['updated_at'], now)}; your score is from your latest stats.</i>",
    ]
    return "\n".join(lines), keyboard(
        goal_row,
        [("📋 Update list", f"lbstat:{aid}:{idx}"), ("🗑 Delete", f"lbdel:{aid}:{idx}")],
        [("⬅️ Edit leaderboards", f"lbedit:{aid}"), HOME],
    )


XLSX_MAX_BYTES = 5 * 1024 * 1024


def lb_import_pick_screen(conn, uid, agents):
    lines = ["📎 <b>Leaderboard file</b>", "", "Which agent is this file for?"]
    return "\n".join(lines), keyboard(
        *[[(f"🕵️ {a['name']}", f"lbxp:{a['id']}")] for a in agents], [HOME],
    )


def lb_import_preview_screen(conn, agent, pending):
    aid = agent["id"]
    boards = pending["boards"]
    tracked = {b["stat"] for b in stats.list_leaderboards(conn, aid)}
    update = [s for s in boards if s in tracked]
    new = [s for s in boards if s not in tracked]
    lines = [f"📎 <b>Leaderboard file</b> · {esc(agent['name'])}", ""]
    lines.append(f"Found {plural(len(boards), 'leaderboard')}:")
    if update:
        lines.append(f"🔄 <b>{len(update)}</b> you already track — I'll update them")
    if new:
        lines.append(f"➕ <b>{len(new)}</b> new: " + ", ".join(esc(s) for s in sorted(new)[:6])
                     + (f" and {len(new) - 6} more" if len(new) > 6 else ""))
    if pending["skipped"]:
        n = len(pending["skipped"])
        lines += ["", f"<i>Skipped {n} event categor{'y' if n == 1 else 'ies'} "
                      "that aren't in your Ingress stats.</i>"]
    rows = []
    if update:
        rows.append([(f"🔄 Update {plural(len(update), 'board')}", f"lbxl:{aid}:upd")])
        if new:
            rows.append([(f"➕ Update + add {len(new)} new", f"lbxl:{aid}:all")])
    elif new:
        rows.append([(f"➕ Add {plural(len(new), 'board')}", f"lbxl:{aid}:all")])
    rows.append([("✖️ Cancel", f"lbedit:{aid}")])
    return "\n".join(lines), keyboard(*rows)


def lb_file_prompt_screen(agent):
    aid = agent["id"]
    return (
        f"📎 <b>Upload leaderboards</b> · {esc(agent['name'])}\n\n"
        "1️⃣ On the-grid.blue, download your rankings as Excel (.xlsx).\n"
        "2️⃣ Here, tap 📎 (attach) → <b>File</b> and pick that file.\n"
        "3️⃣ I'll show what changes before saving anything.\n\n"
        "<i>Every leaderboard in the file is read at once.</i>"
    ), keyboard([("✖️ Cancel", f"lbedit:{aid}")])


def btn_lb_file(conn, uid, ud, args):
    agent = _agent(conn, uid, args[0])
    ud["awaiting"] = {"kind": "lb_file", "agent_id": agent["id"]}
    return (*lb_file_prompt_screen(agent), None)


def _pending_import(ud):
    pending = ud.get("lb_import")
    if not pending:
        raise StaleButton
    return pending


def btn_lb_import_pick(conn, uid, ud, args):
    agent = _agent(conn, uid, args[0])
    return (*lb_import_preview_screen(conn, agent, _pending_import(ud)), None)


def btn_lb_import_save(conn, uid, ud, args):
    agent = _agent(conn, uid, args[0])
    pending = _pending_import(ud)
    tracked = {b["stat"] for b in stats.list_leaderboards(conn, agent["id"])}
    now = datetime.now()
    saved = 0
    for stat, entries in pending["boards"].items():
        if args[1] == "all" or stat in tracked:
            stats.save_leaderboard(conn, agent["id"], stat, entries, now)
            saved += 1
    ud.pop("lb_import", None)
    note = f"✅ Updated {plural(saved, 'leaderboard')} from the file.\n"
    moved = refresh_rival_goals(conn, agent)
    if moved:
        note += "\n" + "\n".join(moved.values()) + "\n"
    return (*leaderboards_screen(conn, agent, note), "Leaderboards updated")


def is_rival_goal(goal) -> bool:
    return goal["period"] == "lifetime" and (goal["note"] or "").startswith("pass ")


def refresh_rival_goals(conn, agent, only=None):
    """Keep "pass X" goals pointed at the next agent up. Returns {stat: note line}.

    A reached goal moves on to the next agent; an open one follows the latest
    list. A goal stays done when you're #1.
    """
    now = datetime.now()
    notes = {}
    for goal in stats.list_goals(conn, agent["id"]):
        stat = goal["stat"]
        if not is_rival_goal(goal) or (only is not None and stat not in only):
            continue
        board = stats.get_leaderboard(conn, agent["id"], stat)
        if board is None:
            continue
        mine = _my_value(conn, agent, stat)
        nxt = stats.next_rival(board, agent["name"], mine)
        if nxt is None:
            continue
        target, note = nxt[1] + 1, f"pass {nxt[0]}"
        reached = mine >= goal["target"]
        if not reached and (target, note) == (goal["target"], goal["note"]):
            continue
        start = mine if reached else goal["start_value"]
        stats.set_goal(conn, agent["id"], stat, "lifetime", target, now, note=note, start_value=start)
        to_go = fmt_stat(stat, target - mine)
        if reached:
            notes[stat] = (f"🏆 Goal done: {esc(goal['note'])} in <b>{esc(stat)}</b>!\n"
                           f"      ╰ 🎯 New goal: pass <b>{esc(nxt[0])}</b>, {to_go} to go")
        elif note != goal["note"]:
            notes[stat] = f"🎯 <b>{esc(stat)}</b> goal now: pass <b>{esc(nxt[0])}</b>, {to_go} to go"
    return notes


def set_rival_goal(conn, agent, stat: str):
    """Make a lifetime goal to pass the next agent up. Returns the goal id, or None if top."""
    board = stats.get_leaderboard(conn, agent["id"], stat)
    mine = _my_value(conn, agent, stat)
    nxt = stats.next_rival(board, agent["name"], mine) if board else None
    if nxt is None:
        return None
    return stats.set_goal(
        conn, agent["id"], stat, "lifetime", nxt[1] + 1, datetime.now(),
        note=f"pass {nxt[0]}", start_value=mine,
    )


def target_prompt_screen(conn, agent, period: str, stat: str):
    aid = agent["id"]
    now = datetime.now()
    snap = stats.latest_snapshot(conn, aid)
    lines = [f"{stats.PERIOD_EMOJI[period]} <b>{stats.PERIOD_LABEL[period]} goal</b> · {esc(stat)}", ""]
    existing = next((g for g in stats.list_goals(conn, aid) if g["stat"] == stat and g["period"] == period), None)
    if existing:
        lines += [f"Current target: <b>{fmt_num(existing['target'])}</b> — send a new one to change it.", ""]
    if period == "lifetime":
        current = snap["values"].get(stat, 0)
        lines.append(f"You have <b>{fmt_stat(stat, current)}</b> now. What total do you want to reach?")
    else:
        noun = stats.PERIOD_NOUN[period]
        lines.append(f"How much <b>{esc(stat)}</b> do you want to gain each {noun}?")
        prev_start, prev_end, _ = stats.previous_window(period, now)
        last = stats.gain_between(conn, aid, stat, prev_start, prev_end)
        if last and last["measured"]:
            lines.append(f"Last {noun} you gained <b>{fmt_num(last['gain'])}</b>.")
        start, end, _ = stats.period_window(period, now)
        so_far = stats.gain_between(conn, aid, stat, start, end)
        if so_far:
            lines.append(f"So far this {noun}: <b>{fmt_num(so_far['gain'])}</b>.")
    lines += ["", "Send a number, like <code>500</code>, <code>2.5k</code> or <code>1M</code>."]
    return "\n".join(lines), keyboard(
        [("⬅️ Pick another stat", f"gper:{aid}:{period}:0")],
        [("✖️ Cancel", f"stats:{aid}")],
    )


def save_goal_target(conn, agent, period: str, stat: str, raw: str):
    """Returns (goal_id, error)."""
    target = stats.parse_amount(raw)
    if target is None:
        return None, "Please send a whole number like <code>500</code>, <code>2.5k</code> or <code>1M</code>."
    start_value = None
    if period == "lifetime":
        start_value = stats.latest_snapshot(conn, agent["id"])["values"].get(stat, 0)
        if target <= start_value:
            return None, f"You already have {fmt_num(start_value)}. Send a bigger total."
    goal_id = stats.set_goal(conn, agent["id"], stat, period, target, datetime.now(), start_value=start_value)
    return goal_id, None


def ending_reminder_text(goal, p, now: datetime) -> str:
    noun = stats.PERIOD_NOUN[goal["period"]]
    lines = [
        f"⏰ <b>Your {stats.PERIOD_LABEL[goal['period']].lower()} goal ends in {fmt_duration(p['end'] - now)}</b>",
        f"👤 {esc(goal['agent_name'])} · {esc(goal['stat'])}",
        f"{progress_bar(p['pct'])} {p['pct']}% · {fmt_num(p['done'])} / {fmt_num(p['target'])}",
        f"🎯 {fmt_num(p['remaining'])} to go{pace_text(p, now)}",
        "",
    ]
    if p["latest_at"] and p["latest_at"] >= p["start"]:
        lines.append(f"<i>Based on stats from {fmt_ago(p['latest_at'], now)}. Paste fresh stats to update.</i>")
    else:
        lines.append(f"<i>No stats pasted this {noun} yet — paste them so I can count your progress.</i>")
    return "\n".join(lines)


def after_change_keyboard(account):
    return keyboard([UNDO], [("📦 View inventory", f"inv:{account['id']}"), HOME])


DIDNT_UNDERSTAND = (
    "🤔 Sorry, I didn't catch that.\n\n"
    "To change items, send something like:\n"
    "<code>R8 10</code>  or  <code>-3 VRPS</code>  or  <code>Keys =120</code>\n\n"
    "To save stats, paste your ALL TIME stats from Ingress.\n"
    "Or open the menu and use the buttons."
)


# ---------------------------------------------------------------------------
# Button router
# ---------------------------------------------------------------------------

class StaleButton(Exception):
    """Raised when a button refers to something that no longer exists."""


def _mode(value: str) -> str:
    if value not in ("a", "r"):
        raise StaleButton
    return value


def _owned(conn, uid: int, account_id: str):
    account = get_owned_account(conn, uid, int(account_id))
    if account is None:
        raise StaleButton
    return account


def _item(index: str) -> str:
    i = int(index)
    if not 0 <= i < len(ITEMS):
        raise StaleButton
    return ITEMS[i]


def _account_or_active(conn, uid: int, args, i: int = 0):
    if len(args) > i and args[i]:
        return _owned(conn, uid, args[i])
    return get_current_account(conn, uid)


def btn_menu(conn, uid, ud, args):
    return (*menu_screen(conn, uid), None)


def btn_categories(conn, uid, ud, args):
    mode = _mode(args[0])
    account = _account_or_active(conn, uid, args, 1)
    if account is None:
        return (*no_active_screen(conn, uid), None)
    return (*categories_screen(conn, account, mode), None)


def btn_category(conn, uid, ud, args):
    mode, account = _mode(args[0]), _owned(conn, uid, args[1])
    cat_idx = int(args[2])
    if not 0 <= cat_idx < len(CATEGORIES):
        raise StaleButton
    return (*items_screen(conn, account, mode, cat_idx), None)


def btn_item(conn, uid, ud, args):
    mode, account = _mode(args[0]), _owned(conn, uid, args[1])
    item = _item(args[2])
    ud["awaiting"] = {"kind": "qty", "account_id": account["id"], "item": item, "mode": mode}
    if ud.get("undo"):
        ud["undo"]["merge_key"] = None
    return (*quantity_screen(conn, account, mode, item), None)


def btn_quantity(conn, uid, ud, args):
    mode, account = _mode(args[0]), _owned(conn, uid, args[1])
    item = _item(args[2])
    if args[3] == "all":
        op, amount = "set", 0
    else:
        delta = int(args[3])
        op, amount = ("add", delta) if delta > 0 else ("remove", -delta)
    remember_undo(ud, snapshot(conn, [account["id"]]), item, merge_key=f"{account['id']}:{item}")
    old, new = apply_change(conn, account["id"], item, op, amount)
    ud["awaiting"] = {"kind": "qty", "account_id": account["id"], "item": item, "mode": mode}
    note = change_note(item, op, amount, old, new)
    return (*quantity_screen(conn, account, mode, item, note), f"{item}: {old} → {new}")


def btn_inventory(conn, uid, ud, args):
    account = _account_or_active(conn, uid, args)
    if account is None:
        return (*no_active_screen(conn, uid), None)
    return (*inventory_screen(conn, uid, account), None)


def btn_all(conn, uid, ud, args):
    return (*all_accounts_screen(conn, uid), None)


def btn_switch(conn, uid, ud, args):
    return (*switch_screen(conn, uid), None)


def btn_use(conn, uid, ud, args):
    account = _owned(conn, uid, args[0])
    set_active(conn, uid, account["id"])
    return (*menu_screen(conn, uid), f"Now using {account['name']}")


def btn_new(conn, uid, ud, args):
    ud["awaiting"] = {"kind": "new_account"}
    return (*new_account_prompt(), None)


def btn_manage(conn, uid, ud, args):
    return (*manage_screen(conn, uid), None)


def btn_account(conn, uid, ud, args):
    return (*account_screen(conn, uid, _owned(conn, uid, args[0])), None)


def btn_rename(conn, uid, ud, args):
    account = _owned(conn, uid, args[0])
    ud["awaiting"] = {"kind": "rename", "account_id": account["id"]}
    return (
        f"✏️ Send me the new name for <b>{esc(account['name'])}</b>.",
        keyboard([("✖️ Cancel", f"acct:{account['id']}")]),
        None,
    )


def btn_delete(conn, uid, ud, args):
    return (*delete_confirm_screen(conn, _owned(conn, uid, args[0])), None)


def btn_delete_ok(conn, uid, ud, args):
    account = _owned(conn, uid, args[0])
    delete_account(conn, uid, account)
    return (*manage_screen(conn, uid), f"Deleted {account['name']}")


def btn_sync(conn, uid, ud, args):
    account = _account_or_active(conn, uid, args)
    if account is None:
        return (*no_active_screen(conn, uid), None)
    ud.pop("pending_sync", None)
    ud["awaiting"] = {"kind": "sync", "account_id": account["id"]}
    return (*sync_prompt_screen(conn, account), None)


def btn_sync_ok(conn, uid, ud, args):
    account = _owned(conn, uid, args[0])
    pending = ud.pop("pending_sync", None)
    if not pending or pending["account_id"] != account["id"]:
        raise StaleButton
    remember_undo(ud, snapshot(conn, [account["id"]]), "sync")
    replace_inventory(conn, account["id"], pending["counts"])
    return (*inventory_screen(conn, uid, account, "✅ <b>Inventory synced.</b>", undo=True), "Inventory synced ✅")


def btn_import(conn, uid, ud, args):
    ud.pop("pending_import", None)
    ud["awaiting"] = {"kind": "import"}
    return (*import_prompt_screen(), None)


def btn_import_ok(conn, uid, ud, args):
    pending = ud.pop("pending_import", None)
    if not pending:
        raise StaleButton
    existing = {a["id"] for name, _ in pending if (a := find_import_account(conn, uid, name))}
    before = snapshot(conn, existing)
    created, updated = apply_import(conn, uid, pending)
    remember_undo(ud, before, "import", created=created)
    toast = f"Imported: {len(created)} new, {len(updated)} updated"
    text, markup = all_accounts_screen(conn, uid)
    return text, with_row(markup, [("↩️ Undo import", "undo")]), toast


def btn_description(conn, uid, ud, args):
    account = _owned(conn, uid, args[0])
    ud["awaiting"] = {"kind": "description", "account_id": account["id"]}
    return (*description_prompt_screen(account), None)


def btn_description_delete(conn, uid, ud, args):
    account = _owned(conn, uid, args[0])
    conn.execute("UPDATE accounts SET description = '' WHERE id = ?", (account["id"],))
    return (*account_screen(conn, uid, _owned(conn, uid, args[0])), "Description removed")


def btn_search(conn, uid, ud, args):
    ud["awaiting"] = {"kind": "search"}
    return (*search_prompt_screen(), None)


def _agent(conn, uid: int, agent_id: str):
    agent = stats.get_agent(conn, uid, int(agent_id))
    if agent is None:
        raise StaleButton
    return agent


def _period(value: str) -> str:
    if value not in stats.PERIODS:
        raise StaleButton
    return value


def _goal(conn, uid: int, goal_id: str):
    goal = stats.get_goal(conn, uid, int(goal_id))
    if goal is None:
        raise StaleButton
    return goal


def btn_stats(conn, uid, ud, args):
    agent = _agent(conn, uid, args[0]) if args and args[0] else None
    if agent is None and not stats.list_agents(conn, uid):
        ud["awaiting"] = {"kind": "stats"}
        return (*stats_prompt_screen(), None)
    return (*stats_screen(conn, uid, agent), None)


def btn_agent_start(conn, uid, ud, args):
    agent = _agent(conn, uid, args[0])
    ud["awaiting"] = {"kind": "agent_start", "agent_id": agent["id"]}
    return (*start_date_prompt_screen(agent), None)


def btn_stats_in(conn, uid, ud, args):
    ud["awaiting"] = {"kind": "stats"}
    return (*stats_prompt_screen(), None)


def btn_stats_all(conn, uid, ud, args):
    return (*all_stats_screen(conn, _agent(conn, uid, args[0])), None)


def btn_goals(conn, uid, ud, args):
    return (*goals_screen(conn, uid, _agent(conn, uid, args[0])), None)


def btn_goals_done(conn, uid, ud, args):
    return (*completed_goals_screen(conn, uid, _agent(conn, uid, args[0])), None)


def btn_goals_edit(conn, uid, ud, args):
    return (*edit_goals_screen(conn, uid, _agent(conn, uid, args[0])), None)


def btn_goal(conn, uid, ud, args):
    return (*goal_screen(conn, uid, _goal(conn, uid, args[0])), None)


def btn_goal_new(conn, uid, ud, args):
    return (*new_goal_screen(_agent(conn, uid, args[0])), None)


def btn_goal_period(conn, uid, ud, args):
    agent, period = _agent(conn, uid, args[0]), _period(args[1])
    page = int(args[2]) if len(args) > 2 else 0
    ud["awaiting"] = {"kind": "goal_stat", "agent_id": agent["id"], "period": period}
    return (*stat_pick_screen(conn, agent, period, page), None)


def btn_goal_stat(conn, uid, ud, args):
    agent, period = _agent(conn, uid, args[0]), _period(args[1])
    i = int(args[2])
    if not 0 <= i < len(stats.STAT_FIELDS):
        raise StaleButton
    stat = stats.STAT_FIELDS[i]
    ud["awaiting"] = {"kind": "goal_target", "agent_id": agent["id"], "period": period, "stat": stat}
    return (*target_prompt_screen(conn, agent, period, stat), None)


def _stat_arg(index: str) -> str:
    i = int(index)
    if not 0 <= i < len(stats.STAT_FIELDS):
        raise StaleButton
    return stats.STAT_FIELDS[i]


def _board_args(conn, uid, args):
    agent, stat = _agent(conn, uid, args[0]), _stat_arg(args[1])
    if stats.get_leaderboard(conn, agent["id"], stat) is None:
        raise StaleButton
    return agent, stat


def btn_leaderboards(conn, uid, ud, args):
    return (*leaderboards_screen(conn, _agent(conn, uid, args[0])), None)


def btn_leaderboards_edit(conn, uid, ud, args):
    return (*edit_leaderboards_screen(conn, _agent(conn, uid, args[0])), None)


def btn_leaderboard(conn, uid, ud, args):
    return (*leaderboard_screen(conn, *_board_args(conn, uid, args)), None)


def btn_leaderboard_new(conn, uid, ud, args):
    agent = _agent(conn, uid, args[0])
    page = int(args[1]) if len(args) > 1 else 0
    ud["awaiting"] = {"kind": "lb_stat", "agent_id": agent["id"]}
    return (*leaderboard_stat_screen(conn, agent, page), None)


def btn_leaderboard_stat(conn, uid, ud, args):
    agent, stat = _agent(conn, uid, args[0]), _stat_arg(args[1])
    ud["awaiting"] = {"kind": "leaderboard", "agent_id": agent["id"], "stat": stat}
    return (*leaderboard_prompt_screen(conn, agent, stat), None)


def btn_leaderboard_goal(conn, uid, ud, args):
    agent, stat = _board_args(conn, uid, args)
    goal_id = set_rival_goal(conn, agent, stat)
    if goal_id is None:
        return (*leaderboard_screen(conn, agent, stat), "You're already at the top of this list")
    goal = stats.get_goal(conn, uid, goal_id)
    return (*goal_screen(conn, uid, goal, "✅ <b>Goal saved!</b>\n"), None)


def btn_leaderboard_delete(conn, uid, ud, args):
    agent, stat = _board_args(conn, uid, args)
    stats.delete_leaderboard(conn, agent["id"], stat)
    return (*edit_leaderboards_screen(conn, agent), "Leaderboard deleted")


def btn_goal_delete(conn, uid, ud, args):
    goal = _goal(conn, uid, args[0])
    stats.delete_goal(conn, goal["id"])
    return (*edit_goals_screen(conn, uid, _agent(conn, uid, goal["agent_id"])), "Goal deleted")


def btn_help(conn, uid, ud, args):
    return (*help_screen(args[0] if args else None), None)


def btn_more(conn, uid, ud, args):
    return (*more_screen(conn, uid), None)


def btn_undo(conn, uid, ud, args):
    result = undo_last(conn, uid, ud)
    if result is None:
        return (*menu_screen(conn, uid), "Nothing to undo")
    label, restored = result
    what = {"sync": "the sync", "import": "the import"}.get(label, f"the {label} change")
    note = f"↩️ <b>Undone</b> — {esc(what)} was taken back."
    if len(restored) == 1:
        account = get_owned_account(conn, uid, restored[0])
        return (*inventory_screen(conn, uid, account, note), "Undone ↩️")
    text, markup = all_accounts_screen(conn, uid)
    return f"{note}\n\n{text}", markup, "Undone ↩️"


def btn_catalog(conn, uid, ud, args):
    return (*catalog_screen(), None)


BUTTONS = {
    "menu": btn_menu,
    "cats": btn_categories,
    "cat": btn_category,
    "it": btn_item,
    "q": btn_quantity,
    "inv": btn_inventory,
    "all": btn_all,
    "switch": btn_switch,
    "use": btn_use,
    "new": btn_new,
    "manage": btn_manage,
    "acct": btn_account,
    "ren": btn_rename,
    "del": btn_delete,
    "delok": btn_delete_ok,
    "sync": btn_sync,
    "syncok": btn_sync_ok,
    "import": btn_import,
    "importok": btn_import_ok,
    "desc": btn_description,
    "descdel": btn_description_delete,
    "search": btn_search,
    "stats": btn_stats,
    "astart": btn_agent_start,
    "statsin": btn_stats_in,
    "statsall": btn_stats_all,
    "goals": btn_goals,
    "goal": btn_goal,
    "gedit": btn_goals_edit,
    "gdone": btn_goals_done,
    "gnew": btn_goal_new,
    "gper": btn_goal_period,
    "gsel": btn_goal_stat,
    "gdel": btn_goal_delete,
    "lbs": btn_leaderboards,
    "lb": btn_leaderboard,
    "lbedit": btn_leaderboards_edit,
    "lbnew": btn_leaderboard_new,
    "lbstat": btn_leaderboard_stat,
    "lbgoal": btn_leaderboard_goal,
    "lbdel": btn_leaderboard_delete,
    "lbxp": btn_lb_import_pick,
    "lbfile": btn_lb_file,
    "lbxl": btn_lb_import_save,
    "help": btn_help,
    "catalog": btn_catalog,
    "more": btn_more,
    "undo": btn_undo,
}


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Every button carries all the info it needs, so old buttons keep working."""
    query = update.callback_query
    uid = update.effective_user.id
    context.user_data.pop("awaiting", None)
    action, *args = (query.data or "").split(":")
    handler = BUTTONS.get(action)

    try:
        if handler is None:
            raise StaleButton
        with closing(get_conn()) as conn, conn:
            ensure_user(conn, uid)
            text, markup, toast = handler(conn, uid, context.user_data, args)
    except (StaleButton, ValueError, IndexError):
        with closing(get_conn()) as conn, conn:
            text, markup = menu_screen(conn, uid)
        toast = "That button is out of date — here's the menu."

    try:
        await query.answer(toast)
    except BadRequest:
        # Taps made while the bot was offline arrive too late to acknowledge;
        # still update the message so the tap isn't lost.
        logger.info("Callback query expired; updating the message anyway")
    await respond(update, text, markup)


# ---------------------------------------------------------------------------
# Typed messages
# ---------------------------------------------------------------------------

async def ensure_quick_keyboard(update: Update, ud, force: bool = False):
    """Show the bottom quick buttons once (or again on /start)."""
    if not force and ud.get("quick_kb") == QUICK_KB_VERSION:
        return
    ud["quick_kb"] = QUICK_KB_VERSION
    await update.effective_message.reply_text(
        "⌨️ Quick buttons are at the bottom of the chat — tap them any time.",
        reply_markup=quick_keyboard(),
    )


def _guess_agent(agents, filename: str, boards):
    """The agent this export is for: named in the file name, else the only one listed."""
    fname = (filename or "").lower()
    named = [a for a in agents if a["name"].lower() in fname]
    if len(named) == 1:
        return named[0]
    listed = {name.lower() for entries in boards.values() for name, _ in entries}
    in_file = [a for a in agents if a["name"].lower() in listed]
    if len(in_file) == 1:
        return in_file[0]
    return agents[0] if len(agents) == 1 else None


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """An uploaded the-grid Excel export updates the leaderboards."""
    uid = update.effective_user.id
    doc = update.message.document
    await ensure_quick_keyboard(update, context.user_data)
    name = doc.file_name or ""
    if not name.lower().endswith((".xlsx", ".xlsm")):
        await respond(update, (
            "📎 I can read leaderboard exports from the-grid.blue as Excel files (<b>.xlsx</b>). "
            "For anything else, paste the text instead."
        ), keyboard([HOME]))
        return
    if doc.file_size and doc.file_size > XLSX_MAX_BYTES:
        await respond(update, "📎 That file is too big (over 5 MB) for a leaderboard export.",
                      keyboard([HOME]))
        return

    data = bytes(await (await doc.get_file()).download_as_bytearray())
    try:
        boards, skipped = stats.parse_grid_workbook(data)
    except stats.StatsError as e:
        await respond(update, f"🤔 {esc(str(e))} Send the Excel export from the-grid.blue.",
                      keyboard([HOME]))
        return
    if not boards:
        await respond(update, "🤔 I didn't find any leaderboards for Ingress stats in that file.",
                      keyboard([HOME]))
        return

    awaiting = context.user_data.pop("awaiting", None) or {}
    context.user_data["lb_import"] = {"boards": boards, "skipped": skipped}
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        agents = stats.list_agents(conn, uid)
        if not agents:
            context.user_data.pop("lb_import", None)
            await respond(update, (
                "📎 Paste your Ingress <b>ALL TIME</b> stats first so I know who you are, "
                "then send the file again."
            ), keyboard([HOME]))
            return
        chosen = [a for a in agents if awaiting.get("kind") == "lb_file" and a["id"] == awaiting.get("agent_id")]
        agent = chosen[0] if chosen else _guess_agent(agents, name, boards)
        if agent is None:
            await respond(update, *lb_import_pick_screen(conn, uid, agents))
        else:
            await respond(update, *lb_import_preview_screen(conn, agent, context.user_data["lb_import"]))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    awaiting = context.user_data.get("awaiting") or {}
    kind = awaiting.get("kind")
    await ensure_quick_keyboard(update, context.user_data)

    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)

        # The bottom quick buttons work from anywhere and stop any typing flow.
        if text in QUICK_KEYS:
            context.user_data.pop("awaiting", None)
            reply, markup, _ = BUTTONS[QUICK_KEYS[text]](conn, uid, context.user_data, [])
            await respond(update, reply, markup)
            return

        # Pasted stats are recognised whatever the bot was waiting for.
        if stats.looks_like_stats(text):
            context.user_data.pop("awaiting", None)
            await respond(update, *stats_saved_screen(conn, uid, text))
            return

        if kind == "stats":
            await respond(
                update,
                "🤔 That doesn't look like Ingress stats. Copy your <b>ALL TIME</b> stats in Ingress "
                "and paste the whole thing — it starts with <code>Time Span</code>. Or tap Cancel.",
                keyboard([("✖️ Cancel", "menu")]),
            )
            return

        if kind in ("lb_stat", "leaderboard"):
            agent = stats.get_agent(conn, uid, awaiting["agent_id"])
            if agent is None:
                context.user_data.pop("awaiting", None)
            elif kind == "lb_stat":
                snap = stats.latest_snapshot(conn, agent["id"])
                choices = [s for s in stats.stat_choices(snap) if s in stats.STAT_INDEX]
                stat = stats.lookup_stat(text, choices)
                if stat is None:
                    await respond(
                        update,
                        f"🤔 I don't know a stat called <b>{esc(text)}</b>. Tap one of the buttons, "
                        "or try another name.",
                        keyboard([("📋 Show the stats", f"lbnew:{agent['id']}")],
                                 [("✖️ Cancel", f"lbs:{agent['id']}")]),
                    )
                    return
                context.user_data["awaiting"] = {"kind": "leaderboard", "agent_id": agent["id"], "stat": stat}
                await respond(update, *leaderboard_prompt_screen(conn, agent, stat))
                return
            else:
                entries = stats.parse_leaderboard(text)
                if not stats.rivals({"entries": entries}, agent["name"]):
                    await respond(
                        update,
                        "🤔 I couldn't find any agents in that. Send one per line, like "
                        "<code>cristy92 163,477</code>, or tap Cancel.",
                        keyboard([("✖️ Cancel", f"lbs:{agent['id']}")]),
                    )
                    return
                context.user_data.pop("awaiting", None)
                stats.save_leaderboard(conn, agent["id"], awaiting["stat"], entries, datetime.now())
                note = f"✅ Saved {plural(len(entries), 'agent')}.\n"
                moved = refresh_rival_goals(conn, agent, only={awaiting["stat"]})
                if moved:
                    note += "\n".join(moved.values()) + "\n"
                await respond(update, *leaderboard_screen(conn, agent, awaiting["stat"], note))
                return

        if kind == "agent_start":
            agent = stats.get_agent(conn, uid, awaiting["agent_id"])
            if agent is None:
                context.user_data.pop("awaiting", None)
            else:
                day = stats.parse_start_date(text)
                if day is None or not (stats.INGRESS_LAUNCH <= day <= datetime.now()):
                    await respond(
                        update,
                        "🤔 I need a date between 2012-11-15 (Ingress launch) and today, like "
                        "<code>2017-03-01</code>. Or tap Cancel.",
                        keyboard([("✖️ Cancel", f"stats:{agent['id']}")]),
                    )
                    return
                context.user_data.pop("awaiting", None)
                stats.set_start_date(conn, agent["id"], day)
                agent = stats.get_agent(conn, uid, agent["id"])
                note = f"✅ Start date set to <b>{day:%Y-%m-%d}</b>.\n"
                await respond(update, *stats_screen(conn, uid, agent, note))
                return

        if kind == "lb_file":
            await respond(
                update,
                "📎 I'm waiting for the Excel file. Tap 📎 (attach) → <b>File</b> and pick the "
                ".xlsx, or tap Cancel.",
                keyboard([("✖️ Cancel", f"lbs:{awaiting['agent_id']}")]),
            )
            return

        if kind in ("goal_stat", "goal_target"):
            agent = stats.get_agent(conn, uid, awaiting["agent_id"])
            period = awaiting["period"]
            if agent is None:
                context.user_data.pop("awaiting", None)
            elif kind == "goal_stat":
                snap = stats.latest_snapshot(conn, agent["id"])
                stat = stats.lookup_stat(text, stats.stat_choices(snap))
                if stat is None:
                    await respond(
                        update,
                        f"🤔 I don't know a stat called <b>{esc(text)}</b>. Tap one of the buttons, "
                        "or try another name.",
                        keyboard([("📋 Show the stats", f"gper:{agent['id']}:{period}:0")],
                                 [("✖️ Cancel", f"stats:{agent['id']}")]),
                    )
                    return
                context.user_data["awaiting"] = {**awaiting, "kind": "goal_target", "stat": stat}
                await respond(update, *target_prompt_screen(conn, agent, period, stat))
                return
            else:
                goal_id, error = save_goal_target(conn, agent, period, awaiting["stat"], text)
                if error:
                    await respond(
                        update,
                        f"⚠️ {error}\n\nSend it again, or tap Cancel.",
                        keyboard([("✖️ Cancel", f"stats:{agent['id']}")]),
                    )
                    return
                context.user_data.pop("awaiting", None)
                goal = stats.get_goal(conn, uid, goal_id)
                await respond(update, *goal_screen(conn, uid, goal, "✅ <b>Goal saved!</b>\n"))
                return

        if kind == "new_account":
            account, error = create_account(conn, uid, text)
            if error:
                reply = (f"⚠️ {error}\n\nSend another name, or tap Cancel.", keyboard([("✖️ Cancel", "menu")]))
            else:
                context.user_data.pop("awaiting", None)
                reply = (
                    f"🎉 Account <b>{esc(account['name'])}</b> created — it's now your active account.",
                    keyboard([("📥 Add items now", f"cats:a:{account['id']}")], [HOME]),
                )
            await respond(update, *reply)
            return

        if kind == "rename":
            account = get_owned_account(conn, uid, awaiting["account_id"])
            if account is None:
                context.user_data.pop("awaiting", None)
            else:
                new_name, error = rename_account(conn, uid, account, text)
                if error:
                    reply = (
                        f"⚠️ {error}\n\nSend another name, or tap Cancel.",
                        keyboard([("✖️ Cancel", f"acct:{account['id']}")]),
                    )
                else:
                    context.user_data.pop("awaiting", None)
                    reply = (
                        f"✏️ Renamed to <b>{esc(new_name)}</b>.",
                        keyboard([("⬅️ Accounts", "manage"), HOME]),
                    )
                await respond(update, *reply)
                return

        if kind == "description":
            account = get_owned_account(conn, uid, awaiting["account_id"])
            if account is None:
                context.user_data.pop("awaiting", None)
            else:
                _, error = save_description(conn, account, text)
                if error:
                    await respond(
                        update,
                        f"⚠️ {error}\n\nSend it again, or tap Cancel.",
                        keyboard([("✖️ Cancel", f"acct:{account['id']}")]),
                    )
                    return
                context.user_data.pop("awaiting", None)
                await respond(update, *account_screen(conn, uid, get_owned_account(conn, uid, account["id"])))
                return

        if kind == "search":
            context.user_data.pop("awaiting", None)
            await respond(update, *search_results_screen(conn, uid, " ".join(text.split())))
            return

        if kind == "import" or looks_like_import(text):
            parsed, bad = parse_import_text(text)
            if not parsed:
                await respond(
                    update,
                    "🤔 I couldn't find any accounts in that. Start each account with its "
                    "name in square brackets, like <code>[AgentOne]</code>, or tap Cancel.",
                    keyboard([("✖️ Cancel", "menu")]),
                )
                return
            # Keep "awaiting" so a corrected paste can be sent instead.
            context.user_data["awaiting"] = {"kind": "import"}
            context.user_data["pending_import"] = parsed
            await respond(update, *import_preview_screen(conn, uid, parsed, bad))
            return

        if kind == "sync":
            account = get_owned_account(conn, uid, awaiting["account_id"])
            if account is None:
                context.user_data.pop("awaiting", None)
            else:
                counts, bad = parse_sync_text(text)
                if not counts:
                    await respond(
                        update,
                        "🤔 I couldn't find any items in that. Send one item per line, "
                        "like <code>R8 120</code>, or tap Cancel.",
                        keyboard([("✖️ Cancel", f"inv:{account['id']}")]),
                    )
                    return
                # Keep "awaiting" so a corrected list can be sent instead.
                context.user_data["pending_sync"] = {"account_id": account["id"], "counts": counts}
                await respond(update, *sync_preview_screen(conn, account, counts, bad))
                return

        if kind == "qty":
            account = get_owned_account(conn, uid, awaiting["account_id"])
            default_op = "add" if awaiting["mode"] == "a" else "remove"
            qty = parse_qty(text.replace(" ", ""), default_op)
            if account is not None and qty is not None:
                op, amount = qty
                item = awaiting["item"]
                remember_undo(context.user_data, snapshot(conn, [account["id"]]), item,
                              merge_key=f"{account['id']}:{item}")
                old, new = apply_change(conn, account["id"], item, op, amount)
                note = change_note(item, op, amount, old, new)
                await respond(update, *quantity_screen(conn, account, awaiting["mode"], item, note))
                return

        # Free text: try "R8 10" style lines against the active account.
        if any(parse_line(line) for line in text.splitlines()):
            account = get_current_account(conn, uid)
            if account is None:
                await respond(update, *no_active_screen(conn, uid))
                return
            before = snapshot(conn, [account["id"]])
            report = apply_text_changes(conn, account, text)
            remember_undo(context.user_data, before, "typed")
            await respond(update, report, after_change_keyboard(account))
            return

        # Just an item name: open its +/- screen.
        item = lookup_item(text)
        account = get_current_account(conn, uid)
        if item and account:
            context.user_data["awaiting"] = {
                "kind": "qty", "account_id": account["id"], "item": item, "mode": "a",
            }
            await respond(update, *quantity_screen(conn, account, "a", item))
            return

        if not list_accounts(conn, uid):
            await respond(update, *welcome_screen())
            return

        # A bare number: we don't know which item it's for.
        if parse_qty(text.replace(" ", "")) and account:
            await respond(
                update,
                f"🤔 <b>{esc(text)}</b> of what? Send it with an item, like "
                f"<code>R8 {esc(text.lstrip('+'))}</code>, or pick the item with the buttons.",
                keyboard([("📥 Add items", f"cats:a:{account['id']}"),
                          ("📤 Remove items", f"cats:r:{account['id']}")], [HOME]),
            )
            return

        # A misspelt item: suggest the closest one.
        guess = suggest_item(text) if len(text.splitlines()) == 1 else None
        if guess and account:
            hint = (
                f"🤔 I don't know <b>{esc(text)}</b>. Did you mean <b>{esc(guess)}</b>?\n\n"
                f"Tap below to open it, or send it again like <code>{esc(guess)} 10</code>."
            )
            await respond(update, hint, keyboard(
                [(f"✅ Yes, open {guess}", f"it:a:{account['id']}:{ITEM_INDEX[guess]}")],
                [HOME, ("📋 Item names", "catalog")],
            ))
            return

    await respond(update, DIDNT_UNDERSTAND, keyboard([HOME, ("❓ Help", "help:type")], [("📋 Item names", "catalog")]))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _args_text(context) -> str:
    return " ".join(context.args).strip()


async def _show(update: Update, screen_fn, *extra):
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, update.effective_user.id)
        text, markup = screen_fn(conn, update.effective_user.id, *extra)
    await respond(update, text, markup)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting", None)
    await ensure_quick_keyboard(update, context.user_data, force=update.message.text.startswith("/start"))
    await _show(update, menu_screen)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await respond(update, *help_screen())


async def items_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await respond(update, *catalog_screen())


async def accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show(update, manage_screen)


async def listall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _show(update, all_accounts_screen)


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = _args_text(context)
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        if name:
            account = get_account_by_name(conn, uid, name)
            if account is None:
                text, markup = (
                    f"🤔 No account called <b>{esc(name)}</b>.",
                    keyboard([("🔄 See my accounts", "switch"), HOME]),
                )
            else:
                text, markup = inventory_screen(conn, uid, account)
        else:
            account = get_current_account(conn, uid)
            text, markup = inventory_screen(conn, uid, account) if account else no_active_screen(conn, uid)
    await respond(update, text, markup)


async def _items_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    uid = update.effective_user.id
    raw = _args_text(context)
    default_op = "add" if mode == "a" else "remove"
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        account = get_current_account(conn, uid)
        if account is None:
            text, markup = no_active_screen(conn, uid)
        elif not raw:
            text, markup = categories_screen(conn, account, mode)
        else:
            before = snapshot(conn, [account["id"]])
            report = apply_text_changes(conn, account, raw, default_op)
            if report:
                remember_undo(context.user_data, before, "typed")
                text, markup = report, after_change_keyboard(account)
            else:
                cmd = "/add" if mode == "a" else "/remove"
                text = (
                    f"🤔 I couldn't read <code>{esc(raw)}</code>.\n\n"
                    f"Try <code>{cmd} 5 U3</code>, or just {cmd} for the tap menu."
                )
                markup = keyboard([("📋 Item names", "catalog"), HOME])
    await respond(update, text, markup)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _items_cmd(update, context, "a")


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _items_cmd(update, context, "r")


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    query = _args_text(context)
    if not query:
        context.user_data["awaiting"] = {"kind": "search"}
        await respond(update, *search_prompt_screen())
        return
    context.user_data.pop("awaiting", None)
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        text, markup = search_results_screen(conn, uid, query)
    await respond(update, text, markup)


async def describe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    raw = re.sub(r"^/\S+", "", update.message.text).strip()
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        account = get_current_account(conn, uid)
        if account is None:
            text, markup = no_active_screen(conn, uid)
        elif not raw:
            context.user_data["awaiting"] = {"kind": "description", "account_id": account["id"]}
            text, markup = description_prompt_screen(account)
        else:
            _, error = save_description(conn, account, raw)
            if error:
                text, markup = f"⚠️ {error}", keyboard([HOME])
            else:
                text, markup = account_screen(conn, uid, get_owned_account(conn, uid, account["id"]))
    await respond(update, text, markup)


async def import_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/import opens the import prompt; /import with a paste on the next lines previews it."""
    uid = update.effective_user.id
    raw = re.sub(r"^/\S+", "", update.message.text).strip()
    context.user_data.pop("pending_import", None)
    context.user_data["awaiting"] = {"kind": "import"}
    parsed, bad = parse_import_text(raw) if raw else ([], [])
    if not parsed:
        await respond(update, *import_prompt_screen())
        return
    context.user_data["pending_import"] = parsed
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        text, markup = import_preview_screen(conn, uid, parsed, bad)
    await respond(update, text, markup)


async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sync opens the sync prompt; /sync with a list on the next lines previews it directly."""
    uid = update.effective_user.id
    raw = re.sub(r"^/\S+", "", update.message.text).strip()
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        account = get_current_account(conn, uid)
        if account is None:
            text, markup = no_active_screen(conn, uid)
        else:
            context.user_data.pop("pending_sync", None)
            context.user_data["awaiting"] = {"kind": "sync", "account_id": account["id"]}
            counts, bad = parse_sync_text(raw) if raw else ({}, [])
            if counts:
                context.user_data["pending_sync"] = {"account_id": account["id"], "counts": counts}
                text, markup = sync_preview_screen(conn, account, counts, bad)
            else:
                text, markup = sync_prompt_screen(conn, account)
    await respond(update, text, markup)


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/stats shows your stats; /stats with a paste after it saves them."""
    uid = update.effective_user.id
    raw = re.sub(r"^/\S+", "", update.message.text).strip()
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        if raw:
            context.user_data.pop("awaiting", None)
            text, markup = stats_saved_screen(conn, uid, raw)
        elif stats.list_agents(conn, uid):
            text, markup = stats_screen(conn, uid)
        else:
            context.user_data["awaiting"] = {"kind": "stats"}
            text, markup = stats_prompt_screen()
    await respond(update, text, markup)


async def goals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        agents = stats.list_agents(conn, uid)
        if agents:
            text, markup = goals_screen(conn, uid, agents[0])
        else:
            context.user_data["awaiting"] = {"kind": "stats"}
            text, markup = stats_prompt_screen("🎯 Paste your stats first, then you can set goals.\n\n")
    await respond(update, text, markup)


async def newaccount_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = _args_text(context)
    if not name:
        context.user_data["awaiting"] = {"kind": "new_account"}
        await respond(update, *new_account_prompt())
        return
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        account, error = create_account(conn, uid, name)
    if error:
        await respond(update, f"⚠️ {error}", keyboard([HOME]))
        return
    await respond(
        update,
        f"🎉 Account <b>{esc(account['name'])}</b> created — it's now your active account.",
        keyboard([("📥 Add items now", f"cats:a:{account['id']}")], [HOME]),
    )


async def use_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = _args_text(context)
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        account = get_account_by_name(conn, uid, name) if name else None
        if account:
            set_active(conn, uid, account["id"])
            text = f"🔄 Now using <b>{esc(account['name'])}</b>."
            markup = keyboard([("📥 Add items", f"cats:a:{account['id']}"), HOME])
        else:
            text, markup = switch_screen(conn, uid)
            if name:
                text = f"🤔 No account called <b>{esc(name)}</b>.\n\n" + text
    await respond(update, text, markup)


async def delaccount_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    name = _args_text(context)
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)
        account = get_account_by_name(conn, uid, name) if name else None
        if account:
            text, markup = delete_confirm_screen(conn, account)
        else:
            text, markup = manage_screen(conn, uid)
            if name:
                text = f"🤔 No account called <b>{esc(name)}</b>.\n\n" + text
    await respond(update, text, markup)


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting", None)
    with closing(get_conn()) as conn, conn:
        text, markup = menu_screen(conn, update.effective_user.id)
    await respond(update, "👌 Cancelled.\n\n" + text, markup)


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await respond(
        update,
        "🤔 I don't know that command. Here's what I can do:",
        keyboard([HOME, ("❓ Help", "help")]),
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Error while handling an update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "😬 Something went wrong on my side. Please try again, or send /menu."
            )
        except Exception:  # noqa: BLE001 - best effort only
            pass


BOT_COMMANDS = [
    ("menu", "🏠 Main menu"),
    ("add", "📥 Add items (e.g. /add 5 U3)"),
    ("remove", "📤 Remove items (e.g. /remove 2 R8)"),
    ("list", "📦 Active account's inventory"),
    ("listall", "🗂 All accounts + totals"),
    ("sync", "🧾 Replace inventory with a full list"),
    ("import", "📥 Import all accounts at once"),
    ("search", "🔍 Find an item across all accounts"),
    ("describe", "📝 Describe the active account"),
    ("stats", "📊 Agent stats (paste stats to update)"),
    ("goals", "🎯 Your goals and progress"),
    ("use", "🔄 Switch account"),
    ("newaccount", "➕ Create an account"),
    ("accounts", "⚙️ Manage accounts"),
    ("items", "📋 Item names & shortcuts"),
    ("help", "❓ How it works"),
    ("cancel", "✖️ Cancel what you're doing"),
]


# ---------------------------------------------------------------------------
# Goal reminders
# ---------------------------------------------------------------------------

REMINDER_CHECK_SECONDS = 600
_reminder_task = None


async def send_goal_reminders(bot):
    """Remind once per period about unfinished goals whose time is nearly up."""
    now = datetime.now()
    due = []
    with closing(get_conn()) as conn, conn:
        for goal in stats.periodic_goals(conn):
            p = stats.goal_progress(conn, goal, now)
            if p["complete"] or p["end"] - now > stats.REMIND_BEFORE[goal["period"]]:
                continue
            if stats.mark_alert(conn, goal["id"], p["key"], "ending", now):
                due.append((goal["owner_id"], goal["agent_id"], ending_reminder_text(goal, p, now)))
    for chat_id, agent_id, text in due:
        try:
            await bot.send_message(
                chat_id,
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard([("📊 Stats & goals", f"stats:{agent_id}")]),
            )
        except TelegramError as e:
            logger.warning("Couldn't send a goal reminder to %s: %s", chat_id, e)


async def goal_reminder_loop(bot):
    while True:
        try:
            await send_goal_reminders(bot)
        except Exception:  # noqa: BLE001 - keep the loop alive
            logger.exception("Goal reminder check failed")
        await asyncio.sleep(REMINDER_CHECK_SECONDS)


async def post_init(app: Application):
    global _reminder_task
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
    _reminder_task = asyncio.get_running_loop().create_task(goal_reminder_loop(app.bot))


async def post_stop(app: Application):
    if _reminder_task:
        _reminder_task.cancel()


def main():
    if not TOKEN:
        raise SystemExit(
            "Set the TELEGRAM_BOT_TOKEN environment variable before running "
            "(see .env.example / README)."
        )

    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .persistence(PicklePersistence(filepath=STATE_PATH))
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )

    commands = {
        "start": start_cmd,
        "menu": start_cmd,
        "help": help_cmd,
        "items": items_cmd,
        "accounts": accounts_cmd,
        "list": list_cmd,
        "listall": listall_cmd,
        "listAll": listall_cmd,
        "add": add_cmd,
        "additems": add_cmd,
        "remove": remove_cmd,
        "removeitems": remove_cmd,
        "sync": sync_cmd,
        "import": import_cmd,
        "search": search_cmd,
        "describe": describe_cmd,
        "stats": stats_cmd,
        "goals": goals_cmd,
        "newaccount": newaccount_cmd,
        "use": use_cmd,
        "delaccount": delaccount_cmd,
        "cancel": cancel_cmd,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    app.add_error_handler(on_error)

    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
