# Kennel-LeaderBot

A Discord bot that reads daily-game results ("dles") posted in your channels and turns them into monthly leaderboards. It currently tracks **Catfishing**, **FoodGuessr**, and **Gauntle**, and can also parse an introductions channel to build a **"who invited whom" invite tree**.

Adapted from [Krypton's Python-Discord-Bot-Template](https://github.com/kkrypt0nn/Python-Discord-Bot-Template).

---

## Setup

### Requirements

- Python 3.12 (or Docker)
- Dependencies from `requirements.txt`: `discord.py==2.7.1`, `aiosqlite`, `aiohttp`, `pillow`, `python-dotenv`

### Discord Developer Portal

1. Create an application and bot user, and copy the bot token.
2. Under **Bot → Privileged Gateway Intents**, enable **both**:
   - **Message Content Intent** — required to read game results posted in channels.
   - **Server Members Intent** — required to show current nicknames on leaderboards.
3. Generate an OAuth2 invite link with the `bot` and `applications.commands` scopes. In the channels it works in, the bot needs at least: View Channel, Read Message History, Send Messages, and Attach Files.

### Configuration

Create a `.env` file in the repo root:

```env
TOKEN=your-bot-token
PREFIX=!
INVITE_LINK=your-oauth2-invite-url
```

That's all the configuration there is — no other config files. The SQLite database (`database/database.db`) is created and migrated automatically on startup.

### Running

Directly:

```sh
pip install -r requirements.txt
python bot.py
```

Or with Docker (persists the database and log file via volumes):

```sh
docker compose up --build
```

### First run: sync slash commands

Slash commands don't appear in Discord until they've been synced once. As the bot owner, run the **prefix** command (it isn't available as a slash command yet at this point):

```
!sync guild     # sync to the current server only (instant)
!sync global    # sync everywhere (can take up to an hour to propagate)
```

---

## Commands

Most commands are *hybrid*: they work both as slash commands (`/ping`) and prefix commands (`!ping`). Slash is the primary interface.

### Leaderboards (anyone can use)

Each leaderboard command scans the channel it's run in, so run it in the channel where people post their results. The first run scans the channel's history and may take a moment; later runs only pick up new messages.

| Command | What it does |
|---|---|
| `/catfishing [month]` | Leaderboard for [catfishing.net](https://catfishing.net) scores posted in this channel. Ranks players by total points with days played, average, and personal best, plus a "Group best" day and a "Hardest answers" section showing globally-difficult questions someone in the channel got right. |
| `/foodguessr [month]` | Leaderboard for FoodGuessr scores posted in this channel. Ranks players by total points (best score per day if someone posts twice), with days played, average, and a "Most perfects" count of 15,000-point games. |
| `/gauntle [month]` | Leaderboard for Gauntle runs posted in this channel. Shows the top 3 fastest overall runs and a per-category table of the best effective time in each of the 11 puzzle categories and who set it. |

The optional `month` argument accepts formats like `June`, `Jun 2026`, `2026-06`, or `6`. If omitted, it defaults to the **previous** calendar month (or the current month when run on its last day).

For a message to be counted, it just needs to contain the game's standard share text — see [Recognized message formats](#recognized-message-formats) below.

### Invite map (owner-only, in-server only)

These build a tree of who invited whom, based on people's posts in an introductions channel (lines like `Name -`, `Invited by -`, etc.).

| Command | What it does |
|---|---|
| `/introchannel <channel>` | Tell the bot which channel holds this server's introductions. Do this first. |
| `/invitemap` | Scan the intro channel and post the invite tree: a summary embed, an interactive HTML file (collapsible tree with a filter box), and PNG diagram images. Large branches are split across multiple images. |
| `/whois` | List "invited by" names the bot couldn't match to a real user, so you can fix them with `/identify`. |
| `/uninvited` | List current members with no recorded inviter, each with the reason (no intro / intro doesn't say / unresolved name) and a suggested fix. |
| `/identify <alias> <target>` | Teach the bot that a written name means a specific user — e.g. someone wrote "invited by Batty" and Batty is `@battery`. |
| `/setinviter <member> <inviter>` | Manually pin a member's inviter, overriding their intro. `inviter` can be a user, a name, `none` (explicitly nobody), or `auto` (remove the override and go back to automatic resolution). |

### General utilities (anyone can use)

| Command | What it does |
|---|---|
| `/help` | List all commands, grouped by category. (Owner commands are only shown to the owner.) |
| `/ping` | Show the bot's latency. |
| `/botinfo` | Show bot/owner/version info. |
| `/invite` | DM you the bot's invite link. |
| Right-click a message → **Apps → Remove spoilers** | Repost the message's text with `\|\|spoiler\|\|` markers stripped (shown only to you). |
| Right-click a user → **Apps → Grab ID** | Show that user's Discord ID (shown only to you). |

### Bot administration (owner-only)

| Command | What it does |
|---|---|
| `!sync <global\|guild>` | Register slash commands with Discord (prefix-only — see [First run](#first-run-sync-slash-commands)). |
| `!unsync <global\|guild>` | Remove registered slash commands. |
| `/load <cog>` / `/unload <cog>` / `/reload <cog>` | Load, unload, or hot-reload a cog by name (e.g. `gauntle`). |
| `/rebuild [game]` | Wipe the cached results for one game (or all games if omitted) so the next leaderboard command re-scans and re-parses the channel history from scratch. Use after changing a cog's parsing rules. |
| `/say <message>` | Make the bot repeat a message. |
| `/embed <message>` | Make the bot repeat a message inside an embed. |
| `/shutdown` | Shut the bot down. |

### Recognized message formats

- **Catfishing**: the standard share text — a score line like `#723 - 4/10` plus the result grid, either the emoji grid (ten 🐈/🐟/🥚) or the letter grid (ten `C`/`F`/`E` characters).
- **FoodGuessr**: the standard share text containing `Total score: X / 15,000` or `I got X on the FoodGuessr Daily!`; a bare four-line paste of total + three round scores also works.
- **Gauntle**: the standard share text — the header line `I ran the June 18th Gauntlet in X minutes and Y seconds!` plus the per-category lines like `🟩 Sudoku: 0:52.24 (−10s)`.
- **Introductions**: a post with at least two labelled lines such as `Name - ...`, `Pronouns - ...`, `Invited by - ...` (formatting is tolerant of bullets/markdown); a freeform message containing "invited by X" also works if a name can be inferred.

---

## How it works

### Architecture

The entry point is `bot.py`, which builds a `discord.py` `commands.Bot` with the message-content and members intents, configures logging (colorized console plus `discord.log`, truncated on each start), and auto-loads **every** `.py` file in `cogs/` as an extension — dropping a new file into `cogs/` is all it takes to add a feature. Commands are declared as *hybrid commands*, so each one is registered both as a slash command and a prefix command from a single definition. Errors like cooldowns and missing permissions are handled centrally in `bot.py` with user-facing embeds.

The cogs are:

- `cogs/general.py` — the utility commands and context menus.
- `cogs/owner.py` — sync/cog management/`/rebuild`/`/shutdown`/`/say`.
- `cogs/catfishing.py`, `cogs/foodguessr.py`, `cogs/gauntle.py`, `cogs/introductions.py` — the four "leaderboard" cogs, all built on a shared engine.

### The leaderboard engine (`leaderboard/base.py`)

All four leaderboard cogs subclass `LeaderboardCog`, which handles everything except the actual parsing. A subclass only needs to set a `GAME` name (which namespaces its rows in the database) and implement one method:

```python
def parse(content, posted_on) -> (played_on, payload) | None
```

It receives a message's text and returns the date the game was played plus a JSON payload of whatever the cog wants to store (score, times, intro fields, …), or `None` if the message isn't a result.

The engine provides:

- **Incremental scanning** (`_sync_channel`). Per channel and per game, the database remembers the newest message seen and how far back history has been scanned. The first command run in a channel scans the needed history; every later run only fetches messages newer than the last one seen, plus a one-off backfill if an older month is requested (with a 2-day buffer before the month start to catch boundary posts).
- **Live capture**. `on_message`, `on_raw_message_edit`, and `on_raw_message_delete` listeners keep the cache current in real time — new results are stored as they're posted, and edited or deleted messages update or remove their cached rows. (Live capture starts for a channel once a leaderboard command has been run there at least once.)
- **Month resolution** (`_resolve_window`). Turns the free-text `month` argument into a concrete date window, defaulting to the previous month.
- **Name resolution** (`_resolve_names`). Leaderboards display members' *current* server nicknames via the member cache (falling back to an API fetch, and finally to the name stored at post time).

Cog-specific twists on top of this:

- **Catfishing** doesn't trust post dates. Because puzzles are numbered daily, it statistically infers a puzzle-number→date anchor from the whole channel and derives every result's true date from its puzzle number — so late or backfilled posts land in the right month. It also calls the public `catfishing.net` API (cached per puzzle) to find each day's globally hardest questions for the "Hardest answers" field.
- **FoodGuessr** prefers a date embedded in the share text (e.g. `FoodGuessr - Thursday, Jun 18, 2026 UTC`) over the post date, and keeps only each player's best score per day.
- **Gauntle** parses per-category times with bonus/penalty adjustments (including the skip penalty) into *effective* times. The share text states the run's date without a year ("I ran the June 18th Gauntlet…"), so the bot infers it: it picks whichever year makes that date fall closest to the day the message was posted — which gets December/January boundary posts right.
- **Introductions** scans the full channel history rather than a month window. Resolving "invited by" text to an actual user uses a precedence chain: manual override (`/setinviter`) → an actual @mention in the intro → a taught alias (`/identify`) → a unique fuzzy match against member and intro names → otherwise flagged as unknown for `/whois`. The tree rendering prunes departed members' dead branches (keeping them only when a still-present member sits somewhere in their subtree) and splits oversized subtrees across multiple PNGs to stay within Discord's attachment limits.

### Database (`database/`)

Storage is a single SQLite file, `database/database.db`, accessed through `aiosqlite` via the `DatabaseManager` wrapper in `database/__init__.py`. The schema (`database/schema.sql`) is applied with idempotent `CREATE TABLE IF NOT EXISTS` statements on every startup, so there is no migration step.

| Table | Purpose |
|---|---|
| `leaderboard_results` | One row per parsed result: game, channel, message, author, date played, and the JSON payload. Shared by all leaderboard cogs, namespaced by game. |
| `leaderboard_scan` | Per game+channel scan progress: newest message seen and how far back history has been covered. |
| `intro_channels` | Which channel holds each server's introductions. |
| `invite_aliases` | Name→user mappings taught via `/identify`. |
| `invite_overrides` | Per-member inviter pins set via `/setinviter`. |

Everything in `leaderboard_results` and `leaderboard_scan` is a **cache** of what's in the Discord channels: `/rebuild` can wipe it at any time, and the next leaderboard command rebuilds it from message history.
