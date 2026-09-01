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

Pack results are NOT counted: an emoji between the name and the number
("Krillion 🎬 #1 🦐") marks a pack rather than the daily game, and those don't
match the daily patterns.

Each grid symbol is one prompt's answer quality:
⬛ no submission (0) · 🫧 bubbles (10) · 🐟 fish (30) · 🦑 squid (60) ·
🏮 lanternfish (85) · 🦐 shrimp (100), so a perfect day is 700. When a grid is
present it's authoritative (the stated score is ignored); the bare form is
validated strictly (0-700, multiple of 5) so ordinary chat can't match.

Like Catfishing, shares carry a day number instead of a date, so results are
attributed to months via the shared puzzle-number anchor (see
``leaderboard.base``). Parsed results are cached in the database; the command
does an incremental catch-up scan and reads its aggregates from there.
"""

import asyncio
import re
from collections import Counter, defaultdict
from datetime import date, time, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Context

from leaderboard.base import LeaderboardCog, month_choices

# Answer symbol -> points. ⬛ means "no submission".
VALUES = {"⬛": 0, "🫧": 10, "🐟": 30, "🦑": 60, "🏮": 85, "🦐": 100}
PROMPTS = 7
MAX_SCORE = PROMPTS * 100

# The catch symbols counted on the leaderboard, best first.
SHRIMP, LANTERN, SQUID = "🦐", "🏮", "🦑"

# Matches the daily header, e.g. "Krillion #47 🦐". Only whitespace may sit
# between the name and the number: pack results put the pack's emoji there
# ("Krillion 🎬 #1 🦐"), and packs aren't the daily game.
HEADER_RE = re.compile(r"\bkrillion\s*#(\d+)", re.IGNORECASE)

# The bare form: the whole message is just "#day" then the score.
BARE_RE = re.compile(r"#(\d+)\s+(\d+)")

# A line consisting solely of a number (the day's total score).
SCORE_LINE_RE = re.compile(r"^\s*(\d+)\s*$")

# The site only serves the *current* day's data — past days are refused to
# everyone (it's date-gated, not login-gated) — so the bot archives each day
# as it appears (daily task + startup catch-up). `/api/today` gives the day
# number; `/api/reveal?date=<today>` gives the full answer sheet, publicly and
# without auth, from which we keep each prompt's single shrimp (score-100)
# answer.
TODAY_URL = "https://krillion.io/api/today"
REVEAL_URL = "https://krillion.io/api/reveal?date={date}"
# When the daily fetch runs (UTC; the puzzle resets at 04:00 UTC).
FETCH_AT = time(hour=12, tzinfo=timezone.utc)
# The points value that marks a shrimp answer.
SHRIMP_POINTS = 100


def _short(text: str, limit: int = 60) -> str:
    """Trim a prompt text so embed fields stay within Discord's limits."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Krillion(LeaderboardCog, name="krillion"):
    GAME = "krillion"

    def __init__(self, bot) -> None:
        super().__init__(bot)
        self._archived_on_boot = False

    async def cog_load(self) -> None:
        self.prompt_archiver.start()

    async def cog_unload(self) -> None:
        self.prompt_archiver.cancel()

    # ------------------------------------------------------------------ #
    # Prompt archive
    # ------------------------------------------------------------------ #

    @staticmethod
    def _shrimp_from_reveal(reveal) -> list[str] | None:
        """
        Pull each prompt's single shrimp (score-100) answer from a reveal sheet.

        Returns a list of ``PROMPTS`` entries (the answer text, or None where a
        prompt somehow has no score-100 answer), or None if the sheet doesn't
        have the expected shape.
        """
        if not isinstance(reveal, dict):
            return None
        prompts = reveal.get("prompts")
        if not isinstance(prompts, list) or len(prompts) != PROMPTS:
            return None
        shrimp: list[str | None] = []
        for prompt in prompts:
            answer = next(
                (
                    a.get("answer")
                    for a in (prompt.get("answers") or [])
                    if a.get("score") == SHRIMP_POINTS
                ),
                None,
            )
            shrimp.append(answer)
        return shrimp

    async def _store_today(self, today, reveal) -> bool:
        """
        Validate and store one day's archive from the two API payloads.

        ``today`` supplies the day number and prompt texts; ``reveal`` (may be
        None if that fetch failed) supplies the per-prompt shrimp answers.
        Returns True if stored.
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
        shrimp = self._shrimp_from_reveal(reveal)
        if shrimp is not None:
            payload["shrimp"] = shrimp
        await self.bot.database.set_puzzle_info(self.GAME, day, payload)
        return True

    @staticmethod
    async def _get_json(session, url):
        """GET a URL and return parsed JSON, or None on any failure."""
        try:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    async def _archive_today(self) -> None:
        """Fetch and archive today's puzzle; failures just try again later."""
        async with aiohttp.ClientSession() as session:
            today = await self._get_json(session, TODAY_URL)
            reveal = None
            if isinstance(today, dict) and today.get("date"):
                # The reveal sheet is public for the current day and carries the
                # shrimp answers; a real player can't fetch past days either.
                reveal = await self._get_json(
                    session, REVEAL_URL.format(date=today["date"])
                )
        if await self._store_today(today, reveal):
            extra = "" if reveal else " (prompts only; answer sheet unavailable)"
            self.bot.logger.info(f"Archived today's Krillion puzzle{extra}")

    @tasks.loop(time=FETCH_AT)
    async def prompt_archiver(self) -> None:
        await self._archive_today()

    @prompt_archiver.before_loop
    async def before_prompt_archiver(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Catch-up fetch on startup: prompts are only available on the day, so
        # a bot that was asleep at FETCH_AT still archives today's on boot.
        if self._archived_on_boot:
            return
        self._archived_on_boot = True
        await self._archive_today()

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

        Only a line made up entirely of answer symbols counts, so the 🦐 in the
        "Krillion #47 🦐" header can't pollute the grid. Returns None if no
        such line is present.
        """
        for line in content.splitlines():
            stripped = line.strip().replace("️", "")  # tolerate emoji VS16
            if not stripped:
                continue
            symbols = [ch for ch in stripped if ch in VALUES]
            others = [ch for ch in stripped if ch not in VALUES and not ch.isspace()]
            if others or len(symbols) != PROMPTS:
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
        grids: dict[int, list[list[int]]] = defaultdict(list)
        for entry in players.values():
            for puzzle, answers in entry["answers"].items():
                if answers:
                    grids[puzzle].append(answers)
        questions = []  # (average, text, day, shrimp answer or None)
        for puzzle, all_answers in grids.items():
            info = prompt_info.get(puzzle) or {}
            texts = info.get("prompts") or []
            shrimp_answers = info.get("shrimp") or []
            if len(all_answers) < 2:
                continue
            for slot, text in enumerate(texts[:PROMPTS]):
                average = sum(answers[slot] for answers in all_answers) / len(
                    all_answers
                )
                # Only name the shrimp answer when someone here actually caught
                # it, so we don't reveal answers nobody in the channel found.
                got_shrimp = any(
                    answers[slot] == VALUES[SHRIMP] for answers in all_answers
                )
                ideal = (
                    shrimp_answers[slot]
                    if got_shrimp and slot < len(shrimp_answers)
                    else None
                )
                questions.append((average, text, puzzle, ideal))

        def _line(average, text, puzzle, ideal) -> str:
            ideal_note = f" — 🦐 {_short(ideal, 30)}" if ideal else ""
            return f"• {_short(text)} — avg {average:,.0f} pts (#{puzzle}){ideal_note}"

        if questions:
            questions.sort(key=lambda q: (-q[0], q[2]))
            best = questions[:3]
            # Worst from the other end, never overlapping the best list.
            worst = sorted(questions[3:], key=lambda q: (q[0], q[2]))[:3]
            embed.add_field(
                name="💪 Best questions (group average)",
                value="\n".join(_line(*q) for q in best),
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
                (SHRIMP, 100), (LANTERN, 85), (SQUID, 60), ("🐟", 30), ("🫧", 10),
            ):
                lines.append(f"{symbol} {points} pts — **{catches[points]}**")

        # Which questions they caught a shrimp on, naming the specific answer
        # (each prompt has a single shrimp answer, so a shrimp on that prompt
        # means they gave it). Needs the archived sheet, so only days from the
        # bot's deployment onward can be named.
        prompt_info = await self.bot.database.get_puzzle_info(self.GAME)
        shrimps = []
        for puzzle in sorted(mine):
            answers = answers_kept[(author_id, puzzle)]
            info = prompt_info.get(puzzle) or {}
            texts = info.get("prompts") or []
            shrimp_answers = info.get("shrimp") or []
            if not answers:
                continue
            for slot, value in enumerate(answers):
                if value != VALUES[SHRIMP] or slot >= len(texts):
                    continue
                answer = shrimp_answers[slot] if slot < len(shrimp_answers) else None
                named = f" → **{_short(answer, 40)}**" if answer else ""
                shrimps.append(f"• {_short(texts[slot])}{named} (#{puzzle})")
        if shrimps:
            lines.append("### Shrimp catches 🦐")
            lines += shrimps[:8]
            if len(shrimps) > 8:
                lines.append(f"…and {len(shrimps) - 8} more")
        return lines


async def setup(bot) -> None:
    await bot.add_cog(Krillion(bot))
