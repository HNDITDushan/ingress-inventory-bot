# Ingress Prime Inventory Bot

A Telegram bot to track inventory across multiple Ingress Prime accounts.
Each Telegram user gets their own isolated set of accounts and items,
stored locally in SQLite (`inventory.db`, created automatically next to `bot.py`).

## Setup

```bash
pip install -r requirements.txt
```

Put your bot token in the `.env` file in this folder (already created for you):

```
TELEGRAM_BOT_TOKEN=your_bot_token_here
```

`bot.py` loads it automatically via `python-dotenv` — no need to `export`/`set`
it manually each session. Don't commit `.env` to git (it holds a secret);
`.env.example` is the template that's safe to commit.

Then just run:

```bash
python bot.py
```

The bot uses long polling, so no public URL/webhook is needed — just keep
`bot.py` running (e.g. in a terminal, a scheduled task, or a small VPS).

> Since the token was pasted into a chat session, consider regenerating it via
> **@BotFather → /revoke** once you've copied the new one into your environment.

## Item catalog

Items are a fixed whitelist (not free text), grouped into categories:

- **Resonators**: R1–R8
- **XMP Bursters**: X1–X8
- **Ultra Strikes**: U1–U8
- **Portal Shields**: Common / Rare / Very Rare / Aegis
- **Mods**: Force Amp, Turret
- **Power Cubes**: P1–P8, Hyper Cube
- **Heat Sinks**: Common / Rare / Very Rare
- **Multi-Hacks**: Common / Rare / Very Rare
- **Link Amps**: Common / Rare / Soft Bank Ultra Link
- **Ops & Keys**: ITO +, ITO -, Keys
- **Viruses**: ADA Refactor, JARVIS Virus

Send `/items` any time to see the full list from the bot itself.

## Usage (in Telegram, talk to your bot)

Send `/start` (or `/menu`). Everything can be done with buttons:

- **Quick buttons** stay at the bottom of the chat: 📦 Inventory, 🔍 Search,
  📊 Stats and 🏠 Menu work from anywhere.
- **↩️ Undo** appears after every inventory change (button taps, typed lines,
  sync and import). One tap takes the last change back; a run of taps on the
  same item is undone together.
- **Did you mean…?** If an item name is misspelt (`vrsp`, `hypercub`), the bot
  suggests the closest item and offers a button to open it.
- **❓ Help** is split into short topics. Rarely used tools (sync, import,
  manage accounts, item names) are under **🧰 More tools** in the menu.

- **📥 Add items / 📤 Remove items**: pick a category, then an item, then tap
  `+1 +5 +10 +25 +50 +100` (or `−…`, `🗑 All`), or type a number. Type `=20` to
  set an exact count.
- **📦 My inventory / 🗂 All accounts**: grouped by category, with combined
  totals across accounts.
- **🔄 Switch account / ➕ New account / ⚙️ Manage accounts**: view, rename,
  describe or delete accounts. A description is a short note such as
  "Ambalangoda keys" and shows on the account and inventory screens.
- **🔍 Search all accounts**: send an item (`R8`) to see which accounts hold it,
  how many each, and the total. A wider word (`shield`, `res`) covers a whole
  family, with totals per account and per item. Account names and descriptions
  are searched too. `/search R8` does the same.

- **🧾 Sync full inventory**: send your complete list (one item per line) and the
  account is set to match it exactly. Items you leave out become 0. The bot
  shows your current list ready to copy and edit, and previews every change
  before applying it. `/sync` does the same; `/sync` followed by a list on the
  next lines goes straight to the preview.

- **📥 Import all accounts**: paste every account in one message. Each account
  starts with its name in square brackets, followed by one item per line:

  ```
  [AgentOne]
    - R8: 120
    - X8: 300

  [AgentTwo]
    - Hyper Cube: 14
  ```

  Missing accounts are created. Existing ones are matched by name (or by the agent
  name before a `(`) and set to match the list exactly. Accounts you don't list
  are left alone. A preview shows what will be created or changed. Pasting text
  in this format at any time opens the preview; so does `/import`.
- **📊 Stats & goals**: in Ingress, copy your **ALL TIME** agent stats and paste
  them into the chat at any time (the paste starts with `Time Span`). Each paste
  is saved, and the bot shows what grew since the last one. Tap ➕ New goal to
  set a target for any stat:
  - ☀️ Daily / 📅 Weekly (Mon–Sun) / 🗓 Monthly / 🎆 Yearly: gain an amount in
    that period, e.g. 500 Hacks a day.
  - 🏔 Lifetime: reach a total, e.g. 200M Lifetime AP.

  When a paste puts a goal at 80% or more you get a 🔥 "almost there" note, and a
  🏆 when it's done. Unfinished goals get one ⏰ reminder near the end of their
  period (3 hours before the day ends, 1 day before the week ends, 3 days before
  the month ends, 14 days before the year ends). Progress is measured from the
  last stats pasted before the period started, so paste at least at the start
  and end of each day or week for accurate numbers.
- **🏁 Leaderboards** (in 📊 Stats & goals): pick a stat and paste a leaderboard,
  e.g. Glyph Hack Points from the-grid.blue, one agent per line (`cristy92 163,477`;
  ranks, commas and text copied from the page all work). The bot ranks you by your
  latest stats, shows who's next and the gap, and 🎯 sets a lifetime goal to pass
  them. When a stats paste moves you past someone you get a 🚀 note and a button
  to chase the next agent. Lifetime goals now show progress from where you were
  when you set them.
- **📎 Upload Excel** (on the Leaderboards screen): send the-grid.blue's Excel
  export (.xlsx) and every leaderboard in it is updated at once, after a preview.
  "Pass X" goals move on by themselves: when you pass X the goal becomes "pass"
  the next agent up, and a newer list keeps the target current.

Buttons never expire. Old menus keep working, even after the bot restarts.

### Typing shortcuts

Just send a message to change the active account. Several lines at once work too:

```
R8 10        -> add 10 R8
-3 VRPS      -> remove 3 Very Rare Portal Shields
Keys =120    -> set Keys to exactly 120
```

Common abbreviations are understood (`res8`, `xmp8`, `us8`, `pc8`, `CPS`, `RPS`,
`VRPS`, `Aegis`, `FA`, `HC`, `VRHS`, `VRMH`, `SBUL`, `ITO+`, `key`…), in any case.
Sending just an item name (e.g. `R8`) opens its +/− screen.

### Commands

| Command | Effect |
|---|---|
| `/menu` | Main menu |
| `/newaccount [name]` | Create an account (it becomes the active one) |
| `/use [name]` | Switch the active account (no name → pick from a list) |
| `/accounts` | Manage accounts (view, rename, delete) |
| `/add [qty item]` | Quick add, e.g. `/add 5 U3`; no args → tap menu |
| `/remove [qty item]` | Quick remove; no args → tap menu |
| `/list [name]` | Show the active (or named) account |
| `/listall` | Show every account plus combined totals |
| `/sync [list]` | Replace the active account's inventory with a full list (preview first) |
| `/import [paste]` | Create/update many accounts at once from `[Name]` blocks (preview first) |
| `/search [text]` | Find an item, family, account name or description across all accounts |
| `/describe [text]` | Set the active account's description |
| `/stats [paste]` | Show your agent stats, or save the stats pasted after it |
| `/goals` | Show your goals and progress |
| `/items` | Item names and shortcuts |
| `/delaccount [name]` | Delete an account (asks for confirmation) |
| `/cancel` | Stop what you're doing |

## 🔄 Sync stats from the-grid

If you upload your stats to [the-grid.blue](https://the-grid.blue) or the old
[the-grid.org](https://the-grid.org), the bot can pull them all in, so you don't
need to paste each one.

1. Open **📊 Stats & goals → 🌐 Sync the-grid**.
2. Tap **🔑 Add .blue key** and/or **🔑 Add .org key**, and send the key as a
   message. For the-grid.org you can paste the whole export link; the bot takes
   the `key=` part. The bot checks the key, saves it for your Telegram account,
   and deletes your message.
3. Pick:
   - **🔄 New uploads**: everything since your latest stats in the bot.
   - **📜 Full history**: every upload, from every site you have a key for.

| Site | API | Notes |
|---|---|---|
| the-grid.blue | `api_public.php?action=uploads&from=…&to=…` with `X-API-Key` | 100 uploads per call; the bot pages back through older days |
| the-grid.org | `r/export/?key=…&t0=…&t1=…&dir=asc` | Fetched a year at a time. No agent name or level, so uploads go to your agent and Level is carried over from your earlier stats |

Every user adds their own keys. **Change** and **Remove** buttons are on the
same screen. Keys are stored in `inventory.db`, which is never committed. For
admins without a .blue key in the bot, `STATS_API_KEY` in `.env` is used as a
fallback.

Uploads already saved are skipped, whether they're at the same time or have the
same Lifetime AP within 15 minutes. the-grid.org stamps uploads with the upload
time, which differs slightly from the paste's own time. The bot waits 1 second
between calls.

## Notes

- Quantities accumulate: adding U3 twice (5, then 3) results in 8.
- Removing more than you have stops at zero.
- Only items from the catalog are accepted; unrecognized names are listed back
  to you with a pointer to `/items`.
- Button/typing state is kept in `bot_state.pickle` next to `bot.py`.
- All data is per Telegram user ID — if multiple people use the same bot,
  they won't see each other's accounts/items.
- To run this continuously, use a process manager (systemd, pm2, NSSM on
  Windows, Task Scheduler, or a Docker container with `restart: always`).

## 📚 Sinhala Ingress Wiki assistant

Any Telegram user can ask questions about Ingress Prime in Sinhala, English or
a mix, and get an easy Sinhala answer built only from the
[Ingress Wiki](https://ingress.fandom.com/wiki/Ingress), with the source page link.
The inventory, stats, goals and leaderboard features are unchanged.

### Commands

| Command | What it does |
|---|---|
| `/wiki` | Introduction and buttons for everything below |
| `/ask <question>` | Any Ingress question, e.g. `/ask ADA කියන්නේ මොකක්ද?` |
| `/item <item>` | Structured item details (category, rarity, use, restrictions, tips — only what the wiki supports) |
| `/compare <A> vs <B>` | Side-by-side comparison, e.g. `/compare Heat Sink vs Multi-Hack` |
| `/portal [topic]` | Portal mechanics (overview without a topic) |
| `/wsearch <term>` | Search the wiki knowledge base (no AI needed) |
| `/updatewiki [full]` | **Admin only.** Download new/changed wiki pages now (`full` re-downloads all) |

`/search` still searches your inventory (it was already taken), so wiki search is `/wsearch`.
Messages the inventory bot doesn't understand get a "📚 Ask Ingress Wiki" button;
Sinhala messages and messages ending in `?` are offered to the wiki first.
Answers have buttons: 📖 Read Full Wiki · 🔍 Related Topics · ⚔️ Compare Items · ❓ Ask another.

### How it works

- `wiki.py` — fetches pages through the official MediaWiki API
  (`https://ingress.fandom.com/api.php`), at most 1 request/second with timeouts
  and retries, cleans the HTML to text and stores it in **`wiki.db`** (SQLite,
  separate from `inventory.db`): pages (title, URL, revision id, categories,
  our category, content, linked terms, status, fetched time), redirect aliases,
  an FTS5 full-text index of page sections, an answer cache and a log of failed
  searches. Updates first fetch every article's latest revision id (a few
  requests) and download only new/changed pages; deleted pages are marked and
  removed from search.
- `wiki_qa.py` — retrieval-augmented answers with Claude (`claude-opus-5-5`,
  effort `low`, server-side refusal fallback on). Claude searches/reads the local
  knowledge base through two tools, so Sinhala questions are mapped to English
  Ingress terms before searching. The prompt forbids facts that aren't in the
  retrieved wiki text; unknown questions get a clear "not in the wiki" answer.
  Answers are cached per question until the knowledge base changes (max 14 days).
- `wiki_bot.py` — Telegram commands, buttons, rate limiting (5 questions/minute,
  60/day per user; admins exempt), safe splitting of long answers, the daily
  background update, and friendly Sinhala errors. Nothing in it can crash
  the inventory bot.

Wiki text is CC BY-SA (Fandom); every answer links its source pages.

### Sinhala content without AI (`wiki_si/`)

`wiki_si/*.json` holds hand-written Sinhala explanations for ~210 wiki pages
(every item, portal, link/field, basics, mechanics, faction, story, medal and
event page; old update notes and misc pages are skipped). Each entry has the
exact wiki title, the wiki revision it was written from, Sinhala keywords
(so Sinhala questions find it), a heading, a summary, key points and section
summaries. Without `ANTHROPIC_API_KEY` the bot answers from these: pictures,
the Sinhala card, the wiki's English facts box and data table, and the source
link. `/compare` shows two pages side by side. If the wiki page later gets a
newer revision, the card adds a note that the wiki has changed since.
With an API key, Claude's answers are used instead (and these files are not needed).

To edit or add a page, change or add an entry in a `wiki_si/*.json` file and
restart the bot.

### Configuration (`.env`)

| Variable | Needed | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | for Sinhala answers | Claude API key from https://console.anthropic.com. Without it, `/ask` shows the matching wiki text in English. |
| `ADMIN_IDS` | for `/updatewiki` | Comma-separated Telegram user IDs allowed to run admin commands |
| `WIKI_DB_PATH` | no | Knowledge base file (default `wiki.db` next to `bot.py`) |
| `WIKI_UPDATE_HOURS` | no | How often to check the wiki for changes (default 24) |
| `WIKI_AI_MODEL` / `WIKI_AI_EFFORT` | no | Defaults `claude-opus-5-5` / `low` |
| `WIKI_PER_MINUTE` / `WIKI_PER_DAY` | no | Per-user question limits (default 5 / 60) |
| `WIKI_AI_CONCURRENCY` | no | Answers generated at the same time (default 3) |

### Setup

```bash
venv/bin/pip install -r requirements.txt
```

Add `ANTHROPIC_API_KEY=...` (and `ADMIN_IDS=...`) to `.env`, then restart the bot.
On first start with an empty `wiki.db` the background updater downloads the wiki
(~340 articles, about 6 minutes at 1 request/second). To fill it straight away:
send `/updatewiki` as an admin.

### Cost

Each fresh answer is one Claude conversation with 2–3 model calls over a few
thousand tokens of wiki text — roughly **US$0.02–0.06 per question** at
`claude-opus-5-5` prices ($4 / $20 per million input/output tokens). Repeated
questions come from the cache for free. The wiki itself costs nothing.
