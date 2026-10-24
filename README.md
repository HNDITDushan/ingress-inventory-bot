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

Send `/items` any time to see the full list from the bot itself.

## Usage (in Telegram, talk to your bot)

Send `/start` (or `/menu`). Everything can be done with buttons:

- **📥 Add items / 📤 Remove items**: pick a category, then an item, then tap
  `+1 +5 +10 +25 +50 +100` (or `−…`, `🗑 All`), or type a number. Type `=20` to
  set an exact count.
- **📦 My inventory / 🗂 All accounts**: grouped by category, with combined
  totals across accounts.
- **🔄 Switch account / ➕ New account / ⚙️ Manage accounts**: view, rename or
  delete accounts.

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
| `/items` | Item names and shortcuts |
| `/delaccount [name]` | Delete an account (asks for confirmation) |
| `/cancel` | Stop what you're doing |

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
