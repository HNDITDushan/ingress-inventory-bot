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
- **Heat Sinks**: Common / Rare / Very Rare
- **Multi-Hacks**: Common / Rare / Very Rare
- **Link Amps**: Common / Rare / Soft Bank Ultra Link
- **Ops & Keys**: ITO +, ITO -, Keys

Send `/items` any time to see the full list from the bot itself.

## Usage (in Telegram, talk to your bot)

| Command | Effect |
|---|---|
| `/newaccount <name>` | Create an inventory account (auto-selected if it's your first) |
| `/use <name>` | Switch the active account |
| `/accounts` | List your accounts, marks the active one |
| `/additems` | Tap-to-add menu: pick a category, pick an item, type a quantity, repeat |
| `/add <qty> <item>` | Quick one-off add, e.g. `/add 5 U3` (item must match the catalog) |
| `/remove <qty> <item>` | Remove items from the active account |
| `/list` | Show items in the active account |
| `/list <name>` | Show items in a specific account (without switching to it) |
| `/listall` | Show items across every account you own |
| `/items` | Show the full recognized item catalog |
| `/delaccount <name>` | Delete an account and all its items |

### Example flow

```
/newaccount MainAccount
/additems             -> tap "Resonators" -> tap "R1" -> type 8 -> tap "Done"
/newaccount AltAccount
/use AltAccount
/add 3 Keys
/list                  -> shows AltAccount's items
/use MainAccount
/list                  -> shows MainAccount's items
/listall               -> shows both accounts, all items
```

## Notes

- Quantities accumulate: adding U3 twice (5, then 3) results in 8.
- `/remove` past zero deletes the item row.
- `/add`/`/remove` only accept items from the catalog (case-insensitive);
  unrecognized names are rejected with a pointer to `/items`.
- All data is per Telegram user ID — if multiple people use the same bot,
  they won't see each other's accounts/items.
- To run this continuously, use a process manager (systemd, pm2, NSSM on
  Windows, Task Scheduler, or a Docker container with `restart: always`).
