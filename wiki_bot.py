"""
Telegram side of the Sinhala Ingress Wiki assistant.

Kept separate from bot.py: bot.py registers the commands, sends taps on
w* buttons and typed answers for wiki prompts here, and nothing in the
inventory flows changes. A failure in here is caught and turned into a
friendly Sinhala message, so it can't take the inventory bot down.
"""

import asyncio
import html
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

import wiki
import wiki_qa
import wiki_si

logger = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 3900
MAX_QUESTION_LEN = 500
PER_MINUTE = int(os.environ.get("WIKI_PER_MINUTE", "5"))
PER_DAY = int(os.environ.get("WIKI_PER_DAY", "60"))
UPDATE_HOURS = float(os.environ.get("WIKI_UPDATE_HOURS", "24"))
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

store: wiki.WikiStore = None
assistant: wiki_qa.WikiAssistant = None
ADMIN_IDS: set = set()
_requests = defaultdict(deque)       # uid -> recent request times
_update_task = None


def setup(base_dir: str):
    global store, assistant, ADMIN_IDS
    path = os.environ.get("WIKI_DB_PATH") or os.path.join(base_dir, "wiki.db")
    store = wiki.WikiStore(path)
    assistant = wiki_qa.WikiAssistant(store)
    ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
    si = wiki_si.load(os.path.join(base_dir, "wiki_si"))
    logger.info("Wiki assistant ready: %d pages, %d with Sinhala text, AI %s, %d admin(s)",
                store.page_count(), si, "on" if wiki_qa.is_enabled() else "off", len(ADMIN_IDS))


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def kb(*rows):
    """Rows of (label, callback_data) or (label, None, url)."""
    out = []
    for row in rows:
        buttons = []
        for b in row:
            if len(b) == 3:
                buttons.append(InlineKeyboardButton(b[0], url=b[2]))
            else:
                buttons.append(InlineKeyboardButton(b[0], callback_data=b[1]))
        if buttons:
            out.append(buttons)
    return InlineKeyboardMarkup(out)


HOME = ("🏠 Menu", "menu")
WIKI_HOME = ("📚 Wiki", "wiki")
ASK_AGAIN = ("❓ තවත් ප්‍රශ්නයක්", "wask")


def split_text(text: str):
    """Split long answers between paragraphs (then lines) so HTML tags stay whole."""
    if len(text) <= MAX_MESSAGE_LEN:
        return [text]
    chunks, current = [], ""
    for para in text.split("\n\n"):
        piece = para if len(para) <= MAX_MESSAGE_LEN else None
        if piece is None:
            for line in para.split("\n"):
                if current and len(current) + len(line) + 1 > MAX_MESSAGE_LEN:
                    chunks.append(current)
                    current = ""
                current = f"{current}\n{line}" if current else line
            continue
        if current and len(current) + len(piece) + 2 > MAX_MESSAGE_LEN:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


async def send(update: Update, text: str, markup=None, edit_message=None):
    """Send (or edit) a possibly long HTML message; the buttons go on the last part."""
    chunks = split_text(text)
    target = update.effective_message
    for i, chunk in enumerate(chunks):
        last = i == len(chunks) - 1
        rm = markup if last else None
        if i == 0 and edit_message is not None:
            try:
                await edit_message.edit_text(chunk, parse_mode=ParseMode.HTML, reply_markup=rm,
                                             link_preview_options=NO_PREVIEW)
                continue
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    continue
        await target.reply_text(chunk, parse_mode=ParseMode.HTML, reply_markup=rm,
                                link_preview_options=NO_PREVIEW)


async def send_images(update: Update, page) -> bool:
    """The page's wiki pictures: one photo, or an album of up to 4. False if none/failed."""
    urls = store.images(page["pageid"]) if page is not None else []
    if not urls:
        return False
    caption = f'🖼 <b>{esc(page["title"])}</b> · <a href="{esc(page["url"])}">Ingress Wiki</a>'
    try:
        if len(urls) == 1:
            await update.effective_message.reply_photo(urls[0], caption=caption, parse_mode=ParseMode.HTML)
        else:
            media = [InputMediaPhoto(u, caption=caption if i == 0 else None,
                                     parse_mode=ParseMode.HTML if i == 0 else None)
                     for i, u in enumerate(urls[:4])]
            await update.effective_message.reply_media_group(media)
        return True
    except TelegramError as exc:
        logger.warning("Wiki images for %r not sent: %s", page["title"], exc)
        return False


async def _drop(message):
    if message is None:
        return
    try:
        await message.delete()
    except TelegramError:
        pass


# ---------------------------------------------------------------------------
# Screens
# ---------------------------------------------------------------------------

def intro_screen():
    pages = store.page_count()
    lines = [
        "📚 <b>Ingress Wiki සහායකයා</b>",
        "",
        "Ingress Prime ගැන ඕනෑම ප්‍රශ්නයක් සිංහලෙන්, English වලින් හෝ දෙකම මිශ්‍ර කරලා අහන්න. "
        "Ingress Wiki එකේ තොරතුරු අනුව සිංහලෙන් පිළිතුරු දෙනවා, මූලාශ්‍ර link එකත් එක්කම.",
        "",
        "<b>භාවිතා කරන හැටි:</b>",
        "• <code>/ask ADA Refactor කියන්නේ මොකක්ද?</code>",
        "• <code>/item Heat Sink</code> – item එකක විස්තර (<code>/item</code> තනියම = items ලැයිස්තුව)",
        "• <code>/compare XMP Burster vs Ultra Strike</code>",
        "• <code>/portal</code> – Portal ගැන විස්තර",
        "• <code>/wsearch Jarvis</code> – Wiki එකේ සොයන්න",
        "",
        f"<i>📖 Wiki පිටු {pages}ක් දැනුම් ගබඩාවේ තියෙනවා.</i>",
    ]
    if wiki_si.count():
        lines.append(f"<i>🇱🇰 Wiki පිටු {wiki_si.count()}කට සිංහල විස්තර සූදානම් — AI නැතුවම වැඩ කරනවා.</i>")
    elif not wiki_qa.is_enabled():
        lines.append("<i>ℹ️ සිංහල AI පිළිතුරු දැනට off — Wiki සෙවීම English වලින් වැඩ කරනවා.</i>")
    if pages == 0:
        lines.append("<i>⏳ දැනුම් ගබඩාව තවම සකස් වෙමින් පවතිනවා. ටික වෙලාවකින් නැවත උත්සාහ කරන්න.</i>")
    return "\n".join(lines), kb(
        [("❓ ප්‍රශ්නයක් අහන්න", "wask"), ("🧰 Items", "wits")],
        [("⚔️ Compare", "wcmp"), ("🔍 Wiki සෙවීම", "wsrch")],
        [("🌀 Portals", "wport")],
        [HOME],
    )


# Item topics: groups shown in the 🧰 Items browser. Items from the wiki's
# Category:Items that aren't listed here appear under "Other".
ITEM_GROUPS = [
    ("⚔️ Weapons", ["XMP Burster", "Ultra Strike", "ADA Refactor", "JARVIS Virus", "Flip Card"]),
    ("🛡️ Portal Mods", ["Portal Shield", "Force Amp", "Turret", "Heat Sink", "Multi-Hack", "Link Amp",
                        "ITO EN Transmuter"]),
    ("🔋 Resonators & Power", ["Resonator", "Power Cube"]),
    ("🔑 Keys & Storage", ["Portal Key", "Capsule", "Key Locker", "Media"]),
    ("🚀 Boosts & Store", ["Apex", "Battle Beacon", "Beacon", "Portal Fracker"]),
]
ITEM_EMOJI = {
    "XMP Burster": "💥", "Ultra Strike": "⚡", "ADA Refactor": "🔵", "JARVIS Virus": "🟢", "Flip Card": "🔄",
    "Portal Shield": "🛡️", "Force Amp": "💪", "Turret": "🎯", "Heat Sink": "🔥", "Multi-Hack": "🔁",
    "Link Amp": "📡", "ITO EN Transmuter": "🍵", "Resonator": "🔋", "Power Cube": "🧊", "Portal Key": "🔑",
    "Capsule": "📦", "Key Locker": "🔐", "Media": "🎞️", "Apex": "⚡", "Battle Beacon": "🏁",
    "Beacon": "📍", "Portal Fracker": "⛏️",
}


def item_groups():
    """[(group label, [page rows])] from the wiki's Category:Items (static list as fallback)."""
    titles = [t for t in store.category("Items") if t != "Items"]
    if not titles:
        titles = [t for _, ts in ITEM_GROUPS for t in ts]
    pages = {t: store.get_page(t) for t in titles}
    pages = {t: p for t, p in pages.items() if p is not None}
    groups, used = [], set()
    for label, names in ITEM_GROUPS:
        rows = [pages[n] for n in names if n in pages]
        used.update(names)
        if rows:
            groups.append((label, rows))
    other = [p for t, p in sorted(pages.items()) if t not in used]
    if other:
        groups.append(("🧩 Other", other))
    return groups


def items_screen():
    groups = item_groups()
    total = sum(len(rows) for _, rows in groups)
    parts = [f"🧰 <b>Ingress Items</b> · {total}",
             "කාණ්ඩයක් තෝරන්න. Item එකක් ඔබපුවම සිංහල විස්තරය සහ Wiki පින්තූර එනවා."]
    parts += [f"<b>{esc(label)}</b>\n<i>{esc(', '.join(p['title'] for p in rows))}</i>" for label, rows in groups]
    parts.append("✍️ <i>නැත්නම් item එකේ නම type කරන්න (උදා: VRHS, Aegis).</i>")
    buttons = [(f"{label} ({len(rows)})", f"witg:{i}") for i, (label, rows) in enumerate(groups)]
    return "\n\n".join(parts), kb(
        *[buttons[i:i + 2] for i in range(0, len(buttons), 2)],
        [WIKI_HOME, HOME],
    )


def item_group_screen(index: int):
    groups = item_groups()
    if not 0 <= index < len(groups):
        return items_screen()
    label, rows = groups[index]
    lines = [f"<b>{esc(label)}</b>", "", "Item එකක් තෝරන්න:"]
    buttons = [(f"{ITEM_EMOJI.get(p['title'], '▫️')} {p['title']}", f"wpg:{p['pageid']}") for p in rows]
    return "\n".join(lines), kb(
        *[buttons[i:i + 2] for i in range(0, len(buttons), 2)],
        [("⬅️ Items", "wits"), WIKI_HOME],
    )


PROMPTS = {
    "wiki_ask": ("❓ <b>ප්‍රශ්නය අහන්න</b>\n\nIngress ගැන ඔබේ ප්‍රශ්නය යවන්න. උදා:\n"
                 "<code>Heat Sink එකෙන් මොකද වෙන්නේ?</code>\n<code>What is a Control Field?</code>"),
    "wiki_item": "🧰 <b>Item විස්තර</b>\n\nItem එකේ නම යවන්න. උදා: <code>Heat Sink</code>, <code>Aegis Shield</code>",
    "wiki_compare": ("⚔️ <b>Compare</b>\n\nසසඳන්න ඕන දෙක යවන්න. උදා:\n"
                     "<code>ADA Refactor vs Jarvis Virus</code>\n<code>Heat Sink vs Multi-Hack</code>"),
    "wiki_search": "🔍 <b>Wiki සෙවීම</b>\n\nසොයන්න ඕන වචනය යවන්න (English Ingress term එකක් හොඳයි). උදා: <code>Jarvis</code>",
}
PROMPT_MODES = {"wiki_ask": "ask", "wiki_item": "item", "wiki_compare": "compare"}


def search_screen(query: str):
    hits = store.search(query, limit=8)
    q = esc(query)
    if not hits:
        store.log_failed(query, "wsearch: no hits")
        lines = [f"🔍 <b>{q}</b>", "", "😕 Wiki එකේ ගැළපෙන දෙයක් හමු වුණේ නැහැ."]
        rows = []
        tips = store.suggest_titles(query)
        if tips:
            lines.append("\nඔබ අදහස් කළේ මේ එකක්ද?")
            for t in tips[:4]:
                page = store.get_page(t)
                if page:
                    rows.append([(f"📖 {t}", f"wpg:{page['pageid']}")])
        if wiki_qa.is_enabled():
            lines.append("\n<i>සිංහලෙන් ඇහුවා නම් ❓ ප්‍රශ්නයක් ලෙස අහන්න — AI එක Ingress term එක හොයාගන්නවා.</i>")
            rows.append([("❓ ප්‍රශ්නයක් ලෙස අහන්න", "wq")])
        return "\n".join(lines), kb(*rows, [ASK_AGAIN, WIKI_HOME])
    lines = [f"🔍 <b>{q}</b> — Wiki ප්‍රතිඵල", ""]
    rows, seen = [], set()
    for h in hits:
        if h["pageid"] in seen:
            continue
        seen.add(h["pageid"])
        snippet = " ".join(h["body"].split())[:180]
        section = f" › {esc(h['section'])}" if h["section"] and h["section"] != h["title"] else ""
        lines.append(f"📖 <b>{esc(h['title'])}</b>{section}\n<i>{esc(snippet)}…</i>\n")
        rows.append([(f"🇱🇰 {h['title']} – සිංහලෙන්", f"wpg:{h['pageid']}")])
    lines.append("<i>පිටුවක් සිංහලෙන් පැහැදිලි කරගන්න පහළ button එක ඔබන්න.</i>")
    return "\n".join(lines), kb(*rows[:6], [ASK_AGAIN, WIKI_HOME])


def related_screen(pageid: int):
    page = store.get_page_by_id(pageid)
    if page is None:
        return "😕 ඒ පිටුව දැන් Wiki එකේ නැහැ.", kb([ASK_AGAIN, WIKI_HOME])
    rel = store.related(pageid)
    if not rel:
        return (f"🔍 <b>{esc(page['title'])}</b>\n\nසම්බන්ධ මාතෘකා හමු වුණේ නැහැ.",
                kb([("📖 Read Full Wiki", None, page["url"])], [ASK_AGAIN, WIKI_HOME]))
    lines = [f"🔍 <b>{esc(page['title'])}</b> — සම්බන්ධ මාතෘකා", "", "එකක් තෝරන්න:"]
    rows = [[(f"📖 {r['title']}", f"wpg:{r['pageid']}")] for r in rel]
    return "\n".join(lines), kb(*rows, [ASK_AGAIN, WIKI_HOME])


def answer_markup(sources):
    rows = []
    if sources:
        first = store.get_page(sources[0][0])
        row = [("📖 Read Full Wiki", None, sources[0][1])]
        if first:
            row.append(("🔍 Related Topics", f"wrel:{first['pageid']}"))
        rows.append(row)
    rows.append([("⚔️ Compare Items", "wcmp"), ASK_AGAIN])
    return kb(*rows)


def sources_html(sources) -> str:
    links = ", ".join(f'<a href="{esc(url)}">{esc(title)}</a>' for title, url in sources)
    return f"📚 <b>මූලාශ්‍රය:</b> {links}\n<i>Ingress Wiki (CC BY-SA)</i>"


CATEGORY_EMOJI = {
    "Items and Inventory": "🧰", "Portal Information": "🌀", "Links and Control Fields": "🔗",
    "Missions and Medals": "🎖", "Events": "🎉", "Game Updates": "🆕", "Factions and Story": "⚔️",
    "Advanced Gameplay": "🧠", "Game Basics": "📘",
}
FIELD_EMOJI = {
    "category": "🏷", "rarity": "💎", "rarity(s)": "💎", "xm cost": "⚡", "recycle value": "♻️",
    "introduced": "📅", "level": "🎚", "range": "📏", "damage": "💥", "faction": "⚔️",
}
SKIP_SECTIONS = {"history", "gallery", "trivia", "references", "see also", "external links",
                 "notes", "videos", "images"}
FIELD_RE = re.compile(r"^([A-Z][A-Za-z0-9 ()'/-]{1,30}):\s*(.*)$")


def _table_html(rows):
    """[[cells]] -> quote lines, header bold. Only tidy tables (2-5 columns)."""
    width = len(rows[0])
    if len(rows) < 3 or not 2 <= width <= 5 or any(len(r) != width for r in rows):
        return None
    out = [" · ".join(f"<b>{esc(c)}</b>" for c in rows[0])]
    out += ["▫️ " + " · ".join(esc(c) for c in r) for r in rows[1:7]]
    return "\n".join(out)


def _section_html(body: str, limit: int = 450, tables_only: bool = False):
    """Prose, bullets and tidy tables of a section, shortened."""
    lines, out, used, table = body.split("\n"), [], 0, []

    def flush_table():
        if len(table) >= 2:
            t = _table_html(table)
            if t:
                out.append(t)
        table.clear()

    for line in lines:
        line = line.strip()
        if " | " in line:
            table.append([c.strip() for c in line.split("|")])
            continue
        flush_table()
        if not line or used > limit or tables_only:
            continue
        if line.startswith("• "):
            out.append("• " + esc(line[2:180]))
        elif len(line) > 25:
            text = line if len(line) <= 320 else line[:320].rsplit(" ", 1)[0] + "…"
            out.append(esc(text))
        else:
            continue
        used += len(line)
    flush_table()
    return "\n".join(out)


def lead_facts(page):
    """(tagline, facts box html or '', lead prose lines, lead tables html, sections)."""
    sections = wiki.split_sections(page["content"])
    lead_heading, lead = sections[0] if sections else ("", "")
    fields, prose, tagline, pending, seen_table = [], [], "", None, False
    for line in lead.split("\n"):
        line = line.strip()
        if not line:
            continue
        if " | " in line:
            seen_table = True
        m = FIELD_RE.match(line)
        if m and " | " not in line:
            key, value = m[1], m[2].strip()
            if key.lower() == "description":
                tagline = value
            elif key.lower() in ("main article", "see also", "main articles"):
                pass
            elif any(f[0] == key for f in fields):
                pending = None  # levelled items repeat the box per level; keep the first
            elif value:
                fields.append([key, value])
            else:
                fields.append([key, ""])
                pending = fields[-1]
            continue
        if line.startswith("• ") and pending is not None:
            pending[1] = (pending[1] + " · " if pending[1] else "") + line[2:]
            continue
        pending = None
        if (not seen_table and " | " not in line and len(line) > 40 and not line.startswith("• ")
                and sum(len(p) for p in prose) < 450):
            prose.append(line)
    facts = [f"{FIELD_EMOJI.get(k.lower(), '▪️')} <b>{esc(k)}:</b> {esc(v)}" for k, v in fields if v][:7]
    facts_html = "<blockquote>" + "\n".join(facts) + "</blockquote>" if facts else ""
    return tagline, facts_html, prose, _section_html(lead, tables_only=True), sections


def page_card(page) -> str:
    """A tidy overview of a wiki page: facts box, summary, a few folded sections."""
    emoji = CATEGORY_EMOJI.get(page["category"], "📖")
    tagline, facts_html, prose, lead_tables, sections = lead_facts(page)
    parts = [f"{emoji} <b>{esc(page['title'].upper())}</b>"]
    if tagline:
        parts.append(f"<i>{esc(tagline)}</i>")
    if facts_html:
        parts.append(facts_html)
    if prose:
        parts.append("📝 " + "\n\n".join(esc(p) for p in prose))
    if lead_tables and "<b>" in lead_tables:
        parts.append("📊 <b>ප්‍රධාන දත්ත</b>\n<blockquote expandable>" + lead_tables + "</blockquote>")

    shown = 0
    for heading, body in sections[1:]:
        if heading.lower() in SKIP_SECTIONS or shown >= 3:
            continue
        html_body = _section_html(body)
        if len(html_body) < 80:  # nothing useful (e.g. a lone "The following medals:")
            continue
        parts.append(f"🔹 <b>{esc(heading)}</b>\n<blockquote expandable>{html_body}</blockquote>")
        shown += 1
    parts.append(f'🔗 <a href="{esc(page["url"])}">Ingress Wiki එකේ සම්පූර්ණ පිටුව</a>')
    text = "\n\n".join(parts)
    return text if len(text) < 3600 else text[:3500].rsplit("\n\n", 1)[0] + \
        f'\n\n🔗 <a href="{esc(page["url"])}">සම්පූර්ණ පිටුව</a>'


def si_card(entry, page) -> str:
    """Static Sinhala page card, with the wiki's English facts box and data table."""
    parts = [f"<b>{esc(entry['h'])}</b>", f"<i>{esc(page['title'])} · Ingress Wiki</i>", esc(entry["s"])]
    _tagline, facts_html, _prose, lead_tables, _sections = lead_facts(page)
    if facts_html:
        parts.append(facts_html)
    if entry.get("p"):
        parts.append("✅ <b>වැදගත් කරුණු</b>\n" + "\n".join(f"• {esc(x)}" for x in entry["p"]))
    if lead_tables and "<b>" in lead_tables:
        parts.append("📊 <b>Wiki දත්ත (English)</b>\n<blockquote expandable>" + lead_tables + "</blockquote>")
    for heading, text in entry.get("sec", []):
        parts.append(f"🔹 <b>{esc(heading)}</b>\n<blockquote expandable>{esc(text)}</blockquote>")
    parts.append(sources_html([(page["title"], page["url"])]))
    if entry.get("revid") and page["revid"] and page["revid"] != entry["revid"]:
        parts.append("⚠️ <i>මේ සිංහල විස්තරය ලියූ පසු Wiki පිටුව වෙනස් වෙලා. නවතම තොරතුරු සඳහා Wiki link එක බලන්න.</i>")
    return "\n\n".join(parts)


def si_compare_card(a, b) -> str:
    """Two Sinhala entries side by side (summary + key points)."""
    (ea, pa), (eb, pb) = a, b
    parts = [f"⚔️ <b>{esc(pa['title'])} සහ {esc(pb['title'])} සසඳමු</b>"]
    for e, p in ((ea, pa), (eb, pb)):
        block = f"<b>{esc(e['h'])}</b>\n{esc(e['s'])}"
        if e.get("p"):
            block += "\n" + "\n".join(f"• {esc(x)}" for x in e["p"][:5])
        parts.append(f"<blockquote>{block}</blockquote>")
    parts.append(sources_html([(pa["title"], pa["url"]), (pb["title"], pb["url"])]))
    return "\n\n".join(parts)


async def static_answer(update: Update, question: str, mode: str, placeholder) -> bool:
    """Answer from the stored Sinhala content. False if there's none for this question."""
    if mode == "compare":
        pair = wiki_si.split_compare(question)
        if pair:
            found = []
            for part in pair:
                title = wiki_si.find_title(part, store)
                entry, page = wiki_si.get(title) if title else None, store.get_page(title) if title else None
                if entry and page:
                    found.append((entry, page))
            if len(found) == 2 and found[0][1]["pageid"] != found[1][1]["pageid"]:
                await send(update, si_compare_card(*found), kb(
                    [("📖 " + found[0][1]["title"], None, found[0][1]["url"]),
                     ("📖 " + found[1][1]["title"], None, found[1][1]["url"])],
                    [ASK_AGAIN, WIKI_HOME]), edit_message=placeholder)
                return True
    title = wiki_si.find_title(question, store)
    entry = wiki_si.get(title) if title else None
    page = store.get_page(title) if title else None
    if not entry or page is None:
        return False
    if await send_images(update, page):
        await _drop(placeholder)
        placeholder = None
    is_item = any(p["pageid"] == page["pageid"] for _, rows in item_groups() for p in rows)
    await send(update, si_card(entry, page), kb(
        [("📖 Read Full Wiki", None, page["url"]), ("🔍 Related Topics", f"wrel:{page['pageid']}")],
        [("⚔️ Compare Items", "wcmp"), ASK_AGAIN],
        [("⬅️ Items", "wits"), WIKI_HOME] if is_item else []), edit_message=placeholder)
    return True


def best_page(question: str):
    """The page a question is most likely about."""
    page = store.get_page(question)
    if page is not None:
        return page
    hits = store.search(question, limit=1)
    return store.get_page_by_id(hits[0]["pageid"]) if hits else None


def fallback_text(question: str, reason: str) -> str:
    """No AI available: a tidy English card of the best matching wiki page."""
    note = ("ℹ️ <i>සිංහල AI පිළිතුරු දැනට ක්‍රියාත්මක නැහැ"
            + (" (admin තවම සකසා නැහැ)" if reason == "no API key" else " (සේවාව තාවකාලිකව නැහැ)")
            + " — Wiki තොරතුරු English වලින් පෙන්වනවා.</i>")
    page = best_page(question)
    if page is None:
        return note + "\n\n😕 ඔබේ ප්‍රශ්නයට ගැළපෙන Wiki තොරතුරු හමු වුණේ නැහැ. English Ingress term " \
                      "එකකින් <code>/wsearch</code> කරලා බලන්න."
    return page_card(page) + "\n\n" + note


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def rate_ok(uid: int) -> bool:
    if is_admin(uid):
        return True
    now = time.monotonic()
    times = _requests[uid]
    while times and now - times[0] > 86400:
        times.popleft()
    if len(times) >= PER_DAY or sum(1 for t in times if now - t < 60) >= PER_MINUTE:
        return False
    times.append(now)
    return True


def clean_question(text: str) -> str:
    text = " ".join((text or "").replace("\x00", " ").split())
    return text[:MAX_QUESTION_LEN]


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str, mode: str = "ask"):
    """Answer a wiki question in Sinhala (or explain why not). Never raises."""
    uid = update.effective_user.id
    question = clean_question(question)
    if len(question) < 2:
        await send(update, PROMPTS["wiki_ask"], kb([WIKI_HOME]))
        context.user_data["awaiting"] = {"kind": "wiki_ask"}
        return
    if store.page_count() == 0:
        await send(update, "⏳ Wiki දැනුම් ගබඩාව තවම සකස් වෙමින් පවතිනවා. විනාඩි කිහිපයකින් නැවත උත්සාහ කරන්න.",
                   kb([WIKI_HOME]))
        return
    if not rate_ok(uid):
        await send(update, "⏱ ප්‍රශ්න ගොඩක් එකපාර ආවා. විනාඩියක් ඉඳලා නැවත අහන්න.", kb([WIKI_HOME]))
        return

    context.user_data["wiki_last_q"] = question
    placeholder = None
    try:
        placeholder = await update.effective_message.reply_text("🔎 Ingress Wiki එකේ සොයමින්…")
        await update.effective_chat.send_action(ChatAction.TYPING)
    except TelegramError:
        pass

    try:
        result = await assistant.answer(question, mode)
    except wiki_qa.AIUnavailable as exc:
        if await static_answer(update, question, mode, placeholder):
            return
        page = best_page(question)
        text = fallback_text(question, str(exc))
        rows = [[("📖 Read Full Wiki", None, page["url"]), ("🔍 Related Topics", f"wrel:{page['pageid']}")]] if page else []
        if page is not None and await send_images(update, page):
            await _drop(placeholder)
            placeholder = None
        await send(update, text, kb(*rows, [("🔍 Wiki සෙවීම", "wsrch"), ASK_AGAIN]), edit_message=placeholder)
        return
    except Exception:  # noqa: BLE001 - never let the wiki break the bot
        logger.exception("Wiki answer failed")
        await send(update, "😬 සමාවෙන්න, පිළිතුර සකස් කිරීමේදී දෝෂයක් ආවා. ටිකකින් නැවත උත්සාහ කරන්න.",
                   kb([ASK_AGAIN, WIKI_HOME]), edit_message=placeholder)
        return

    if not result["found"]:
        lines = [result["text"] or "😕 මේ ප්‍රශ්නයට අදාළ තොරතුරු Ingress Wiki දැනුම් ගබඩාවේ හමු වුණේ නැහැ."]
        rows = []
        for t in store.suggest_titles(question, n=3):
            page = store.get_page(t)
            if page:
                rows.append([(f"📖 {t}", f"wpg:{page['pageid']}")])
        await send(update, "\n".join(lines), kb(*rows, [ASK_AGAIN, WIKI_HOME]), edit_message=placeholder)
        return

    text = result["text"]
    if result["sources"]:
        text += "\n\n" + sources_html(result["sources"])
        if await send_images(update, store.get_page(result["sources"][0][0])):
            await _drop(placeholder)
            placeholder = None
    await send(update, text, answer_markup(result["sources"]), edit_message=placeholder)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _args(context) -> str:
    return " ".join(context.args or []).strip()


async def wiki_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting", None)
    await send(update, *intro_screen())


async def _ask_or_prompt(update, context, kind):
    question = _args(context)
    if not question:
        context.user_data["awaiting"] = {"kind": kind}
        await send(update, PROMPTS[kind], kb([("✖️ Cancel", "wiki")]))
        return
    context.user_data.pop("awaiting", None)
    await ask(update, context, question, PROMPT_MODES[kind])


async def ask_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ask_or_prompt(update, context, "wiki_ask")


async def item_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _args(context):
        context.user_data["awaiting"] = {"kind": "wiki_item"}
        await send(update, *items_screen())
        return
    await _ask_or_prompt(update, context, "wiki_item")


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _ask_or_prompt(update, context, "wiki_compare")


async def portal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("awaiting", None)
    await ask(update, context, _args(context) or "Portals in Ingress", "portal")


async def wsearch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = clean_question(_args(context))
    if not query:
        context.user_data["awaiting"] = {"kind": "wiki_search"}
        await send(update, PROMPTS["wiki_search"], kb([("✖️ Cancel", "wiki")]))
        return
    context.user_data.pop("awaiting", None)
    context.user_data["wiki_last_q"] = query
    await send(update, *search_screen(query))


async def updatewiki_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await send(update, "⛔ මෙම command එක bot admin ලාට පමණයි.")
        return
    force = _args(context).lower() == "full"
    msg = await update.effective_message.reply_text(
        "🔄 Wiki යාවත්කාලීන කරමින්… " + ("(සියලු පිටු)" if force else "(වෙනස් වූ පිටු පමණක්)"))

    async def progress(i, n):
        try:
            await msg.edit_text(f"🔄 Wiki යාවත්කාලීන කරමින්… {i}/{n}")
        except TelegramError:
            pass

    try:
        result = await wiki.update_knowledge(store, force=force, progress=progress)
    except RuntimeError:
        await msg.edit_text("⏳ යාවත්කාලීන කිරීමක් දැනටමත් සිදු වෙමින් පවතිනවා.")
        return
    except wiki.WikiUnavailable as exc:
        logger.warning("Manual wiki update failed: %s", exc)
        await msg.edit_text("😕 Ingress Wiki එකට දැන් සම්බන්ධ වෙන්න බැහැ. පසුව නැවත උත්සාහ කරන්න.")
        return
    except Exception:  # noqa: BLE001
        logger.exception("Manual wiki update crashed")
        await msg.edit_text("😬 යාවත්කාලීන කිරීමේදී දෝෂයක් ආවා. Log එක බලන්න.")
        return
    await msg.edit_text(
        "✅ Wiki යාවත්කාලීන කළා\n"
        f"➕ අලුත්: {result['new']} · 🔄 වෙනස්: {result['changed']} · 🗑 ඉවත් කළ: {result['deleted']}\n"
        f"✔️ වෙනස් නොවූ: {result['unchanged']} · 🔗 Aliases: {result['aliases']} · ⚠️ අසාර්ථක: {result['failed']}\n"
        f"📖 දැන් පිටු {store.page_count()}ක්"
    )


COMMANDS = {
    "wiki": wiki_cmd,
    "ask": ask_cmd,
    "item": item_cmd,
    "compare": compare_cmd,
    "portal": portal_cmd,
    "wsearch": wsearch_cmd,
    "updatewiki": updatewiki_cmd,
}

BOT_COMMANDS = [
    ("wiki", "📚 Ingress Wiki සහායකයා (සිංහල)"),
    ("ask", "❓ Ingress ප්‍රශ්නයක් අහන්න"),
    ("item", "🧰 Item එකක විස්තර"),
    ("compare", "⚔️ Items දෙකක් සසඳන්න"),
    ("portal", "🌀 Portal ගැන විස්තර"),
    ("wsearch", "🔍 Wiki එකේ සොයන්න"),
]

HELP_TOPIC = ("wiki", "📚 Ingress Wiki (සිංහල)", (
    "Ingress ගැන ප්‍රශ්න සිංහලෙන්, English වලින් හෝ මිශ්‍රව අහන්න — Ingress Wiki එක අනුව සිංහලෙන් "
    "පිළිතුරු සහ මූලාශ්‍ර link එක ලැබෙනවා.\n\n"
    "/wiki – Wiki සහායකයා\n"
    "/ask <i>ප්‍රශ්නය</i> – ඕනෑම Ingress ප්‍රශ්නයක්\n"
    "/item <i>item</i> – item එකක විස්තර\n"
    "/compare <i>A vs B</i> – සසඳන්න\n"
    "/portal – Portal ගැන\n"
    "/wsearch <i>වචනය</i> – Wiki එකේ සොයන්න\n\n"
    "<i>/search තවමත් inventory එකේ items සොයනවා.</i>"
))


# ---------------------------------------------------------------------------
# Buttons and typed answers (called from bot.py)
# ---------------------------------------------------------------------------

BUTTON_ACTIONS = {"wiki", "wask", "witem", "wcmp", "wsrch", "wport", "wq", "wrel", "wpg", "wits", "witg"}


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, args) -> None:
    query = update.callback_query
    try:
        await query.answer()
    except BadRequest:
        pass
    ud = context.user_data
    try:
        if action == "wiki":
            ud.pop("awaiting", None)
            await send(update, *intro_screen(), edit_message=query.message)
        elif action in ("wask", "witem", "wcmp", "wsrch"):
            kind = {"wask": "wiki_ask", "witem": "wiki_item", "wcmp": "wiki_compare", "wsrch": "wiki_search"}[action]
            ud["awaiting"] = {"kind": kind}
            await send(update, PROMPTS[kind], kb([("✖️ Cancel", "wiki")]))
        elif action == "wits":
            ud["awaiting"] = {"kind": "wiki_item"}
            await send(update, *items_screen(), edit_message=query.message)
        elif action == "witg":
            ud["awaiting"] = {"kind": "wiki_item"}
            await send(update, *item_group_screen(int(args[0])), edit_message=query.message)
        elif action == "wport":
            await ask(update, context, "Portals in Ingress", "portal")
        elif action == "wq":
            question = ud.get("wiki_last_q")
            if not question:
                ud["awaiting"] = {"kind": "wiki_ask"}
                await send(update, PROMPTS["wiki_ask"], kb([("✖️ Cancel", "wiki")]))
            else:
                await ask(update, context, question, "ask")
        elif action == "wrel":
            await send(update, *related_screen(int(args[0])))
        elif action == "wpg":
            page = store.get_page_by_id(int(args[0]))
            if page is None:
                await send(update, "😕 ඒ පිටුව දැන් Wiki එකේ නැහැ.", kb([ASK_AGAIN, WIKI_HOME]))
            else:
                mode = "item" if page["category"] == "Items and Inventory" else "ask"
                await ask(update, context, page["title"] if mode == "item" else f"Explain {page['title']}", mode)
    except (ValueError, IndexError):
        await send(update, *intro_screen())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, text: str) -> bool:
    """Typed answer to a wiki prompt. Returns False if `kind` isn't a wiki prompt."""
    if kind not in PROMPTS:
        return False
    context.user_data.pop("awaiting", None)
    if kind == "wiki_search":
        query = clean_question(text)
        context.user_data["wiki_last_q"] = query
        await send(update, *search_screen(query))
    else:
        await ask(update, context, text, PROMPT_MODES[kind])
    return True


def offer_row(ud, text: str):
    """A '📚 Ask the wiki' button row for messages the inventory bot didn't understand."""
    if store is None or store.page_count() == 0:
        return []
    ud["wiki_last_q"] = clean_question(text)
    return [("📚 Ingress Wiki එකෙන් අහන්න", "wq")]


def looks_like_question(text: str) -> bool:
    return any("඀" <= ch <= "෿" for ch in text) or text.rstrip().endswith("?")


# ---------------------------------------------------------------------------
# Background updates
# ---------------------------------------------------------------------------

async def update_loop():
    """Check for wiki changes once every UPDATE_HOURS (only changed pages download)."""
    await asyncio.sleep(90)
    while True:
        try:
            last = store.get_meta("last_update")
            due = last is None or datetime.now() - datetime.strptime(last, wiki.TS_FORMAT) > timedelta(hours=UPDATE_HOURS)
            if due:
                await wiki.update_knowledge(store)
        except RuntimeError:
            pass  # a manual update is running
        except wiki.WikiUnavailable as exc:
            logger.warning("Scheduled wiki update skipped: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("Scheduled wiki update failed")
        await asyncio.sleep(3600)


def start_background():
    global _update_task
    _update_task = asyncio.get_running_loop().create_task(update_loop())


def stop_background():
    if _update_task:
        _update_task.cancel()
