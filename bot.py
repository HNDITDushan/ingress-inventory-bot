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

import html
import logging
import os
import re
import sqlite3
from contextlib import closing

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
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


def split_message(text: str):
    if len(text) <= MAX_MESSAGE_LEN:
        return [text]
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


def menu_screen(conn, uid: int):
    accounts = list_accounts(conn, uid)
    if not accounts:
        return welcome_screen()
    active = get_current_account(conn, uid)
    if active:
        total = sum(get_counts(conn, active["id"]).values())
        active_line = f"👤 Active account: <b>{esc(active['name'])}</b> · {plural(total)}"
    else:
        active_line = "👤 No active account — tap 🔄 Switch account to pick one."
    text = (
        "🏠 <b>Ingress Inventory</b>\n\n"
        f"{active_line}\n"
        f"🗂 You have {plural(len(accounts), 'account')}\n\n"
        "💡 <i>Tip: just send me</i> <code>R8 10</code> <i>to add 10 R8 to the active account.</i>"
    )
    markup = keyboard(
        [("📥 Add items", "cats:a"), ("📤 Remove items", "cats:r")],
        [("📦 My inventory", "inv"), ("🗂 All accounts", "all")],
        [("🔍 Search all accounts", "search")],
        [("🧾 Sync this account", "sync"), ("📥 Import all accounts", "import")],
        [("🔄 Switch account", "switch"), ("➕ New account", "new")],
        [("⚙️ Manage accounts", "manage"), ("❓ Help", "help")],
    )
    return text, markup


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
    return text, keyboard(*rows, nav)


def inventory_screen(conn, uid: int, account):
    active = get_current_account(conn, uid)
    is_active = active is not None and active["id"] == account["id"]
    return (
        account_block(conn, account, is_active),
        keyboard(
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


def help_screen():
    text = (
        "❓ <b>How it works</b>\n\n"
        "<b>1. Accounts</b>\n"
        "Make one account for each Ingress agent you play. New items go into the "
        "<i>active</i> account. Change it any time with 🔄 Switch account.\n\n"
        "<b>2. Adding gear with buttons</b>\n"
        "Tap 📥 Add items → pick a category → pick an item → tap +1, +5, +10… "
        "or type a number.\n\n"
        "<b>3. Typing is even faster</b>\n"
        "Just send me a message like:\n"
        "<code>R8 10</code>  → add 10 R8\n"
        "<code>-3 VRPS</code>  → remove 3 Very Rare Shields\n"
        "<code>Keys =120</code>  → set Keys to exactly 120\n"
        "Send several lines at once to update many items.\n\n"
        "<b>4. Syncing everything at once</b>\n"
        "Tap 🧾 Sync full inventory and send your complete list (one item per line). "
        "The account is set to match it exactly — items you leave out become 0. "
        "You see a preview first.\n\n"
        "Got several accounts? Tap 📥 Import all accounts and paste them all in one "
        "message, each starting with <code>[Account name]</code>.\n\n"
        "<b>5. Finding gear</b>\n"
        "Tap 🔍 Search all accounts and send an item (<code>R8</code>) to see which accounts "
        "hold it and the total, or a wider word (<code>shield</code>, <code>res</code>) for a "
        "whole family. It also searches account names and descriptions.\n\n"
        "<b>6. Describing accounts</b>\n"
        "In ⚙️ Manage accounts, pick an account and tap 📝 Add description to note things "
        "like which area's keys it holds.\n\n"
        "<b>7. Checking your gear</b>\n"
        "📦 My inventory shows the active account. 🗂 All accounts shows every "
        "account plus combined totals.\n\n"
        "<b>Commands</b>\n"
        "/menu – main menu\n"
        "/add 5 U3 – quick add\n"
        "/remove 2 R8 – quick remove\n"
        "/list – active account's items\n"
        "/listall – all accounts\n"
        "/sync – replace the whole inventory with a pasted list\n"
        "/import – import many accounts at once\n"
        "/search <i>item</i> – find an item across all accounts\n"
        "/describe <i>text</i> – describe the active account\n"
        "/use <i>name</i> – switch account\n"
        "/newaccount <i>name</i> – create an account\n"
        "/items – item names and shortcuts\n"
        "/cancel – stop what you're doing"
    )
    return text, keyboard([("📋 Item names & shortcuts", "catalog")], [HOME])


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
        report += "\n\n⚠️ I couldn't read:\n" + "\n".join(f"• <code>{esc(b)}</code>" for b in bad)
        report += "\nSee /items for item names."
    return report


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
        text += "\n".join(f"• <code>{esc(b)}</code>" for b in bad)
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
        text += "\n".join(f"• <code>{esc(b)}</code>" for b in bad[:20])
        if len(bad) > 20:
            text += f"\n…and {len(bad) - 20} more"
    text += "\n\nApply this? You can also send a corrected list instead."
    return text, keyboard([("✅ Apply import", "importok")], [("✖️ Cancel", "menu")])


def apply_import(conn, uid: int, parsed):
    """Create/replace each account. Returns (created, updated) counts."""
    created = updated = 0
    for name, counts in parsed:
        account = find_import_account(conn, uid, name)
        if account is None:
            clean, error = clean_account_name(name)
            if error:
                continue
            cur = conn.execute("INSERT INTO accounts (owner_id, name) VALUES (?, ?)", (uid, clean))
            account_id = cur.lastrowid
            created += 1
        else:
            account_id = account["id"]
            updated += 1
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


def after_change_keyboard(account):
    return keyboard([("📦 View inventory", f"inv:{account['id']}"), HOME])


DIDNT_UNDERSTAND = (
    "🤔 Sorry, I didn't catch that.\n\n"
    "To change items, send something like:\n"
    "<code>R8 10</code>  or  <code>-3 VRPS</code>  or  <code>Keys =120</code>\n\n"
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
    return (*quantity_screen(conn, account, mode, item), None)


def btn_quantity(conn, uid, ud, args):
    mode, account = _mode(args[0]), _owned(conn, uid, args[1])
    item = _item(args[2])
    if args[3] == "all":
        op, amount = "set", 0
    else:
        delta = int(args[3])
        op, amount = ("add", delta) if delta > 0 else ("remove", -delta)
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
    replace_inventory(conn, account["id"], pending["counts"])
    return (*inventory_screen(conn, uid, account), "Inventory synced ✅")


def btn_import(conn, uid, ud, args):
    ud.pop("pending_import", None)
    ud["awaiting"] = {"kind": "import"}
    return (*import_prompt_screen(), None)


def btn_import_ok(conn, uid, ud, args):
    pending = ud.pop("pending_import", None)
    if not pending:
        raise StaleButton
    created, updated = apply_import(conn, uid, pending)
    toast = f"Imported: {created} new, {updated} updated"
    return (*all_accounts_screen(conn, uid), toast)


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


def btn_help(conn, uid, ud, args):
    return (*help_screen(), None)


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
    "help": btn_help,
    "catalog": btn_catalog,
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

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    awaiting = context.user_data.get("awaiting") or {}
    kind = awaiting.get("kind")

    with closing(get_conn()) as conn, conn:
        ensure_user(conn, uid)

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
            report = apply_text_changes(conn, account, text)
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

    await respond(update, DIDNT_UNDERSTAND, keyboard([HOME, ("📋 Item names", "catalog")]))


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
            report = apply_text_changes(conn, account, raw, default_op)
            if report:
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
    ("use", "🔄 Switch account"),
    ("newaccount", "➕ Create an account"),
    ("accounts", "⚙️ Manage accounts"),
    ("items", "📋 Item names & shortcuts"),
    ("help", "❓ How it works"),
    ("cancel", "✖️ Cancel what you're doing"),
]


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])


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
        "newaccount": newaccount_cmd,
        "use": use_cmd,
        "delaccount": delaccount_cmd,
        "cancel": cancel_cmd,
    }
    for name, handler in commands.items():
        app.add_handler(CommandHandler(name, handler))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
    app.add_error_handler(on_error)

    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
