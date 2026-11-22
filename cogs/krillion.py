"""
Krillion leaderboard cog for Kennel-LeaderBot.

Tallies shared Krillion (krillion.io, "the daily dive") results in the
channel/thread the command is invoked in — seven prompts a day, rarer answers
score more — and posts a monthly leaderboard by total score, with counts of
each player's shrimp/lanternfish/squid catches.

Expected message formats (as copy-pasted from krillion.io):

    Krillion #47 🦐
    260

    🦑🦑🫧🦑🐟🫧🐟

or the bare form, just the day number and score:

    #47
    265

Archive replays ("Krillion ⟲ #99 🦐") are a past daily game played again, and
count just like a regular share. Pack results are NOT counted: any other emoji
between the name and the number ("Krillion 🎬 #1 🦐") marks a pack rather than
the daily game, and those don't match the daily patterns.

Each grid symbol is one prompt's answer quality:
⬛ no submission (0) · 🫧 bubbles (10) · 🤡 too smart (15) · 🐟 fish (30) ·
🦑 squid (60) · 🏮 lanternfish (85) · 🌟 shrimp (100), so a perfect day is
700. "Too smart" is a rare smartass answer, usually one per prompt at most,
e.g. "Human" for "Name a living mammal native to Africa". When a grid is
present it's authoritative (the stated score is ignored); the bare form is
validated strictly (0-700, multiple of 5) so ordinary chat can't match.

Like Catfishing, shares carry a day number instead of a date, so results are
attributed to months via the shared puzzle-number anchor (see
``leaderboard.base``). Parsed results are cached in the database; the command
does an incremental catch-up scan and reads its aggregates from there.
"""

import asyncio
import io
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Context

from leaderboard.base import LeaderboardCog, Trimmable, month_choices
from leaderboard.krillion_table import render_day_table

# Answer symbol -> points. ⬛ means "no submission".
VALUES = {"⬛": 0, "🫧": 10, "🤡": 15, "🐟": 30, "🦑": 60, "🏮": 85, "🌟": 100}
PROMPTS = 7
MAX_SCORE = PROMPTS * 100

# The catch symbols counted on the leaderboard, best first. The top tier
# ("shrimp", the game's 100-point answer) shows as a star in the result grid.
SHRIMP, LANTERN, SQUID = "🌟", "🏮", "🦑"
# The rare "too smart" (smartass) answer.
TOO_SMART = "🤡"

# Matches the daily header, e.g. "Krillion #47 🦐", or an archive replay of a
# past day, "Krillion ⟲ #99 🦐". Nothing else may sit between the name and the
# number: pack results put the pack's emoji there ("Krillion 🎬 #1 🦐"), and
# packs aren't the daily game.
HEADER_RE = re.compile(r"\bkrillion\s*(?:⟲️?\s*)?#(\d+)", re.IGNORECASE)

# The bare form: the whole message is just "#day" then the score.
BARE_RE = re.compile(r"#(\d+)\s+(\d+)")

# A line consisting solely of a number (the day's total score).
SCORE_LINE_RE = re.compile(r"^\s*(\d+)\s*$")

# The site only serves the *current* day's data publicly — past days sit
# behind the paid archive (`/api/archive/<day>/reveal`, which needs the $5
# Unlimited unlock token) — so the bot archives each day as it appears
# (daily task + startup catch-up), and uses the archive only for days it
# missed. `/api/today` gives the day
# number; `/api/reveal?date=<today>` gives the full answer sheet, publicly and
# without auth, from which we keep each prompt's shrimp (score-100) and too
# smart (score-15) answers.
TODAY_URL = "https://krillion.io/api/today"
REVEAL_URL = "https://krillion.io/api/reveal?date={date}"
# The paid archive, for filling in days the daily fetch missed (e.g. the bot
# was offline all day). Needs the Unlimited unlock token, read from the
# KRILLION_UNLOCK_TOKEN env var and sent as a header; skipped when unset.
# Only past days are served — today's day isn't in it yet.
ARCHIVE_URL = "https://krillion.io/api/archive/{day}"
ARCHIVE_REVEAL_URL = "https://krillion.io/api/archive/{day}/reveal"
UNLOCK_HEADER = "x-krillion-unlock"
# Pause between archive requests, to go easy on a one-person site.
ARCHIVE_DELAY = 1.0
# When the daily fetch runs (UTC; the puzzle resets at midnight US Eastern,
# 04:00 or 05:00 UTC depending on daylight saving).
FETCH_AT = time(hour=12, tzinfo=timezone.utc)
RESET_TZ = ZoneInfo("America/New_York")
# The points values that mark a shrimp and a too smart answer.
SHRIMP_POINTS = 100
TOO_SMART_POINTS = 15

# The site's Friends tab: today's scores of the bot owner's Krillion friends,
# plus the answers of friends who turned on "Share my answers with friends".
# It needs the owner's Google sign-in, so the bot keeps the site's Firebase
# refresh token (KRILLION_REFRESH_TOKEN) and swaps it for an hour-long ID
# token. The swap needs krillion.io's Firebase web API key
# (KRILLION_FIREBASE_API_KEY) — public, it ships in the site's own JS, but
# kept out of the code so it doesn't trip secret scanners. Only the current
# day is served, so it's snapshotted through the afternoon and evening; the
# answers are only shown for members who linked themselves with
# /krillionlink, and only once the day is over.
FRIENDS_URL = "https://krillion.io/api/friends"
TOKEN_URL = "https://securetoken.googleapis.com/v1/token?key={key}"
# When to snapshot the Friends tab (US Eastern, the site's day): spread from
# the afternoon on, so one failed fetch doesn't lose the day, with the last
# shortly before the midnight reset to catch late players.
FRIENDS_AT = [
    time(hour=15, tzinfo=RESET_TZ),
    time(hour=18, tzinfo=RESET_TZ),
    time(hour=21, tzinfo=RESET_TZ),
    time(hour=23, minute=45, tzinfo=RESET_TZ),
]
# How often a command may re-fetch the friends list (e.g. /krillionlink
# checking a name that wasn't on the last copy).
FRIEND_LIST_COOLDOWN = timedelta(minutes=1)
# The daily post only goes out once this many linked members shared that
# day's result in the channel.
MIN_DAILY_PLAYERS = 2
# krillion.io usernames, by the site's own rule (a leading @ is stripped).
USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,19}")


def _short(text: str, limit: int = 60) -> str:
    """Trim a prompt text so embed fields stay within Discord's limits."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _slot_answers(info: dict, key: str, slot: int) -> list[str]:
    """
    The archived answers under ``key`` ("shrimp" or "too_smart") for one
    prompt slot, as a list.

    Tolerates the current per-slot list form and the earlier single-string
    form, returning [] when nothing is archived for that slot.
    """
    archived = info.get(key) or []
    if slot >= len(archived):
        return []
    entry = archived[slot]
    if not entry:
        return []
    return [entry] if isinstance(entry, str) else [a for a in entry if a]


def _star_answers(info: dict, slot: int) -> list[str]:
    """The archived shrimp answers for one prompt slot."""
    return _slot_answers(info, "shrimp", slot)


def _too_smart_answers(info: dict, slot: int) -> list[str]:
    """The archived too smart answers for one prompt slot."""
    return _slot_answers(info, "too_smart", slot)


def _star_note(answers: list[str], limit: int = 40) -> str:
    """Format one or more star answers for display (joined with ' / ')."""
    return _short(" / ".join(answers), limit)


def _friend_round(day: dict | None, slot: int) -> dict | None:
    """
    One friend's stored round (``{answer, score}``) for a prompt slot, or None.

    Rounds are stored in the archived prompts' order (checked when they're
    fetched), so the prompt text itself comes from the daily archive.
    """
    rounds = (day or {}).get("answers") or []
    if slot >= len(rounds) or not isinstance(rounds[slot], dict):
        return None
    return rounds[slot]


def _given_answers(days: list[dict | None], slot: int, points: int) -> list[str]:
    """The answers these friends gave on a prompt slot that scored ``points``."""
    given = []
    for day in days:
        played = _friend_round(day, slot)
        if played and played.get("score") == points and played.get("answer"):
            given.append(played["answer"])
    return given


def _narrow(candidates: list[str], given: list[str]) -> list[str]:
    """
    Narrow a prompt's archived answers to the ones players actually gave.

    Given answers are matched to the archived spelling case-insensitively;
    one that matches none of them is kept as given. With nothing given, all
    the candidates stand.
    """
    if not given:
        return candidates
    wanted = {g.strip().lower() for g in given}
    found = [c for c in candidates if c.strip().lower() in wanted]
    known = {c.strip().lower() for c in found}
    for g in given:
        if g.strip().lower() not in known:
            known.add(g.strip().lower())
            found.append(g.strip())
    return found


class Krillion(LeaderboardCog, name="krillion"):
    GAME = "krillion"

    def __init__(self, bot) -> None:
        super().__init__(bot)
        self._archived_on_boot = False
        # Cached Firebase ID token for the Friends tab: (token, expires at).
        self._id_token: tuple[str, datetime] | None = None
        # From the last Friends fetch: the owner's username and their friends'.
        self._owner_username: str | None = None
        self._friend_usernames: set[str] = set()
        self._friends_fetched_at: datetime | None = None

    async def cog_load(self) -> None:
        self.prompt_archiver.start()
        self.friends_snapshot.start()

    async def cog_unload(self) -> None:
        self.prompt_archiver.cancel()
        self.friends_snapshot.cancel()

    # ------------------------------------------------------------------ #
    # Prompt archive
    # ------------------------------------------------------------------ #

    @staticmethod
    def _answers_from_reveal(
        reveal, points: int = SHRIMP_POINTS
    ) -> list[list[str]] | None:
        """
        Pull each prompt's answers worth ``points`` from a reveal sheet — the
        shrimp (100) answers by default, or the too smart (15) ones.

        There's usually at most one, but a prompt can have several, so each
        entry is a list of all its matching answers (possibly empty). Returns a
        list of ``PROMPTS`` such entries, or None if the sheet's shape is
        unexpected.
        """
        if not isinstance(reveal, dict):
            return None
        prompts = reveal.get("prompts")
        if not isinstance(prompts, list) or len(prompts) != PROMPTS:
            return None
        found: list[list[str]] = []
        for prompt in prompts:
            found.append(
                [
                    a.get("answer")
                    for a in (prompt.get("answers") or [])
                    if a.get("score") == points and a.get("answer")
                ]
            )
        return found

    async def _store_today(self, today, reveal) -> bool:
        """
        Validate and store one day's archive from the two API payloads.

        ``today`` supplies the day number and prompt texts; ``reveal`` (may be
        None if that fetch failed) supplies the per-prompt shrimp and too smart
        answers. Returns True if stored.
        """
        if not isinstance(today, dict):
            return False
        day = today.get("dayNumber")
        prompts = [
            prompt.get("text")
            for prompt in today.get("prompts") or []
            if isinstance(prompt, dict) and prompt.get("text")
        ]
        if not isinstance(day, int) or len(prompts) != PROMPTS:
            return False
        payload = {"date": today.get("date"), "prompts": prompts}
        shrimp = self._answers_from_reveal(reveal, SHRIMP_POINTS)
        if shrimp is not None:
            payload["shrimp"] = shrimp
            payload["too_smart"] = self._answers_from_reveal(
                reveal, TOO_SMART_POINTS
            )
        await self.bot.database.set_puzzle_info(self.GAME, day, payload)
        return True

    @staticmethod
    async def _get_json(session, url, headers=None):
        """GET a URL and return parsed JSON, or None on any failure."""
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    @staticmethod
    def _from_archive(meta, rounds_sheet) -> tuple[dict | None, dict | None]:
        """
        Reshape the archive's day and reveal payloads into the ``/api/today``
        and ``/api/reveal`` shapes, so ``_store_today`` can store them.

        The archive's reveal lists ``rounds`` keyed by prompt id; they're put
        back into the day's prompt order. Returns ``(None, None)`` if the day
        payload is unusable, and a None reveal if only the sheet is.
        """
        if not isinstance(meta, dict) or not isinstance(meta.get("prompts"), list):
            return None, None
        today = {
            "date": meta.get("date"),
            "dayNumber": meta.get("game"),
            "prompts": meta["prompts"],
        }
        rounds = (
            rounds_sheet.get("rounds") if isinstance(rounds_sheet, dict) else None
        )
        if not isinstance(rounds, list):
            return today, None
        by_id = {r.get("round"): r for r in rounds if isinstance(r, dict)}
        ids = [p.get("id") for p in meta["prompts"] if isinstance(p, dict)]
        if any(i not in by_id for i in ids):
            return today, None
        return today, {"prompts": [by_id[i] for i in ids]}

    async def _backfill_missing(self, session, today_day: int) -> None:
        """
        Fill past days the daily fetch missed from the paid archive.

        A day counts as missing if it was never stored, or was stored without
        its answer sheet (the reveal fetch failed that day). Does nothing
        without an unlock token; stops at the first failure and tries again
        on the next run.
        """
        token = os.getenv("KRILLION_UNLOCK_TOKEN")
        if not token:
            return
        stored = await self.bot.database.get_puzzle_info(self.GAME)
        missing = [
            day
            for day in range(1, today_day)
            if "shrimp" not in stored.get(day, {})
            or "too_smart" not in stored.get(day, {})
        ]
        headers = {UNLOCK_HEADER: token}
        filled = []
        for day in missing:
            meta = await self._get_json(
                session, ARCHIVE_URL.format(day=day), headers
            )
            await asyncio.sleep(ARCHIVE_DELAY)
            sheet = await self._get_json(
                session, ARCHIVE_REVEAL_URL.format(day=day), headers
            )
            await asyncio.sleep(ARCHIVE_DELAY)
            today, reveal = self._from_archive(meta, sheet)
            if today is None or today.get("dayNumber") != day or reveal is None:
                self.bot.logger.warning(
                    f"Couldn't fetch Krillion day #{day} from the archive "
                    "(is KRILLION_UNLOCK_TOKEN still valid?); will retry later"
                )
                break
            if await self._store_today(today, reveal):
                filled.append(day)
        if filled:
            self.bot.logger.info(
                f"Filled {len(filled)} missed Krillion day(s) from the archive: "
                + ", ".join(f"#{day}" for day in filled)
            )

    async def _archive_today(self) -> None:
        """
        Fetch and archive today's puzzle, then fill any missed past days from
        the archive; failures just try again later.
        """
        async with aiohttp.ClientSession() as session:
            today = await self._get_json(session, TODAY_URL)
            reveal = None
            if isinstance(today, dict) and today.get("date"):
                # The reveal sheet is public for the current day and carries the
                # shrimp and too smart answers; past days need the paid archive.
                reveal = await self._get_json(
                    session, REVEAL_URL.format(date=today["date"])
                )
            if await self._store_today(today, reveal):
                extra = "" if reveal else " (prompts only; answer sheet unavailable)"
                self.bot.logger.info(f"Archived today's Krillion puzzle{extra}")
                await self._backfill_missing(session, today["dayNumber"])

    @tasks.loop(time=FETCH_AT)
    async def prompt_archiver(self) -> None:
        await self._archive_today()

    @prompt_archiver.before_loop
    async def before_prompt_archiver(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Catch-up fetch on startup: a bot that was asleep at FETCH_AT still
        # archives today's on boot, and fills any missed days from the archive.
        # Then a Friends snapshot, so a bot that was down for one of the
        # scheduled ones still has today's results (once today's archived).
        if self._archived_on_boot:
            return
        self._archived_on_boot = True
        stripped = await self.bot.database.strip_krillion_friend_prompts()
        if stripped:
            self.bot.logger.info(
                f"Removed stored prompt text from {stripped} Krillion friend-days "
                "(it's read from the daily archive)"
            )
        await self._archive_today()
        await self._snapshot_friends()

    # ------------------------------------------------------------------ #
    # Friends' answers
    # ------------------------------------------------------------------ #

    async def _friends_id_token(
        self, session, refresh_token: str, api_key: str
    ) -> str | None:
        """A Firebase ID token for the owner's account, refreshed as needed."""
        now = datetime.now(timezone.utc)
        if self._id_token is not None and self._id_token[1] > now:
            return self._id_token[0]
        try:
            async with session.post(
                TOKEN_URL.format(key=api_key),
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    self.bot.logger.warning(
                        f"Krillion sign-in refresh failed (HTTP {resp.status}); "
                        "are KRILLION_REFRESH_TOKEN and KRILLION_FIREBASE_API_KEY "
                        "still valid?"
                    )
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None
        token = data.get("id_token")
        if not token:
            return None
        # Renew five minutes early so a request never goes out with a stale one.
        lifetime = int(data.get("expires_in", 3600)) - 300
        self._id_token = (token, now + timedelta(seconds=max(lifetime, 60)))
        return token

    @staticmethod
    def _friend_day(entry) -> tuple[str, dict] | None:
        """
        Reshape one Friends leaderboard entry into ``(username, payload)`` for
        storage, or None if the friend hasn't played yet. A friend who keeps
        their answers private is stored with just their score (``answers`` is
        None), and nothing of theirs is ever displayed.
        """
        if not isinstance(entry, dict) or not isinstance(entry.get("username"), str):
            return None
        score = entry.get("score")
        if not isinstance(score, int):
            return None
        rounds = entry.get("answers")
        if rounds is None:
            return entry["username"], {
                "username": entry["username"],
                "score": score,
                "answers": None,
            }
        if not isinstance(rounds, list) or len(rounds) != PROMPTS:
            return None
        answers = []
        for played in rounds:
            if not isinstance(played, dict):
                return None
            # The prompt text isn't kept: it's checked against the archive when
            # fetched, and read from there.
            answers.append(
                {"answer": played.get("answer"), "score": played.get("score") or 0}
            )
        return entry["username"], {
            "username": entry["username"],
            "score": score,
            "answers": answers,
        }

    async def _fetch_friends_board(self) -> dict | None:
        """
        Fetch the owner's Friends tab, remembering the owner's username and
        the friends list. Returns the board, or None without
        KRILLION_REFRESH_TOKEN and KRILLION_FIREBASE_API_KEY or on failure.
        """
        refresh_token = os.getenv("KRILLION_REFRESH_TOKEN")
        api_key = os.getenv("KRILLION_FIREBASE_API_KEY")
        if not refresh_token or not api_key:
            return None
        async with aiohttp.ClientSession() as session:
            token = await self._friends_id_token(session, refresh_token, api_key)
            if token is None:
                return None
            board = await self._get_json(
                session, FRIENDS_URL, {"Authorization": f"Bearer {token}"}
            )
        if not isinstance(board, dict) or not isinstance(board.get("leaderboard"), list):
            # Most likely an expired ID token; get a fresh one next time.
            self._id_token = None
            return None
        profile = board.get("profile") or {}
        self._owner_username = profile.get("username")
        self._friend_usernames = {
            e["username"].lower()
            for e in board["leaderboard"]
            if isinstance(e, dict) and isinstance(e.get("username"), str)
        }
        self._friends_fetched_at = datetime.now(timezone.utc)
        return board

    async def _refresh_friend_list(self) -> None:
        """
        Re-fetch the friends list for a command, at most once a minute, so
        someone who just added the owner is found without a flood of requests
        from repeated typos.
        """
        last = self._friends_fetched_at
        if last is None or datetime.now(timezone.utc) - last >= FRIEND_LIST_COOLDOWN:
            await self._fetch_friends_board()

    @staticmethod
    def _misordered(board: dict, prompts: list[str]) -> list[str]:
        """
        Friends whose rounds don't line up with the archived prompts, by
        position. Rounds are stored without their prompt text (it's read from
        the archive), so they must match before they're stored.
        """
        wanted = [p.strip().lower() for p in prompts]
        odd = []
        for entry in board["leaderboard"]:
            rounds = entry.get("answers") if isinstance(entry, dict) else None
            # Only check what would be stored; malformed entries are skipped.
            if not isinstance(rounds, list) or len(rounds) != PROMPTS or not all(
                isinstance(r, dict) for r in rounds
            ):
                continue
            got = [(r.get("prompt") or "").strip().lower() for r in rounds]
            if got != wanted:
                odd.append(f"@{entry.get('username')}")
        return odd

    async def _snapshot_friends(self) -> None:
        """
        Fetch today's Friends tab and store every friend's result under the
        day's number. Does nothing without the sign-in settings, or while the
        board is still locked (the owner hasn't finished today's daily).
        """
        board = await self._fetch_friends_board()
        if board is None:
            if os.getenv("KRILLION_REFRESH_TOKEN") and os.getenv("KRILLION_FIREBASE_API_KEY"):
                self.bot.logger.warning("Krillion friends fetch failed; will retry later")
            return
        if not board.get("unlocked"):
            self.bot.logger.info(
                "Krillion friends fetch: board locked until the owner finishes "
                "today's daily; nothing stored"
            )
            return

        stored = await self.bot.database.get_puzzle_info(self.GAME)
        day = next((d for d, i in stored.items() if i.get("date") == board.get("date")), None)
        if day is None:
            # Today's puzzle isn't archived yet (e.g. just after the reset).
            await self._archive_today()
            stored = await self.bot.database.get_puzzle_info(self.GAME)
            day = next(
                (d for d, i in stored.items() if i.get("date") == board.get("date")),
                None,
            )
            if day is None:
                self.bot.logger.warning(
                    f"Krillion friends fetch: no archived puzzle for "
                    f"{board.get('date')}; nothing stored"
                )
                return
        misordered = self._misordered(board, stored[day].get("prompts") or [])
        if misordered:
            self.bot.logger.warning(
                f"Krillion friends fetch for #{day}: rounds don't match the "
                f"archived prompts for {', '.join(misordered)}; nothing stored"
            )
            return
        shared, private, unplayed, skipped = 0, 0, [], []
        for entry in board["leaderboard"]:
            found = self._friend_day(entry)
            if found is not None:
                await self.bot.database.set_krillion_friend_answers(day, *found)
                if found[1]["answers"]:
                    shared += 1
                else:
                    private += 1
            elif isinstance(entry, dict) and entry.get("score") is None:
                unplayed.append(f"@{entry.get('username')}")
            else:
                # Played, but in a shape we don't understand.
                name = entry.get("username") if isinstance(entry, dict) else None
                skipped.append(f"@{name}")
        summary = (
            f"Krillion friends fetch for #{day}: stored {shared + private} "
            f"({shared} with answers, {private} score only)"
        )
        if unplayed:
            summary += f"; not played yet: {', '.join(unplayed)}"
        if skipped:
            summary += f"; skipped (unexpected data): {', '.join(skipped)}"
        self.bot.logger.info(summary)

    @tasks.loop(time=FRIENDS_AT)
    async def friends_snapshot(self) -> None:
        await self._snapshot_friends()

    @friends_snapshot.before_loop
    async def before_friends_snapshot(self) -> None:
        await self.bot.wait_until_ready()

    async def _linked_days(self) -> dict[int, dict[int, dict]]:
        """Linked members' stored answers: puzzle -> Discord user id -> payload."""
        links = await self.bot.database.get_krillion_links()
        stored = await self.bot.database.get_krillion_friend_answers()
        return {
            puzzle: {
                user_id: friends[username]
                for user_id, username in links.items()
                if username in friends
            }
            for puzzle, friends in stored.items()
        }

    async def _carry_history(self, old: str, new: str) -> int:
        """
        Move a member's stored days to their new username when they relink
        after renaming on krillion.io (the site has no stable friend id, only
        usernames). Only done when it looks like a real rename: the old name
        is gone from the friends list, and the two names never played the
        same day, so a member who'd linked the wrong name can't absorb someone
        else's history. Returns how many days moved.
        """
        if not self._friend_usernames:
            # Not fetched since startup; one fetch tells us who's still there.
            await self._refresh_friend_list()
        if not self._friend_usernames or old.lower() in self._friend_usernames:
            return 0
        stored = await self.bot.database.get_krillion_friend_answers()
        if any(old.lower() in day and new.lower() in day for day in stored.values()):
            return 0
        return await self.bot.database.rename_krillion_friend(old, new)

    @commands.hybrid_command(
        name="krillionlink",
        description="Link your krillion.io username so your shared answers can be shown here.",
    )
    @app_commands.describe(username="Your krillion.io username, e.g. `shrimpfan`.")
    async def krillionlink(self, context: Context, username: str) -> None:
        """
        Link the caller's Discord account to their Krillion username.

        :param context: The hybrid command context.
        :param username: The caller's krillion.io username.
        """
        username = username.strip().lstrip("@")
        if not USERNAME_RE.fullmatch(username):
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=f"`{username}` doesn't look like a Krillion username.",
                    color=0xE02B2B,
                ),
                ephemeral=True,
            )
            return
        # The name must be on the owner's friends list; re-check the site if
        # it isn't in the last copy, in case they've only just added the owner.
        if username.lower() not in self._friend_usernames:
            await self._refresh_friend_list()
        owner = f"`@{self._owner_username}`" if self._owner_username else "the bot owner"
        if not self._friend_usernames:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        "I couldn't check the Krillion friends list right now. "
                        "Please try again later."
                    ),
                    color=0xE02B2B,
                ),
                ephemeral=True,
            )
            return
        if username.lower() not in self._friend_usernames:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        f"`@{username}` isn't on {owner}'s Krillion friends list.\n\n"
                        f"• Make sure you've added {owner} as a friend on krillion.io "
                        "(Friends page).\n"
                        "• Make sure you typed your Krillion username correctly; "
                        "it's shown on your Friends page under **Manage your "
                        "profile & invites**."
                    ),
                    color=0xE02B2B,
                ),
                ephemeral=True,
            )
            return
        previous = (await self.bot.database.get_krillion_links()).get(context.author.id)
        if not await self.bot.database.set_krillion_link(context.author.id, username):
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        f"`@{username}` is already linked to someone else. "
                        "If it's yours, ask the bot owner to unlink it."
                    ),
                    color=0xE02B2B,
                ),
                ephemeral=True,
            )
            return
        description = (
            f"Linked you to Krillion user `@{username}`.\n\n"
            "As long as you keep **Share my answers with friends** turned on, "
            "then once each day is over:\n"
            "• your answers appear in the daily Krillion post in channels with "
            "Krillion auto-posts on\n"
            "• your exact shrimp 🌟 and too smart 🤡 answers are named in "
            "`/mystats` and the leaderboards\n\n"
            "Run `/krillionunlink` any time to stop."
        )
        if previous and previous != username.lower():
            moved = await self._carry_history(previous, username)
            if moved:
                description += (
                    f"\n\nMoved {moved} day{'s' if moved != 1 else ''} of history "
                    f"from your old username `@{previous}`."
                )
        await context.send(
            embed=discord.Embed(
                title="🦐 Krillion link", description=description, color=0xBEBEFE
            ),
            ephemeral=True,
        )

    @commands.hybrid_command(
        name="krillionunlink",
        description="Unlink your krillion.io username so your answers are no longer shown.",
    )
    @app_commands.describe(member="Owner only: unlink someone else.")
    async def krillionunlink(
        self, context: Context, member: discord.User = None
    ) -> None:
        """
        Remove a Krillion link and the answers stored for it.

        :param context: The hybrid command context.
        :param member: Owner only: the member to unlink instead of the caller.
        """
        target = context.author
        if member is not None and member.id != context.author.id:
            if not await self.bot.is_owner(context.author):
                await context.send(
                    embed=discord.Embed(
                        title="Error!",
                        description="Only the bot owner can unlink someone else.",
                        color=0xE02B2B,
                    ),
                    ephemeral=True,
                )
                return
            target = member
        username = await self.bot.database.delete_krillion_link(target.id)
        if username is None:
            description = "There's no Krillion link to remove."
        else:
            description = (
                f"Unlinked Krillion user `@{username}`; their answers are no longer shown."
            )
        await context.send(
            embed=discord.Embed(
                title="🦐 Krillion link", description=description, color=0xBEBEFE
            ),
            ephemeral=True,
        )

    async def daily_post_day(self) -> int | None:
        """
        The day the daily auto-post covers: the one that last ended (yesterday,
        on the site's US Eastern clock). None if that day isn't archived.
        """
        yesterday = (datetime.now(RESET_TZ) - timedelta(days=1)).date().isoformat()
        stored = await self.bot.database.get_puzzle_info(self.GAME)
        return next((d for d, i in stored.items() if i.get("date") == yesterday), None)

    async def build_daily_post(
        self, channel, day: int | None = None
    ) -> tuple[discord.Embed, discord.File] | None:
        """
        A day's answers (by default ``daily_post_day``) from linked members who
        shared that day's result in this channel, for the daily auto-post: an
        embed showing a table image (prompts × players), and the image file to
        send with it. Reads the channel's cached results, so sync the channel
        first. Returns None unless at least ``MIN_DAILY_PLAYERS`` of them did.
        """
        guild = getattr(channel, "guild", None)
        if guild is None:
            return None
        if day is None:
            day = await self.daily_post_day()
        info = (await self.bot.database.get_puzzle_info(self.GAME)).get(day)
        if info is None:
            return None
        # Only linked members who shared their answers with friends and that
        # day's result in this channel, and only once at least two of them did.
        posted_here = {
            row["author_id"]
            for row in await self._load_all(channel.id)
            if row["payload"]["puzzle"] == day
        }
        players = {
            user_id: played
            for user_id, played in (await self._linked_days()).get(day, {}).items()
            if played.get("answers")
            and user_id in posted_here
            and guild.get_member(user_id) is not None
        }
        if len(players) < MIN_DAILY_PLAYERS:
            return None
        names = await self._resolve_names(
            guild, list(players), {uid: p["username"] for uid, p in players.items()}
        )
        order = sorted(players, key=lambda uid: -(players[uid].get("score") or 0))
        texts = info.get("prompts") or []
        prompts = [
            texts[slot] if slot < len(texts) else f"Round {slot + 1}"
            for slot in range(PROMPTS)
        ]
        columns = [
            {
                "name": names[uid],
                "score": players[uid].get("score"),
                "rounds": [_friend_round(players[uid], slot) for slot in range(PROMPTS)],
            }
            for uid in order
        ]
        # Pillow drawing is CPU-bound; keep it off the event loop.
        png = await asyncio.to_thread(
            render_day_table, day, info.get("date") or "", prompts, columns
        )
        filename = f"krillion-{day}.png"
        # Alt text, for screen readers.
        description = _short(
            f"Krillion #{day} answers: "
            + "; ".join(f"{c['name']} {c['score'] or 0} pts" for c in columns),
            1024,
        )
        file = discord.File(io.BytesIO(png), filename=filename, description=description)

        embed = discord.Embed(
            title=f"🦐 Krillion #{day} — the catch",
            description=" · ".join(
                f"**{c['name']}** {c['score'] or 0}" for c in columns
            ),
            color=0xBEBEFE,
        )
        embed.set_image(url=f"attachment://{filename}")
        embed.set_footer(text=f"{info.get('date')} · linked with /krillionlink")
        return embed, file

    # ------------------------------------------------------------------ #
    # Parsing
    # ------------------------------------------------------------------ #

    @staticmethod
    def _valid_score(score: int) -> bool:
        """Every reachable Krillion total is a multiple of 5, at most 700."""
        return 0 <= score <= MAX_SCORE and score % 5 == 0

    @staticmethod
    def _extract_answers(content: str) -> list[int] | None:
        """
        Find the 7-symbol result grid and return its per-prompt point values.

        The line must *start* with exactly seven answer symbols (spaces
        between them are fine); anything after them is ignored, so a comment
        on the grid line ("🏮⬛🫧🫧🐟🦑🏮 got screwed today") still counts.
        The 🦐 in the "Krillion #47 🦐" header can't pollute the grid, since
        that line starts with text. Returns None if no such line is present.
        """
        for line in content.splitlines():
            stripped = line.strip().replace("️", "")  # tolerate emoji VS16
            symbols = []
            for ch in stripped:
                if ch in VALUES:
                    symbols.append(ch)
                elif not ch.isspace():
                    break
            # More than seven leading symbols isn't a valid grid either.
            if len(symbols) != PROMPTS:
                continue
            return [VALUES[ch] for ch in symbols]
        return None

    def parse(self, content: str, posted_on: date):
        """
        Parse a Krillion result into ``(played_on, payload)`` for the cache.

        ``payload`` holds the ``puzzle`` (day) number, the ``score``, and
        ``answers`` — the 7 per-prompt point values when a grid was shared,
        else None. ``played_on`` is the post date (month attribution derives
        the real date from the day number later). Returns None if the message
        isn't a daily Krillion result.
        """
        header = HEADER_RE.search(content)
        if header is not None:
            puzzle = int(header.group(1))
            answers = self._extract_answers(content)
            if answers is not None:
                # The grid is authoritative over the stated score.
                return posted_on, {
                    "puzzle": puzzle,
                    "score": sum(answers),
                    "answers": answers,
                }
            # No grid: take the first score-only line after the header.
            for line in content[header.end():].splitlines():
                match = SCORE_LINE_RE.match(line)
                if match:
                    stated = int(match.group(1))
                    if self._valid_score(stated):
                        return posted_on, {
                            "puzzle": puzzle,
                            "score": stated,
                            "answers": None,
                        }
                    return None
            return None

        # Bare form: strictly the whole message, and a plausible score, so
        # ordinary chat like "#1\n2026" can't sneak in.
        bare = BARE_RE.fullmatch(content.strip())
        if bare:
            stated = int(bare.group(2))
            if self._valid_score(stated):
                return posted_on, {
                    "puzzle": int(bare.group(1)),
                    "score": stated,
                    "answers": None,
                }
        return None

    @commands.hybrid_command(
        name="krillion",
        description="Tally Krillion scores in this channel and show a leaderboard.",
    )
    @app_commands.describe(
        month="Month to tally, e.g. 'August', 'Aug 2026' or '2026-08'. Defaults to the previous full month.",
    )
    async def krillion(self, context: Context, *, month: str = None) -> None:
        """
        Tally the current channel/thread's Krillion results and post a leaderboard.

        :param context: The hybrid command context.
        :param month: Optional month to tally; defaults to the previous full month.
        """
        window = self._resolve_window(month)
        if window is None:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        f"Couldn't understand the month `{month}`.\n"
                        "Try something like `August`, `Aug 2026` or `2026-08`."
                    ),
                    color=0xE02B2B,
                )
            )
            return

        after, label, month_filter = window

        # Bringing the cache up to date can take a while on the first scan.
        await context.defer()

        try:
            # Scan a little before the month starts so a day posted just before
            # the boundary is still cached. Which month a result *counts*
            # toward is decided by its day number, not the post date.
            await self._sync_channel(context.channel, after - self.SCAN_BUFFER)
        except discord.Forbidden:
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description=(
                        "I don't have permission to read the history in this channel.\n\n"
                        "Please give me the **View Channel** and **Read Message History** "
                        "permissions here (check the channel-specific permission overrides), "
                        "then try again."
                    ),
                    color=0xE02B2B,
                )
            )
            return

        embed = await self.build_leaderboard(context.channel, month_filter, label)
        if embed is None:
            await context.send(
                embed=discord.Embed(
                    title="🦐 Krillion Leaderboard",
                    description=(
                        f"No Krillion results found for **{label}** in this channel.\n\n"
                        "Make sure results are posted here and that I can read message "
                        "history (the `message_content` intent must be enabled)."
                    ),
                    color=0xE02B2B,
                )
            )
            return
        await context.send(embed=embed)

    @krillion.autocomplete("month")
    async def krillion_month_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return month_choices(current)

    async def build_leaderboard(
        self, channel, month_filter, label: str
    ) -> discord.Embed | None:
        """
        Build the month's leaderboard embed from the cache.

        Shared by the /krillion command and the monthly auto-poster. Returns
        None when the channel has no results for the month.
        """
        # Attribute each result to a month by its day number's real date (see
        # Catfishing: same mechanism, shared via the base class).
        all_rows = await self._load_all(channel.id)
        anchor = self._date_anchor(all_rows)
        rows = []
        for row in all_rows:
            played_on = self._puzzle_date(
                row["payload"]["puzzle"], anchor, row["played_on"]
            )
            if (played_on.year, played_on.month) == month_filter:
                rows.append({**row, "played_on": played_on})

        # Resolve each player's current server nickname from their stored id, so
        # names stay right even after someone renames (stored name is fallback).
        names = await self._resolve_names(
            getattr(channel, "guild", None),
            [row["author_id"] for row in rows],
            {row["author_id"]: row["author_name"] for row in rows},
        )

        # players[author_id] = {"name", "scores": {puzzle: score},
        #                       "answers": {puzzle: [values] | None}}
        players: dict[int, dict] = defaultdict(
            lambda: {"name": "", "scores": {}, "answers": {}}
        )
        puzzles: set[int] = set()

        for row in rows:
            puzzle = row["payload"]["puzzle"]
            score = row["payload"]["score"]
            entry = players[row["author_id"]]
            entry["name"] = names[row["author_id"]]
            puzzles.add(puzzle)
            # Keep the best score if someone posts the same day more than once.
            existing = entry["scores"].get(puzzle)
            if existing is None or score > existing:
                entry["scores"][puzzle] = score
                entry["answers"][puzzle] = row["payload"].get("answers")

        if not players:
            return None

        ranking = sorted(
            players.values(),
            key=lambda e: (sum(e["scores"].values()), len(e["scores"])),
            reverse=True,
        )

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, entry in enumerate(ranking):
            rank = medals[i] if i < len(medals) else f"`#{i + 1}`"
            scores = entry["scores"].values()
            days = len(scores)
            total = sum(scores)
            average = total / days
            catches = Counter()
            for answers in entry["answers"].values():
                if answers:
                    catches.update(answers)
            lines.append(
                f"{rank} **{entry['name']}** — {total:,} pts "
                f"({days} day{'s' if days != 1 else ''}, avg {average:,.0f}) · "
                f"{SHRIMP}{catches[VALUES[SHRIMP]]} "
                f"{LANTERN}{catches[VALUES[LANTERN]]} "
                f"{SQUID}{catches[VALUES[SQUID]]}"
            )

        embed = discord.Embed(
            title="🦐 Krillion Leaderboard",
            description="\n".join(lines),
            color=0xBEBEFE,
        )

        # Group question averages: for every day at least two players shared a
        # grid for, average each prompt's points, then surface the questions
        # the group collectively aced and flunked. Needs the prompt texts, so
        # only days that were archived (from the bot's deployment on) qualify.
        prompt_info = await self.bot.database.get_puzzle_info(self.GAME)
        # Linked players' own answers, to narrow a prompt with several star
        # (or too smart) answers down to the ones actually given here.
        linked = await self._linked_days()
        grids: dict[int, list[list[int]]] = defaultdict(list)
        for entry in players.values():
            for puzzle, answers in entry["answers"].items():
                if answers:
                    grids[puzzle].append(answers)
        # (average, text, day, [star answers], [too smart answers])
        questions = []
        for puzzle, all_answers in grids.items():
            info = prompt_info.get(puzzle) or {}
            texts = info.get("prompts") or []
            if len(all_answers) < 2:
                continue
            given_days = [
                linked.get(puzzle, {}).get(author_id) for author_id in players
            ]
            for slot, text in enumerate(texts[:PROMPTS]):
                average = sum(answers[slot] for answers in all_answers) / len(
                    all_answers
                )
                # Only name the shrimp answer(s) when someone here actually
                # caught it, so we don't reveal answers nobody in the channel
                # found. (A prompt can have more than one star answer.)
                got_shrimp = any(
                    answers[slot] == VALUES[SHRIMP] for answers in all_answers
                )
                stars = (
                    _narrow(
                        _star_answers(info, slot),
                        _given_answers(given_days, slot, SHRIMP_POINTS),
                    )
                    if got_shrimp
                    else []
                )
                # Same rule for the too smart answer, shown on worst questions.
                got_too_smart = any(
                    answers[slot] == VALUES[TOO_SMART] for answers in all_answers
                )
                clever = (
                    _narrow(
                        _too_smart_answers(info, slot),
                        _given_answers(given_days, slot, TOO_SMART_POINTS),
                    )
                    if got_too_smart
                    else []
                )
                questions.append((average, text, puzzle, stars, clever))

        def _line(average, text, puzzle, stars, clever=()) -> str:
            star_note = f" — {SHRIMP} {_star_note(stars, 30)}" if stars else ""
            clever_note = (
                f" — {TOO_SMART} {_star_note(clever, 30)}" if clever else ""
            )
            return (
                f"• {_short(text)} — avg {average:,.0f} pts (#{puzzle})"
                f"{star_note}{clever_note}"
            )

        if questions:
            questions.sort(key=lambda q: (-q[0], q[2]))
            best = questions[:3]
            # Worst from the other end, never overlapping the best list.
            worst = sorted(questions[3:], key=lambda q: (q[0], q[2]))[:3]
            embed.add_field(
                name="💪 Best questions (group average)",
                value="\n".join(_line(*q[:4]) for q in best),
                inline=False,
            )
            if worst:
                embed.add_field(
                    name="😰 Worst questions (group average)",
                    value="\n".join(_line(*q) for q in worst),
                    inline=False,
                )

        embed.set_footer(
            text=f"Period: {label} • {len(ranking)} players • {len(puzzles)} days played"
        )
        return embed

    async def compare_stats(self, rows: list[dict], author_id: int) -> list[str] | None:
        """
        Comparative stats for one player against everyone in ``rows``.

        Returns formatted lines for the /mystats embed, or None if the player
        has no cached Krillion results. Includes a tally of every answer
        quality they've caught (⬛ non-submissions aren't counted).
        """
        # Best score per player per day, keeping that share's answer grid.
        best: dict[tuple, int] = {}
        answers_kept: dict[tuple, list | None] = {}
        for row in rows:
            key = (row["author_id"], row["payload"]["puzzle"])
            score = row["payload"]["score"]
            if key not in best or score > best[key]:
                best[key] = score
                answers_kept[key] = row["payload"].get("answers")
        mine = {p: score for (pid, p), score in best.items() if pid == author_id}
        if not mine:
            return None

        by_puzzle: dict = defaultdict(dict)
        for (pid, puzzle), score in best.items():
            by_puzzle[puzzle][pid] = score
        shared = [p for p in mine if len(by_puzzle[p]) > 1]
        wins = sum(1 for p in shared if mine[p] == max(by_puzzle[p].values()))

        my_avg = sum(mine.values()) / len(mine)
        channel_avg = sum(best.values()) / len(best)

        lines = [
            f"Days played: **{len(mine)}**",
            f"🏆 Top score of the day: **{wins}** of {len(shared)} shared days",
            f"📈 Average: **{my_avg:,.0f}** vs the channel's {channel_avg:,.0f}",
        ]

        # Tally of every answer quality across their counted grids.
        catches = Counter()
        for puzzle in mine:
            answers = answers_kept[(author_id, puzzle)]
            if answers:
                catches.update(answers)
        if catches:
            lines.append("### Answer counts")
            for symbol, points in (
                (SHRIMP, 100), (LANTERN, 85), (SQUID, 60), ("🐟", 30),
                (TOO_SMART, 15), ("🫧", 10),
            ):
                # Too smart answers are rare, so only show that row once caught.
                if symbol == TOO_SMART and not catches[points]:
                    continue
                lines.append(f"{symbol} {points} pts — **{catches[points]}**")

        # Which questions they caught a shrimp (or a too smart answer) on,
        # naming the answer where it's known. Usually a prompt has a single
        # such answer, so the catch pins it down; where a prompt has several we
        # list them all with "/", unless the player linked their Krillion
        # account and their own answer that day is stored. Needs the archived
        # sheet, so only archived days can be named.
        prompt_info = await self.bot.database.get_puzzle_info(self.GAME)
        linked = await self._linked_days()
        for symbol, title, lookup in (
            (SHRIMP, "Shrimp catches", _star_answers),
            (TOO_SMART, "Too smart catches", _too_smart_answers),
        ):
            caught = []
            for puzzle in sorted(mine):
                answers = answers_kept[(author_id, puzzle)]
                info = prompt_info.get(puzzle) or {}
                texts = info.get("prompts") or []
                if not answers:
                    continue
                for slot, value in enumerate(answers):
                    if value != VALUES[symbol] or slot >= len(texts):
                        continue
                    own_day = linked.get(puzzle, {}).get(author_id)
                    named_answers = _narrow(
                        lookup(info, slot),
                        _given_answers([own_day], slot, VALUES[symbol]),
                    )
                    named = (
                        f" → **{_star_note(named_answers)}**" if named_answers else ""
                    )
                    caught.append(f"• {_short(texts[slot])}{named} (#{puzzle})")
            if caught:
                # /mystats shortens these lists if the embed would get too long.
                lines.append(f"### {title} {symbol}")
                lines.append(Trimmable(caught))
        return lines


async def setup(bot) -> None:
    await bot.add_cog(Krillion(bot))
