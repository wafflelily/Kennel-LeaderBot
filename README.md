# Kennel-LeaderBot

A Discord bot that reads daily-game results ("dles") posted in your channels and turns them into monthly leaderboards. It currently tracks **Catfishing**, **FoodGuessr**, and **Gauntle**, and can also parse an introductions channel to build a **"who invited whom" invite tree**.

Adapted from [Krypton's Python-Discord-Bot-Template](https://github.com/kkrypt0nn/Python-Discord-Bot-Template).

---

## Setup

### Requirements

- Python 3.12 (or Podman)
- Dependencies from `requirements.txt`: `discord.py==2.7.1`, `aiosqlite`, `aiohttp`, `pillow`, `python-dotenv`

### Discord Developer Portal

1. Create an application and bot user, and copy the bot token.
2. Under **Bot → Privileged Gateway Intents**, enable **both**:
   - **Message Content Intent** — required to read game results posted in channels.
   - **Server Members Intent** — required to show current nicknames on leaderboards.
3. Generate an OAuth2 invite link with the `bot` and `applications.commands` scopes. In the channels it works in, the bot needs at least: View Channel, Read Message History, Send Messages, and Attach Files.

### Configuration

Copy `.env.example` to `.env` and fill it in:

```env
TOKEN=your-bot-token
PREFIX=!
INVITE_LINK=your-oauth2-invite-url
```

Optional variables: `OWNER_NAME` (shown by `/botinfo`), `STATUSES` (comma-separated rotating "Playing …" statuses), and `AUTOPOST_HOUR` (UTC hour, 0–23, when monthly auto-posts go out; default 8). That's all the configuration there is — no other config files. The SQLite database (`database/database.db`) is created and migrated automatically on startup.

### Running

Directly:

```sh
pip install -r requirements.txt
python bot.py
```

Or in a container with Podman (persists the database and logs via volumes):

```sh
podman compose up --build
```

(`podman compose` needs a compose provider — `pip install podman-compose` if you don't have one. The `Containerfile`/`compose.yaml` are standard format, so Docker works too if that's what's installed.)

### First run: sync slash commands

Slash commands don't appear in Discord until they've been synced once. As the bot owner, run the **prefix** command (it isn't available as a slash command yet at this point):

```
!sync guild     # sync to the current server only (instant)
!sync global    # sync everywhere (can take up to an hour to propagate)
```

### Running the tests

```sh
pip install -r requirements-dev.txt
pytest
```

The suite covers every game's result parser (including the year/date inference edge cases), month-window resolution, the incremental scan engine and live-capture listeners (against fake channels), the invite-map resolution pipeline (inviter precedence, tree building, pruning, image splitting), and the database layer (against an in-memory SQLite database). Nothing touches Discord, so the tests run offline in under a second.

### Versioning

The bot's version lives in the `VERSION` file (shown by `/botinfo` and logged at startup). The **minor** version bumps automatically: on every push to `main`, a GitHub Action increments it and commits the bump back — so pull after pushing to pick it up locally. Bump the **major** version manually by editing the file (e.g. to `2.0`); automatic bumps then continue from there.

---

## Commands

Most commands are *hybrid*: they work both as slash commands (`/ping`) and prefix commands (`!ping`). Slash is the primary interface. In the slash UI, `month` arguments autocomplete to the last 12 months, `game` arguments autocomplete to the loaded games, and `/autopost`'s state is a fixed on/off/status choice.

### Leaderboards (anyone can use)

Each leaderboard command scans the channel it's run in, so run it in the channel where people post their results. The first run scans the channel's history and may take a moment; later runs only pick up new messages.

| Command | What it does |
|---|---|
| `/catfishing [month]` | Leaderboard for [catfishing.net](https://catfishing.net) scores posted in this channel. Ranks players by total points with days played, average, and personal best, plus a "Group best" day and a "Hardest answers" section showing globally-difficult questions someone in the channel got right. The moment posted results collectively cover all 10 of a puzzle's questions, the bot celebrates with a small emoji message in the channel. |
| `/foodguessr [month]` | Leaderboard for FoodGuessr scores posted in this channel. Ranks players by total points (best score per day if someone posts twice), with days played, average, and a "Most perfects" count of 15,000-point games. |
| `/gauntle [month]` | Leaderboard for Gauntle runs posted in this channel. Shows the top 3 fastest overall runs and a per-category table with, for each of the 11 puzzle categories, the best effective time, the actual solve time with its bonus/penalty (e.g. `0:50.58 (−10s)`), and who set it. |
| `/mystats [game]` | How *you* stack up against everyone tallied in this channel (the game sites already show plain personal stats, so this is all comparative): days you had the top result, Catfishing answers nobody else got — including your 5 hardest and 5 easiest unique solves ranked by global solve rate — your average vs the channel's, Gauntle category bests you hold, and your share of the channel's FoodGuessr perfects. Covers everything cached for this channel, across all games unless you name one. |

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
| `/help` | List all commands, grouped by category. Commands you can't use where you're asking (owner-only, server-only in DMs) are hidden. |
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
| `/autopost <on\|off\|status> [game]` | Run in a channel to opt it in or out of automatic month-end posts: on the 1st of each month the bot posts the finished month's final leaderboard there for the chosen game (or all games if omitted). `status` shows the channel's current opt-ins. |
| `/backup` | Take a database snapshot immediately (one also runs automatically every day). |
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

The entry point is `bot.py`, which builds a `discord.py` `commands.Bot` with the message-content and members intents, configures logging (colorized console plus a rotating file log in `logs/discord.log`), and auto-loads **every** `.py` file in `cogs/` as an extension — dropping a new file into `cogs/` is all it takes to add a feature. Commands are declared as *hybrid commands*, so each one is registered both as a slash command and a prefix command from a single definition. Errors like cooldowns and missing permissions are handled centrally in `bot.py` with user-facing embeds; anything unexpected is logged with its traceback and reported to the user as a generic error.

The cogs are:

- `cogs/general.py` — the utility commands and context menus.
- `cogs/owner.py` — sync/cog management/`/rebuild`/`/shutdown`/`/say`.
- `cogs/catfishing.py`, `cogs/foodguessr.py`, `cogs/gauntle.py`, `cogs/introductions.py` — the four "leaderboard" cogs, all built on a shared engine.
- `cogs/autopost.py` — the `/autopost` opt-in command and the daily task behind the automatic month-end posts.
- `cogs/stats.py` — the `/mystats` command; it only gathers and presents, each game cog computes its own comparisons.
- `cogs/backup.py` — daily database snapshots and the `/backup` command.

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

- **Catfishing** doesn't trust post dates. Because puzzles are numbered daily, it statistically infers a puzzle-number→date anchor from the whole channel and derives every result's true date from its puzzle number — so late or backfilled posts land in the right month. It also calls the public `catfishing.net` API (cached per puzzle) to find each day's globally hardest questions for the "Hardest answers" field. And it hooks live capture: the message that completes the group's 10/10 coverage of a puzzle triggers an immediate emoji-only celebration post — only for live messages (history scans never replay old completions) and only for the completing message, so it can't fire twice for the same puzzle.
- **FoodGuessr** prefers a date embedded in the share text (e.g. `FoodGuessr - Thursday, Jun 18, 2026 UTC`) over the post date, and keeps only each player's best score per day.
- **Gauntle** stores each category's raw solve time and its bonus/penalty adjustment (including the skip penalty) separately; rankings use the *effective* time (raw + adjustment), and the per-category table shows both — the effective time and the actual solve with its adjustment. The share text states the run's date without a year ("I ran the June 18th Gauntlet…"), so the bot infers it: it picks whichever year makes that date fall closest to the day the message was posted — which gets December/January boundary posts right.
- **Month-end auto-posting.** Each game cog's embed rendering lives in a `build_leaderboard` method that both the manual command and the auto-poster call, so an automatic post looks identical to a `/gauntle` run. A daily task (08:00 UTC by default; set `AUTOPOST_HOUR` to change) checks whether it's the 1st; if so it syncs and posts the just-finished month's board to every channel opted in via `/autopost` (stored in the `leaderboard_autopost` table). If the bot is offline at that moment, that month's post is skipped rather than posted late.
- **`/mystats`** asks each game cog for its own `compare_stats` lines over everything cached for the channel — unique Catfishing solves, contested-day wins, average deltas, Gauntle category records. For Catfishing it also fetches each unique solve's global solve rate from the catfishing.net API (cached per puzzle) and lists the five hardest and five easiest answers nobody else in the channel got; if the API is unreachable the counts still work and only the lists are skipped. "Contested"/"shared" days mean at least two people posted, so playing alone never inflates a stat. The command only covers games whose leaderboard has been run in the channel at least once (it reuses their scan state for a cheap catch-up, never a surprise full scan).
- **Introductions** scans the full channel history rather than a month window. Resolving "invited by" text to an actual user uses a precedence chain: manual override (`/setinviter`) → an actual @mention in the intro → a taught alias (`/identify`) → a unique fuzzy match against member and intro names → otherwise flagged as unknown for `/whois`. The tree rendering prunes departed members' dead branches (keeping them only when a still-present member sits somewhere in their subtree) and splits oversized subtrees across multiple PNGs to stay within Discord's attachment limits.

### Database (`database/`)

Storage is a single SQLite file, `database/database.db`, accessed through `aiosqlite` via the `DatabaseManager` wrapper in `database/__init__.py`. The schema (`database/schema.sql`) is applied with idempotent `CREATE TABLE IF NOT EXISTS` statements on every startup, so there is no migration step.

| Table | Purpose |
|---|---|
| `leaderboard_results` | One row per parsed result: game, channel, message, author, date played, and the JSON payload. Shared by all leaderboard cogs, namespaced by game. |
| `leaderboard_scan` | Per game+channel scan progress: newest message seen and how far back history has been covered. |
| `leaderboard_autopost` | Channels opted in to automatic month-end posts, per game (set by `/autopost`). |
| `intro_channels` | Which channel holds each server's introductions. |
| `invite_aliases` | Name→user mappings taught via `/identify`. |
| `invite_overrides` | Per-member inviter pins set via `/setinviter`. |

Everything in `leaderboard_results` and `leaderboard_scan` is a **cache** of what's in the Discord channels: `/rebuild` can wipe it at any time, and the next leaderboard command rebuilds it from message history.

The rest is **not** rebuildable — intro channel settings, taught aliases, inviter overrides, and autopost opt-ins are hand-curated state that only exists in this file. To protect it, the bot snapshots the database daily (04:00 UTC, with a catch-up on startup if today's is missing) into `database/backups/`, keeping the last 7 days. Snapshots use SQLite's online backup API, so they're safe to take while the bot is writing. The backups live inside the same `./database` volume mount as the database, so they survive container rebuilds; to restore one, stop the bot and copy it over `database/database.db`.
