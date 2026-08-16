"""
Telegram bot for managing inventory across multiple Ingress Prime accounts.

Flow:
  1. /newaccount <name>   - register an inventory account
  2. /use <name>          - select which account is "active" for you
  3. /add <qty> <item>    - add items to the active account's inventory
  4. switch with /use <other name>, then /add more items there
  5. /list [name]         - view items for the active (or named) account
  6. /listall             - view items across every account you own

Data is stored per Telegram user, so each person who talks to the bot
has their own set of accounts/items (isolated by telegram_id).

Setup:
  pip install -r requirements.txt
  set TELEGRAM_BOT_TOKEN env var (see .env.example), then:
  python bot.py
"""

import logging
import os
import sqlite3
from contextlib import closing

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "inventory.db")

load_dotenv(os.path.join(BASE_DIR, ".env"))
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


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
]

ITEMS = [item for _, items in CATEGORIES for item in items]
ITEM_LOOKUP = {item.lower(): item for item in ITEMS}
ITEM_CATEGORY = {item: cat for cat, items in CATEGORIES for item in items}
ITEM_SORT_INDEX = {item: i for i, item in enumerate(ITEMS)}

(
    MENU,
    CHOOSING_CATEGORY,
    CHOOSING_ITEM,
    ENTERING_QTY,
    AWAIT_NEW_NAME,
    PICK_ACCOUNT,
    CONFIRM_DELETE,
) = range(7)


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


def get_account_by_name(conn, owner_id: int, name: str):
    return conn.execute(
        "SELECT * FROM accounts WHERE owner_id = ? AND name = ? COLLATE NOCASE",
        (owner_id, name),
    ).fetchone()


def get_current_account(conn, telegram_id: int):
    user = ensure_user(conn, telegram_id)
    if user["current_account_id"] is None:
        return None
    return conn.execute(
        "SELECT * FROM accounts WHERE id = ?", (user["current_account_id"],)
    ).fetchone()


def get_item_qty(conn, account_id: int, item_name: str) -> int:
    row = conn.execute(
        "SELECT quantity FROM items WHERE account_id = ? AND name = ?",
        (account_id, item_name),
    ).fetchone()
    return row["quantity"] if row else 0


def add_item_qty(conn, account_id: int, item_name: str, qty: int) -> int:
    existing = conn.execute(
        "SELECT * FROM items WHERE account_id = ? AND name = ?",
        (account_id, item_name),
    ).fetchone()
    if existing:
        new_qty = existing["quantity"] + qty
        conn.execute("UPDATE items SET quantity = ? WHERE id = ?", (new_qty, existing["id"]))
    else:
        new_qty = qty
        conn.execute(
            "INSERT INTO items (account_id, name, quantity) VALUES (?, ?, ?)",
            (account_id, item_name, qty),
        )
    return new_qty


def sorted_items(rows):
    """Sort item rows by the catalog order, unknown items last (alphabetically)."""
    return sorted(
        rows, key=lambda r: (ITEM_SORT_INDEX.get(r["name"], len(ITEMS)), r["name"])
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def main_menu_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("+ New Account", callback_data="menu:new"),
                InlineKeyboardButton("Switch Account", callback_data="menu:use"),
            ],
            [
                InlineKeyboardButton("Add Items", callback_data="menu:additems"),
                InlineKeyboardButton("Remove Items", callback_data="menu:removeitems"),
            ],
            [
                InlineKeyboardButton("My Accounts", callback_data="menu:accounts"),
                InlineKeyboardButton("Item Catalog", callback_data="menu:items"),
            ],
            [
                InlineKeyboardButton("View Current", callback_data="menu:view"),
                InlineKeyboardButton("View All", callback_data="menu:viewall"),
            ],
            [InlineKeyboardButton("Delete Account", callback_data="menu:delete")],
            [InlineKeyboardButton("Close", callback_data="menu:close")],
        ]
    )


async def show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, telegram_id)
        account = get_current_account(conn, telegram_id)

    active_line = (
        f"Active account: {account['name']}"
        if account
        else "No active account yet - start with 'New Account'."
    )
    text = f"Ingress Prime inventory manager.\n{active_line}\n\nChoose an action:"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=main_menu_keyboard())
    return MENU


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ingress Prime inventory manager.\n\n"
        "Send /menu for tappable buttons, or use these commands directly:\n\n"
        "1. /newaccount <name> - add an inventory account\n"
        "2. /use <name> - select the active account\n"
        "3. /additems - tap menu to add from the fixed item list\n"
        "   (or /add <qty> <item name> for a quick one-off, e.g. /add 5 U3)\n"
        "4. /removeitems - tap menu to remove items\n"
        "   (or /remove <qty> <item name>)\n"
        "5. /list [name] - show items for the active (or named) account\n"
        "6. /listall - show items across all your accounts\n"
        "7. /accounts - list your accounts\n"
        "8. /items - show the full recognized item list\n"
        "9. /delaccount <name> - delete an account and its items"
    )


async def newaccount_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /newaccount <account name>")
        return
    name = " ".join(context.args).strip()

    with closing(get_conn()) as conn, conn:
        ensure_user(conn, telegram_id)
        if get_account_by_name(conn, telegram_id, name):
            await update.message.reply_text(f"Account '{name}' already exists.")
            return
        cur = conn.execute(
            "INSERT INTO accounts (owner_id, name) VALUES (?, ?)",
            (telegram_id, name),
        )
        # first account created becomes the active one automatically
        user = ensure_user(conn, telegram_id)
        if user["current_account_id"] is None:
            conn.execute(
                "UPDATE users SET current_account_id = ? WHERE telegram_id = ?",
                (cur.lastrowid, telegram_id),
            )

    await update.message.reply_text(f"Account '{name}' created and selected.")


async def use_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /use <account name>")
        return
    name = " ".join(context.args).strip()

    with closing(get_conn()) as conn, conn:
        ensure_user(conn, telegram_id)
        account = get_account_by_name(conn, telegram_id, name)
        if account is None:
            await update.message.reply_text(
                f"No account named '{name}'. Use /accounts to see your accounts."
            )
            return
        conn.execute(
            "UPDATE users SET current_account_id = ? WHERE telegram_id = ?",
            (account["id"], telegram_id),
        )

    await update.message.reply_text(f"Active account switched to '{account['name']}'.")


async def accounts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    with closing(get_conn()) as conn, conn:
        user = ensure_user(conn, telegram_id)
        rows = conn.execute(
            "SELECT * FROM accounts WHERE owner_id = ? ORDER BY name COLLATE NOCASE",
            (telegram_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("You have no accounts yet. Use /newaccount <name>.")
        return

    lines = ["Your accounts:"]
    for r in rows:
        marker = " (active)" if r["id"] == user["current_account_id"] else ""
        lines.append(f"- {r['name']}{marker}")
    await update.message.reply_text("\n".join(lines))


async def delaccount_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /delaccount <account name>")
        return
    name = " ".join(context.args).strip()

    with closing(get_conn()) as conn, conn:
        ensure_user(conn, telegram_id)
        account = get_account_by_name(conn, telegram_id, name)
        if account is None:
            await update.message.reply_text(f"No account named '{name}'.")
            return
        conn.execute("DELETE FROM accounts WHERE id = ?", (account["id"],))
        user = ensure_user(conn, telegram_id)
        if user["current_account_id"] == account["id"]:
            conn.execute(
                "UPDATE users SET current_account_id = NULL WHERE telegram_id = ?",
                (telegram_id,),
            )

    await update.message.reply_text(f"Account '{name}' and its items were deleted.")


def _parse_qty_and_item(args):
    """First arg must be an integer quantity, the rest is the item name."""
    if len(args) < 2:
        return None, None
    try:
        qty = int(args[0])
    except ValueError:
        return None, None
    item_name = " ".join(args[1:]).strip()
    if not item_name:
        return None, None
    return qty, item_name


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    qty, raw_name = _parse_qty_and_item(context.args)
    if qty is None:
        await update.message.reply_text(
            "Usage: /add <quantity> <item name>\nExample: /add 5 U3\n"
            "See /items for the full list, or use /additems for a tap menu."
        )
        return
    if qty <= 0:
        await update.message.reply_text("Quantity must be a positive number.")
        return

    item_name = ITEM_LOOKUP.get(raw_name.lower())
    if item_name is None:
        await update.message.reply_text(
            f"'{raw_name}' is not a recognized item. See /items for the full list, "
            "or use /additems for a tap menu."
        )
        return

    with closing(get_conn()) as conn, conn:
        account = get_current_account(conn, telegram_id)
        if account is None:
            await update.message.reply_text(
                "No active account. Use /newaccount <name> or /use <name> first."
            )
            return
        new_qty = add_item_qty(conn, account["id"], item_name, qty)

    await update.message.reply_text(
        f"[{account['name']}] {item_name}: {new_qty} (added {qty})"
    )


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    qty, raw_name = _parse_qty_and_item(context.args)
    if qty is None:
        await update.message.reply_text("Usage: /remove <quantity> <item name>")
        return
    if qty <= 0:
        await update.message.reply_text("Quantity must be a positive number.")
        return

    item_name = ITEM_LOOKUP.get(raw_name.lower())
    if item_name is None:
        await update.message.reply_text(
            f"'{raw_name}' is not a recognized item. See /items for the full list."
        )
        return

    with closing(get_conn()) as conn, conn:
        account = get_current_account(conn, telegram_id)
        if account is None:
            await update.message.reply_text("No active account. Use /use <name> first.")
            return
        existing = conn.execute(
            "SELECT * FROM items WHERE account_id = ? AND name = ?",
            (account["id"], item_name),
        ).fetchone()
        if existing is None:
            await update.message.reply_text(f"'{item_name}' is not in [{account['name']}].")
            return
        new_qty = existing["quantity"] - qty
        if new_qty <= 0:
            conn.execute("DELETE FROM items WHERE id = ?", (existing["id"],))
            new_qty = 0
        else:
            conn.execute("UPDATE items SET quantity = ? WHERE id = ?", (new_qty, existing["id"]))

    await update.message.reply_text(f"[{account['name']}] {item_name}: {new_qty}")


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, telegram_id)
        if context.args:
            name = " ".join(context.args).strip()
            account = get_account_by_name(conn, telegram_id, name)
            if account is None:
                await update.message.reply_text(f"No account named '{name}'.")
                return
        else:
            account = get_current_account(conn, telegram_id)
            if account is None:
                await update.message.reply_text(
                    "No active account. Use /newaccount <name> or /use <name>, "
                    "or pass a name: /list <account name>"
                )
                return

        items = conn.execute(
            "SELECT name, quantity FROM items WHERE account_id = ?",
            (account["id"],),
        ).fetchall()

    if not items:
        await update.message.reply_text(f"[{account['name']}] has no items yet.")
        return

    lines = [f"Inventory for [{account['name']}]:"]
    for it in sorted_items(items):
        lines.append(f"- {it['name']}: {it['quantity']}")
    await update.message.reply_text("\n".join(lines))


async def listall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    with closing(get_conn()) as conn, conn:
        ensure_user(conn, telegram_id)
        accounts = conn.execute(
            "SELECT * FROM accounts WHERE owner_id = ? ORDER BY name COLLATE NOCASE",
            (telegram_id,),
        ).fetchall()

        if not accounts:
            await update.message.reply_text("You have no accounts yet. Use /newaccount <name>.")
            return

        blocks = []
        for account in accounts:
            items = conn.execute(
                "SELECT name, quantity FROM items WHERE account_id = ?",
                (account["id"],),
            ).fetchall()
            block_lines = [f"[{account['name']}]"]
            if items:
                for it in sorted_items(items):
                    block_lines.append(f"  - {it['name']}: {it['quantity']}")
            else:
                block_lines.append("  (empty)")
            blocks.append("\n".join(block_lines))

    await update.message.reply_text("\n\n".join(blocks))


async def items_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = ["Recognized items:"]
    for cat_name, cat_items in CATEGORIES:
        lines.append(f"\n{cat_name}: " + ", ".join(cat_items))
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Add/remove items - tap-to-pick menu (category -> item -> quantity)
# Shared by /additems, /removeitems and their menu-button equivalents;
# context.user_data["mode"] is "add" or "remove".
# ---------------------------------------------------------------------------

def _category_keyboard():
    buttons = [
        [InlineKeyboardButton(cat_name, callback_data=f"cat:{i}")]
        for i, (cat_name, _) in enumerate(CATEGORIES)
    ]
    buttons.append([InlineKeyboardButton("Done", callback_data="done")])
    return InlineKeyboardMarkup(buttons)


def _item_keyboard(conn, account_id: int, cat_idx: int):
    cat_name, cat_items = CATEGORIES[cat_idx]
    buttons = []
    row = []
    for item_name in cat_items:
        qty = get_item_qty(conn, account_id, item_name)
        row.append(
            InlineKeyboardButton(f"{item_name} ({qty})", callback_data=f"item:{item_name}")
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append(
        [
            InlineKeyboardButton("<- Categories", callback_data="back:cat"),
            InlineKeyboardButton("Done", callback_data="done"),
        ]
    )
    return cat_name, InlineKeyboardMarkup(buttons)


async def _begin_items_flow(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    """Shared entry logic for the add/remove tap flow, from a command or a menu button."""
    query = update.callback_query
    telegram_id = update.effective_user.id

    with closing(get_conn()) as conn, conn:
        account = get_current_account(conn, telegram_id)

    if account is None:
        text = (
            "No active account. Use 'New Account' / 'Switch Account' first "
            "(or /newaccount, /use)."
        )
        if query:
            await query.edit_message_text(text, reply_markup=main_menu_keyboard())
            return MENU
        await update.message.reply_text(text)
        return ConversationHandler.END

    context.user_data["mode"] = mode
    context.user_data["additems_account_id"] = account["id"]
    context.user_data["additems_account_name"] = account["name"]

    verb_label = "Adding items to" if mode == "add" else "Removing items from"
    text = f"{verb_label} [{account['name']}]. Pick a category:"
    if query:
        await query.edit_message_text(text, reply_markup=_category_keyboard())
    else:
        await update.message.reply_text(text, reply_markup=_category_keyboard())
    return CHOOSING_CATEGORY


async def additems_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _begin_items_flow(update, context, "add")


async def removeitems_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _begin_items_flow(update, context, "remove")


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_idx = int(query.data.split(":", 1)[1])
    context.user_data["cat_idx"] = cat_idx
    mode = context.user_data.get("mode", "add")
    verb = "add" if mode == "add" else "remove"

    account_id = context.user_data["additems_account_id"]
    with closing(get_conn()) as conn, conn:
        cat_name, keyboard = _item_keyboard(conn, account_id, cat_idx)

    await query.edit_message_text(
        f"Category: {cat_name}\nTap an item to {verb}:", reply_markup=keyboard
    )
    return CHOOSING_ITEM


async def back_to_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_name = context.user_data.get("additems_account_name", "")
    mode = context.user_data.get("mode", "add")
    verb_label = "Adding items to" if mode == "add" else "Removing items from"
    await query.edit_message_text(
        f"{verb_label} [{account_name}]. Pick a category:",
        reply_markup=_category_keyboard(),
    )
    return CHOOSING_CATEGORY


async def item_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    item_name = query.data.split(":", 1)[1]
    context.user_data["pending_item"] = item_name
    mode = context.user_data.get("mode", "add")
    verb = "add" if mode == "add" else "remove"
    await query.edit_message_text(f"Enter quantity to {verb} for '{item_name}' (number, /cancel to stop):")
    return ENTERING_QTY


async def qty_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        qty = int(text)
    except ValueError:
        await update.message.reply_text("Please send a whole number (e.g. 5), or /cancel.")
        return ENTERING_QTY
    if qty < 0:
        await update.message.reply_text("Quantity can't be negative. Send a number >= 0, or /cancel.")
        return ENTERING_QTY

    mode = context.user_data.get("mode", "add")
    item_name = context.user_data["pending_item"]
    account_id = context.user_data["additems_account_id"]
    account_name = context.user_data["additems_account_name"]
    cat_idx = context.user_data["cat_idx"]

    with closing(get_conn()) as conn, conn:
        if mode == "add":
            new_qty = add_item_qty(conn, account_id, item_name, qty) if qty > 0 else get_item_qty(
                conn, account_id, item_name
            )
        else:
            existing_qty = get_item_qty(conn, account_id, item_name)
            if qty > 0:
                new_qty = max(existing_qty - qty, 0)
                if new_qty == 0:
                    conn.execute(
                        "DELETE FROM items WHERE account_id = ? AND name = ?",
                        (account_id, item_name),
                    )
                else:
                    conn.execute(
                        "UPDATE items SET quantity = ? WHERE account_id = ? AND name = ?",
                        (new_qty, account_id, item_name),
                    )
            else:
                new_qty = existing_qty
        cat_name, keyboard = _item_keyboard(conn, account_id, cat_idx)

    verb_label = "add" if mode == "add" else "remove"
    if qty > 0:
        verb_past = "added" if mode == "add" else "removed"
        prefix = f"[{account_name}] {item_name}: {new_qty} ({verb_past} {qty})\n\n"
    else:
        prefix = ""
    await update.message.reply_text(
        f"{prefix}Category: {cat_name}\nTap an item to {verb_label}:", reply_markup=keyboard
    )
    return CHOOSING_ITEM


async def finish_items(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_name = context.user_data.get("additems_account_name", "")
    mode = context.user_data.pop("mode", "add")
    for key in ("additems_account_id", "additems_account_name", "cat_idx", "pending_item"):
        context.user_data.pop(key, None)
    verb_label = "adding items to" if mode == "add" else "removing items from"
    await query.edit_message_text(
        f"Done {verb_label} [{account_name}].", reply_markup=main_menu_keyboard()
    )
    return MENU


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled. Send /menu to reopen.")
    context.user_data.clear()
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Account picker (Switch Account / Delete Account menu buttons)
# ---------------------------------------------------------------------------

def _account_keyboard(accounts):
    buttons = [
        [InlineKeyboardButton(a["name"], callback_data=f"acc:{a['id']}")] for a in accounts
    ]
    buttons.append([InlineKeyboardButton("<- Menu", callback_data="menu:back")])
    return InlineKeyboardMarkup(buttons)


async def account_picked(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    account_id = int(query.data.split(":", 1)[1])
    pick_action = context.user_data.get("pick_action")
    telegram_id = update.effective_user.id

    with closing(get_conn()) as conn, conn:
        account = conn.execute(
            "SELECT * FROM accounts WHERE id = ? AND owner_id = ?", (account_id, telegram_id)
        ).fetchone()
        if account is None:
            await query.edit_message_text("That account no longer exists.", reply_markup=main_menu_keyboard())
            return MENU

        if pick_action == "use":
            conn.execute(
                "UPDATE users SET current_account_id = ? WHERE telegram_id = ?",
                (account_id, telegram_id),
            )
            await query.edit_message_text(
                f"Active account switched to '{account['name']}'.", reply_markup=main_menu_keyboard()
            )
            return MENU

        if pick_action == "delete":
            context.user_data["delete_account_id"] = account_id
            context.user_data["delete_account_name"] = account["name"]
            kb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton(f"Yes, delete '{account['name']}'", callback_data="delconfirm:yes")],
                    [InlineKeyboardButton("Cancel", callback_data="menu:back")],
                ]
            )
            await query.edit_message_text(
                f"Delete account '{account['name']}' and ALL its items? This can't be undone.",
                reply_markup=kb,
            )
            return CONFIRM_DELETE

    await query.edit_message_text("Choose an action:", reply_markup=main_menu_keyboard())
    return MENU


async def delete_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    account_id = context.user_data.pop("delete_account_id", None)
    account_name = context.user_data.pop("delete_account_name", "")

    with closing(get_conn()) as conn, conn:
        conn.execute(
            "DELETE FROM accounts WHERE id = ? AND owner_id = ?", (account_id, telegram_id)
        )
        user = ensure_user(conn, telegram_id)
        if user["current_account_id"] == account_id:
            conn.execute(
                "UPDATE users SET current_account_id = NULL WHERE telegram_id = ?", (telegram_id,)
            )

    await query.edit_message_text(f"Account '{account_name}' deleted.", reply_markup=main_menu_keyboard())
    return MENU


async def new_account_name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Please send a non-empty account name, or /cancel.")
        return AWAIT_NEW_NAME

    with closing(get_conn()) as conn, conn:
        ensure_user(conn, telegram_id)
        if get_account_by_name(conn, telegram_id, name):
            await update.message.reply_text(
                f"Account '{name}' already exists. Send a different name, or /cancel."
            )
            return AWAIT_NEW_NAME
        cur = conn.execute(
            "INSERT INTO accounts (owner_id, name) VALUES (?, ?)", (telegram_id, name)
        )
        user = ensure_user(conn, telegram_id)
        if user["current_account_id"] is None:
            conn.execute(
                "UPDATE users SET current_account_id = ? WHERE telegram_id = ?",
                (cur.lastrowid, telegram_id),
            )

    await update.message.reply_text(
        f"Account '{name}' created and selected.", reply_markup=main_menu_keyboard()
    )
    return MENU


# ---------------------------------------------------------------------------
# Main menu dispatcher
# ---------------------------------------------------------------------------

async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]
    telegram_id = update.effective_user.id

    if action == "back":
        return await show_menu(update, context)

    if action == "close":
        await query.edit_message_text("Closed. Send /menu to reopen.")
        context.user_data.clear()
        return ConversationHandler.END

    if action == "new":
        await query.edit_message_text("Send the new account name as a message (or /cancel):")
        return AWAIT_NEW_NAME

    if action in ("use", "delete"):
        with closing(get_conn()) as conn, conn:
            accounts = conn.execute(
                "SELECT * FROM accounts WHERE owner_id = ? ORDER BY name COLLATE NOCASE",
                (telegram_id,),
            ).fetchall()
        if not accounts:
            await query.edit_message_text(
                "You have no accounts yet. Tap 'New Account' first.",
                reply_markup=main_menu_keyboard(),
            )
            return MENU
        context.user_data["pick_action"] = action
        prompt = "Pick an account to switch to:" if action == "use" else "Pick an account to DELETE:"
        await query.edit_message_text(prompt, reply_markup=_account_keyboard(accounts))
        return PICK_ACCOUNT

    if action == "accounts":
        with closing(get_conn()) as conn, conn:
            user = ensure_user(conn, telegram_id)
            rows = conn.execute(
                "SELECT * FROM accounts WHERE owner_id = ? ORDER BY name COLLATE NOCASE",
                (telegram_id,),
            ).fetchall()
        if not rows:
            text = "You have no accounts yet."
        else:
            lines = ["Your accounts:"]
            for r in rows:
                marker = " (active)" if r["id"] == user["current_account_id"] else ""
                lines.append(f"- {r['name']}{marker}")
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())
        return MENU

    if action == "items":
        lines = ["Recognized items:"]
        for cat_name, cat_items in CATEGORIES:
            lines.append(f"\n{cat_name}: " + ", ".join(cat_items))
        await query.edit_message_text("\n".join(lines), reply_markup=main_menu_keyboard())
        return MENU

    if action == "view":
        with closing(get_conn()) as conn, conn:
            account = get_current_account(conn, telegram_id)
            if account is None:
                await query.edit_message_text("No active account yet.", reply_markup=main_menu_keyboard())
                return MENU
            items = conn.execute(
                "SELECT name, quantity FROM items WHERE account_id = ?", (account["id"],)
            ).fetchall()
        if not items:
            text = f"[{account['name']}] has no items yet."
        else:
            lines = [f"Inventory for [{account['name']}]:"]
            for it in sorted_items(items):
                lines.append(f"- {it['name']}: {it['quantity']}")
            text = "\n".join(lines)
        await query.edit_message_text(text, reply_markup=main_menu_keyboard())
        return MENU

    if action == "viewall":
        with closing(get_conn()) as conn, conn:
            accounts = conn.execute(
                "SELECT * FROM accounts WHERE owner_id = ? ORDER BY name COLLATE NOCASE",
                (telegram_id,),
            ).fetchall()
            if not accounts:
                await query.edit_message_text(
                    "You have no accounts yet.", reply_markup=main_menu_keyboard()
                )
                return MENU
            blocks = []
            for account in accounts:
                items = conn.execute(
                    "SELECT name, quantity FROM items WHERE account_id = ?", (account["id"],)
                ).fetchall()
                block_lines = [f"[{account['name']}]"]
                if items:
                    for it in sorted_items(items):
                        block_lines.append(f"  - {it['name']}: {it['quantity']}")
                else:
                    block_lines.append("  (empty)")
                blocks.append("\n".join(block_lines))
        await query.edit_message_text("\n\n".join(blocks), reply_markup=main_menu_keyboard())
        return MENU

    if action in ("additems", "removeitems"):
        mode = "add" if action == "additems" else "remove"
        return await _begin_items_flow(update, context, mode)

    await query.edit_message_text("Choose an action:", reply_markup=main_menu_keyboard())
    return MENU


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Send /help to see available commands.")


def main():
    if not TOKEN:
        raise SystemExit(
            "Set the TELEGRAM_BOT_TOKEN environment variable before running "
            "(see .env.example / README)."
        )

    init_db()

    app = Application.builder().token(TOKEN).build()

    # Plain-text commands, always available regardless of menu state.
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("newaccount", newaccount_cmd))
    app.add_handler(CommandHandler("use", use_cmd))
    app.add_handler(CommandHandler("accounts", accounts_cmd))
    app.add_handler(CommandHandler("delaccount", delaccount_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("listall", listall_cmd))
    app.add_handler(CommandHandler("listAll", listall_cmd))
    app.add_handler(CommandHandler("items", items_cmd))

    # Button-driven menu: /start or /menu opens it; /additems and /removeitems
    # jump straight into the tap-to-pick item flow.
    main_conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", show_menu),
            CommandHandler("menu", show_menu),
            CommandHandler("additems", additems_entry),
            CommandHandler("removeitems", removeitems_entry),
        ],
        states={
            MENU: [CallbackQueryHandler(menu_button, pattern=r"^menu:")],
            CHOOSING_CATEGORY: [CallbackQueryHandler(category_chosen, pattern=r"^cat:")],
            CHOOSING_ITEM: [
                CallbackQueryHandler(item_chosen, pattern=r"^item:"),
                CallbackQueryHandler(back_to_categories, pattern=r"^back:cat$"),
            ],
            ENTERING_QTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, qty_entered)],
            AWAIT_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_account_name_entered)],
            PICK_ACCOUNT: [
                CallbackQueryHandler(account_picked, pattern=r"^acc:"),
                CallbackQueryHandler(menu_button, pattern=r"^menu:"),
            ],
            CONFIRM_DELETE: [
                CallbackQueryHandler(delete_confirmed, pattern=r"^delconfirm:"),
                CallbackQueryHandler(menu_button, pattern=r"^menu:"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_conv),
            CommandHandler("menu", show_menu),
            CallbackQueryHandler(finish_items, pattern=r"^done$"),
        ],
    )
    app.add_handler(main_conv)

    logger.info("Bot starting (polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
