"""
Catfishing leaderboard cog for Kennel-LeaderBot.

Tallies shared Catfishing results in the channel/thread the command is invoked
in — each person's monthly scores with totals, averages and personal bests —
and reports how many days the group collectively got every question right.

Expected message format (as copy-pasted from catfishing.net):

    catfishing.net
    #723 - 4/10
    🐟🐟🐟🐟🐈
    🐈🐈🐟🐟🐈

    catfishing dot net
    694 - 3.5/10
    🐟🐟🐟🥚🐈
    🐟🐈🐟🐟🐈

The site can optionally append a spoilered list of wrong answers, which is
ignored:

    catfishing.net
    #829 - 9.5/10 🎉
    🐈🐈🐈🐈🐈
    🐈🐈🐈🐈🥚

    ||Q10 🥚 three monkeys||

The grid may also be given as text, with C=cat, F=fish, E=egg::

    723 - 4/10
    FFFFC
    CCFFC

A 🐈 (cat) is a correct answer, a 🐟 (fish) is a wrong answer, and a 🥚 (egg)
is one the player marked as "close enough". A score is one point per cat plus
half a point per egg, out of 10 questions (two rows of five).

Parsed results are cached in the database (see ``leaderboard.base``); the
command does an incremental catch-up scan and reads its aggregates from there
rather than re-scanning the whole channel each time.
"""

import asyncio
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Context

from leaderboard.base import LeaderboardCog, month_choices

# The three result symbols. Cats are correct, eggs are "close enough", fish are
# wrong. Cats and eggs both count as the group getting that question.
CAT = "🐈"
FISH = "🐟"
EGG = "🥚"
SYMBOLS = (CAT, FISH, EGG)
QUESTIONS = 10

# Matches the puzzle/score line, e.g. "#725 - 4/10" or "694 - 3.5/10".
# Group 1 is the puzzle number, group 2 is the stated score.
SCORE_RE = re.compile(r"#?(\d+)\s*-\s*(\d+(?:\.\d+)?)\s*/\s*10\b")

# Per-puzzle stats endpoint. The `day` query param is the puzzle number shown
# in the shared result (e.g. "#714" -> day=714).
API_URL = "https://catfishing.net/api/game?day={day}"

# Puzzle stats are stored in the database (``puzzle_info``) so a restart
# doesn't mean re-fetching them all. The global solve rates keep moving while
# a puzzle is new, so a stored puzzle is re-fetched once it's 24 hours old and
# again at 7 days old, after which it's treated as settled.
REFRESH_AFTER = (timedelta(hours=24), timedelta(days=7))
# Puzzle numbers are a daily sequence: #836 is the puzzle for 2026-10-07.
# A puzzle comes out at midnight in the earliest timezone (UTC+14), i.e. 14
# hours before its date starts in UTC.
DATE_ANCHOR = date(2026, 10, 7).toordinal() - 836
RELEASE_LEAD = timedelta(hours=14)
# Throttling for catfishing.net, which answers HTTP 429 once requests come
# in too fast (seen at ~6/s; ~1.8/s one at a time was fine). Never more than
# this many requests at once (across all commands), and for big batches a
# pause after each request, keeping them to ~1.4/s — a cold /mystats is
# slower, but only once, since the results are stored.
MAX_CONCURRENT_FETCHES = 3
BIG_BATCH = 10
BIG_BATCH_DELAY = 2.0
# If rate-limited anyway, every fetch pauses (for the site's Retry-After, or
# this long if it doesn't say) and the request is retried a few times.
RATE_LIMIT_PAUSE = 15.0
RATE_LIMIT_RETRIES = 3


class RateLimited(Exception):
    """catfishing.net answered HTTP 429; ``retry_after`` is how long to wait."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after

# Posted the moment a puzzle's group coverage reaches all 10 questions.
GROUP_COMPLETE_MESSAGE = "🎉🐈🔟🐈🎉"
# How many of the hardest answers to show.
HARDEST_COUNT = 5


def _fmt(score: float) -> str:
    """Format a score without a trailing ``.0`` (e.g. 4, 3.5)."""
    return f"{score:g}"


class Catfishing(LeaderboardCog, name="catfishing"):
    GAME = "catfishing"

    def __init__(self, bot) -> None:
        super().__init__(bot)
        # In-memory mirror of the stored puzzle stats, keyed by puzzle number:
        # {day: {"titles", "rates", "fetched_at"}}. Loaded from the database
        # on first use and written through on every fetch.
        self._puzzle_cache: dict[int, dict] | None = None
        self._fetch_slots = asyncio.Semaphore(MAX_CONCURRENT_FETCHES)
        # When catfishing.net rate-limits us, no fetch starts before this
        # (event-loop time).
        self._paused_until = 0.0

    async def cog_load(self) -> None:
        self.stats_refresher.start()

    async def cog_unload(self) -> None:
        self.stats_refresher.cancel()

    async def _fetch_puzzle(self, session: aiohttp.ClientSession, day: int):
        """
        Fetch a puzzle's stats from catfishing.net.

        Returns ``(titles, rates)`` — parallel lists where ``titles[i]`` is the
        answer for question ``i`` and ``rates[i]`` is the global percentage of
        players who got it right (lower = harder). Returns None on any failure,
        except a rate limit, which raises ``RateLimited``.
        """
        try:
            async with session.get(
                API_URL.format(day=day),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 429:
                    try:
                        retry_after = float(resp.headers.get("Retry-After", ""))
                    except ValueError:
                        retry_after = RATE_LIMIT_PAUSE
                    raise RateLimited(retry_after)
                if resp.status != 200:
                    self.bot.logger.warning(
                        f"catfishing.net returned HTTP {resp.status} for puzzle #{day}"
                    )
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self.bot.logger.warning(
                f"Couldn't fetch catfishing.net puzzle #{day}: {e!r}"
            )
            return None

        articles = data.get("articles") or []
        stats_articles = (data.get("stats") or {}).get("articles") or []
        titles = [a.get("title") for a in articles]
        rates = [sa.get("correctRate") for sa in stats_articles]
        return titles, rates

    # ------------------------------------------------------------------ #
    # Stored puzzle stats
    # ------------------------------------------------------------------ #

    @staticmethod
    def _released_at(day: int) -> datetime:
        """When puzzle ``day`` came out (midnight UTC+14 on its date)."""
        midnight = datetime.combine(
            date.fromordinal(day + DATE_ANCHOR), time(0), tzinfo=timezone.utc
        )
        return midnight - RELEASE_LEAD

    @classmethod
    def _refresh_due(cls, day: int, stored: dict, now: datetime) -> bool:
        """
        Whether a stored puzzle should be re-fetched: it was fetched before
        the 24-hour or 7-day mark and that mark has now passed.
        """
        fetched_at = datetime.fromisoformat(stored["fetched_at"])
        released = cls._released_at(day)
        return any(
            fetched_at < released + after <= now for after in REFRESH_AFTER
        )

    async def _load_stored(self) -> dict[int, dict]:
        """The stored puzzle stats, loaded from the database once."""
        if self._puzzle_cache is None:
            self._puzzle_cache = await self.bot.database.get_puzzle_info(self.GAME)
        return self._puzzle_cache

    async def _fetch_and_store(self, days: list[int]) -> None:
        """
        Fetch ``days`` from catfishing.net and store whatever succeeds.

        At most ``MAX_CONCURRENT_FETCHES`` requests run at once across the
        whole cog; a big batch also pauses after each request, so it takes
        longer rather than hammering the site. A rate limit pauses every
        fetch for as long as the site asks, then retries.
        """
        if not days:
            return
        stored = await self._load_stored()
        delay = BIG_BATCH_DELAY if len(days) > BIG_BATCH else 0
        loop = asyncio.get_running_loop()

        async def fetch_one(session, day):
            result = None
            async with self._fetch_slots:
                for attempt in range(RATE_LIMIT_RETRIES + 1):
                    wait = self._paused_until - loop.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    try:
                        result = await self._fetch_puzzle(session, day)
                        break
                    except RateLimited as limited:
                        if attempt == RATE_LIMIT_RETRIES:
                            self.bot.logger.warning(
                                f"catfishing.net kept rate-limiting puzzle #{day}; "
                                "giving up for now"
                            )
                            break
                        now = loop.time()
                        if self._paused_until <= now:  # log each pause once
                            self.bot.logger.warning(
                                "catfishing.net is rate-limiting; pausing fetches "
                                f"for {limited.retry_after:g}s"
                            )
                        self._paused_until = max(
                            self._paused_until, now + limited.retry_after
                        )
                if delay:
                    await asyncio.sleep(delay)
            if result is None:
                return
            titles, rates = result
            payload = {
                "titles": titles,
                "rates": rates,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            await self.bot.database.set_puzzle_info(self.GAME, day, payload)
            stored[day] = payload

        async with aiohttp.ClientSession() as session:
            await asyncio.gather(*(fetch_one(session, day) for day in days))

    async def _puzzle_stats(self, days) -> dict[int, tuple | None]:
        """
        ``(titles, rates)`` for each puzzle in ``days``, or None where it
        couldn't be fetched. Uses the stored stats, fetching only the
        puzzles that aren't stored yet.
        """
        stored = await self._load_stored()
        await self._fetch_and_store([day for day in days if day not in stored])
        return {
            day: (stored[day]["titles"], stored[day]["rates"]) if day in stored else None
            for day in days
        }

    @tasks.loop(hours=1)
    async def stats_refresher(self) -> None:
        """Re-fetch stored puzzles that have reached their 24h or 7-day mark."""
        stored = await self._load_stored()
        now = datetime.now(timezone.utc)
        due = sorted(
            day for day, info in stored.items() if self._refresh_due(day, info, now)
        )
        if due:
            await self._fetch_and_store(due)
            self.bot.logger.info(
                f"Refreshed catfishing.net stats for {len(due)} puzzle(s)"
            )

    @stats_refresher.before_loop
    async def before_stats_refresher(self) -> None:
        await self.bot.wait_until_ready()

    @staticmethod
    def _score_grid(grid, cat, egg):
        """
        Score a sequence of result markers.

        Returns ``(score, correct_positions)`` where ``score`` is one point per
        cat plus half a point per egg, and ``correct_positions`` is the list of
        indexes that were a cat or egg.
        """
        score = sum(1 for s in grid if s == cat) + 0.5 * sum(1 for s in grid if s == egg)
        correct = [i for i, s in enumerate(grid) if s in (cat, egg)]
        return score, correct

    @classmethod
    def _extract_grid(cls, content: str):
        """
        Find the result grid in a message and score it.

        Handles the emoji grid (🐈/🐟/🥚) and the text grid (C=cat, F=fish,
        E=egg), e.g.::

            FFFFC
            CCFFC

        Returns ``(score, correct_positions)`` or None if no 10-marker grid
        is present.
        """
        # Emoji grid: only lines made up entirely of grid symbols count, so the
        # site's optional spoilered wrong-answer list ("||Q7 🐟 Agincourt||")
        # and other chat can't add stray markers. Spoiler bars and emoji VS16
        # are tolerated on grid lines.
        symbols = []
        for line in content.splitlines():
            stripped = line.replace("|", "").replace("️", "").strip()
            if stripped and all(ch in SYMBOLS or ch.isspace() for ch in stripped):
                symbols += [ch for ch in stripped if ch in SYMBOLS]
        if len(symbols) == QUESTIONS:
            return cls._score_grid(symbols, CAT, EGG)

        # Text grid: only consider lines made up entirely of C/F/E markers, so
        # ordinary words can't be mistaken for a grid.
        letters = "".join(
            line.strip()
            for line in content.splitlines()
            if re.fullmatch(r"[CFE]+", line.strip())
        )
        if len(letters) == QUESTIONS:
            return cls._score_grid(letters, "C", "E")

        return None

    def parse(self, content: str, posted_on: date):
        """
        Parse a Catfishing result into ``(played_on, payload)`` for the cache.

        ``payload`` holds the ``puzzle`` number, the ``score`` (cats + half-eggs)
        and the ``correct`` question indexes (0-9, cat or egg). ``played_on`` is
        the post date. Returns None if the message isn't a result.
        """
        match = SCORE_RE.search(content)
        if match is None:
            return None

        grid = self._extract_grid(content)
        if grid is None:
            return None

        puzzle = int(match.group(1))
        score, correct = grid
        return posted_on, {"puzzle": puzzle, "score": score, "correct": correct}

    async def on_result_captured(self, message, played_on, payload) -> None:
        """
        Celebrate a group 10/10 the moment it happens.

        When a freshly posted result is the one that completes the group's
        coverage of a puzzle (every question answered by *someone*, cats and
        eggs both counting), post a small emoji-only congratulations. Only
        fires for live messages — history scans replaying old completions
        never end up here — and only for the completing message, so it can't
        double-post when later results re-cover already-covered questions.
        """
        mine = set(payload["correct"])
        if not mine:
            return
        puzzle = payload["puzzle"]
        rows = await self._load_all(message.channel.id)
        covered_before = set().union(
            set(),
            *(
                row["payload"]["correct"]
                for row in rows
                if row["payload"]["puzzle"] == puzzle
                and row["message_id"] != message.id
            ),
        )
        if len(covered_before) >= QUESTIONS:
            return  # already complete before this message
        if len(covered_before | mine) < QUESTIONS:
            return  # still not complete
        try:
            await message.channel.send(GROUP_COMPLETE_MESSAGE)
        except discord.HTTPException:
            pass  # can't send here; the leaderboard still counts it

    @commands.hybrid_command(
        name="catfishing",
        description="Tally Catfishing scores in this channel and show a leaderboard.",
    )
    @app_commands.describe(
        month="Month to tally, e.g. 'June', 'Jun 2026' or '2026-06'. Defaults to the previous full month.",
    )
    async def catfishing(self, context: Context, *, month: str = None) -> None:
        """
        Tally the current channel/thread's Catfishing results and post a leaderboard.

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
                        "Try something like `June`, `Jun 2026` or `2026-06`."
                    ),
                    color=0xE02B2B,
                )
            )
            return

        after, label, month_filter = window

        # Bringing the cache up to date can take a while on the first scan.
        await context.defer()

        try:
            # Scan a little before the month starts so a puzzle posted just
            # before the boundary is still cached. Which month a result *counts*
            # toward is decided below by the puzzle number, not the post date.
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
                    title="🐈 Catfishing Leaderboard",
                    description=(
                        f"No Catfishing results found for **{label}** in this channel.\n\n"
                        "Make sure results are posted here and that I can read message "
                        "history (the `message_content` intent must be enabled)."
                    ),
                    color=0xE02B2B,
                )
            )
            return
        await context.send(embed=embed)

    @catfishing.autocomplete("month")
    async def catfishing_month_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return month_choices(current)

    async def build_leaderboard(
        self, channel, month_filter, label: str
    ) -> discord.Embed | None:
        """
        Build the month's leaderboard embed from the cache.

        Shared by the /catfishing command and the monthly auto-poster. Returns
        None when the channel has no results for the month.
        """
        # Load every cached result and attribute each to a month by its puzzle
        # number's real date, rather than the post date. This keeps boundary and
        # catch-up posts in the month they were actually played, and stops a
        # puzzle from a neighbouring month being counted here by accident.
        all_rows = await self._load_all(channel.id)
        anchor = self._date_anchor(all_rows)
        rows = []
        for row in all_rows:
            played_on = self._puzzle_date(
                row["payload"]["puzzle"], anchor, row["played_on"]
            )
            if (played_on.year, played_on.month) == month_filter:
                # Carry the derived date forward as the result's played-on date.
                rows.append({**row, "played_on": played_on})

        # Resolve each player's current server nickname from their stored id, so
        # names stay right even after someone renames (stored name is fallback).
        names = await self._resolve_names(
            getattr(channel, "guild", None),
            [row["author_id"] for row in rows],
            {row["author_id"]: row["author_name"] for row in rows},
        )

        # players[author_id] = {"name": str, "scores": {puzzle: score}}
        players: dict[int, dict] = defaultdict(lambda: {"name": "", "scores": {}})
        # group_correct[puzzle] = set of question indexes the group got (cat/egg)
        group_correct: dict[int, set] = defaultdict(set)
        # puzzle_date[puzzle] = the (earliest) date that puzzle was posted
        puzzle_date: dict[int, date] = {}
        # solvers[puzzle][question_index] = set of names who got that question
        solvers: dict[int, dict[int, set]] = defaultdict(lambda: defaultdict(set))

        for row in rows:
            played_on = row["played_on"]
            puzzle = row["payload"]["puzzle"]
            score = row["payload"]["score"]
            correct = set(row["payload"]["correct"])
            display = names[row["author_id"]]

            entry = players[row["author_id"]]
            entry["name"] = display
            # Keep the best score if someone posts the same puzzle twice.
            existing = entry["scores"].get(puzzle)
            if existing is None or score > existing:
                entry["scores"][puzzle] = score
            # The group "gets" a question if anyone got it right.
            group_correct[puzzle] |= correct
            for position in correct:
                solvers[puzzle][position].add(display)
            existing_date = puzzle_date.get(puzzle)
            if existing_date is None or played_on < existing_date:
                puzzle_date[puzzle] = played_on

        if not players:
            return None

        # Build per-player stats and rank by total, then by days played.
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
            best = max(scores)
            lines.append(
                f"{rank} **{entry['name']}** — {_fmt(total)} pts "
                f"({days} day{'s' if days != 1 else ''}, "
                f"avg {average:.2f}, best {_fmt(best)}/10)"
            )

        embed = discord.Embed(
            title="🐈 Catfishing Leaderboard",
            description="\n".join(lines),
            color=0xBEBEFE,
        )

        # Group "all 10 correct" days. Each puzzle is a day; the group's aggregate
        # for a day is the number of distinct questions someone got (cat or egg).
        aggregates = {puzzle: len(correct) for puzzle, correct in group_correct.items()}
        best_aggregate = max(aggregates.values())
        best_puzzles = [p for p, value in aggregates.items() if value == best_aggregate]

        # Describe how often the best day was reached: a date if it was unique,
        # otherwise a count.
        if len(best_puzzles) == 1:
            day = puzzle_date[best_puzzles[0]]
            reached = f"on **{day:%b} {day.day}, {day.year}**"
        else:
            reached = f"on **{len(best_puzzles)}** days"

        if best_aggregate == QUESTIONS:
            group_text = f"🎉 The group got **all {QUESTIONS}** correct {reached}."
        else:
            group_text = (
                f"No days with all {QUESTIONS} correct. "
                f"Best group score was **{best_aggregate}/{QUESTIONS}**, reached {reached}."
            )
        embed.add_field(name="🤝 Group best", value=group_text, inline=False)

        # Hardest answers anyone in the channel got. Pull each puzzle's global
        # stats from catfishing.net and rank the solved questions by how few
        # players worldwide got them right.
        puzzle_stats = await self._puzzle_stats(list(solvers))

        answers = []  # (rate, title, puzzle, names)
        for puzzle, positions in solvers.items():
            stats = puzzle_stats.get(puzzle)
            if stats is None:
                continue
            titles, rates = stats
            for position, names in positions.items():
                if position >= len(rates) or rates[position] is None:
                    continue
                title = titles[position] if position < len(titles) else "?"
                answers.append((rates[position], title, puzzle, names))

        answers.sort(key=lambda a: a[0])
        if answers:
            hardest_lines = []
            for i, (rate, title, puzzle, names) in enumerate(answers[:HARDEST_COUNT], 1):
                who = ", ".join(sorted(names))
                hardest_lines.append(
                    f"{i}. **{title}** — only {rate:.1f}% got it (#{puzzle}) — {who}"
                )
            embed.add_field(
                name="🧠 Hardest answers",
                value="\n".join(hardest_lines),
                inline=False,
            )

        embed.set_footer(
            text=f"Period: {label} • {len(ranking)} players • {len(aggregates)} days played"
        )
        return embed

    async def compare_stats(self, rows: list[dict], author_id: int) -> list[str] | None:
        """
        Comparative stats for one player against everyone in ``rows``.

        Returns formatted lines for the /mystats embed, or None if the player
        has no cached Catfishing results. Comparisons only consider "shared"
        puzzles — ones at least two people posted — so playing alone doesn't
        inflate anything. The player's unique solves (answers nobody else in
        the channel got) are ranked by global solve rate from catfishing.net
        and the standouts at both ends are listed; if the API is unreachable
        the counts still work, only the lists are skipped.
        """
        # Correct question indexes per player per puzzle (union of reposts),
        # and each player's best score per puzzle.
        corrects: dict[int, dict[int, set]] = defaultdict(lambda: defaultdict(set))
        scores: dict[tuple, float] = {}
        for row in rows:
            puzzle = row["payload"]["puzzle"]
            pid = row["author_id"]
            corrects[puzzle][pid] |= set(row["payload"]["correct"])
            key = (pid, puzzle)
            score = row["payload"]["score"]
            if key not in scores or score > scores[key]:
                scores[key] = score
        mine = {p: score for (pid, p), score in scores.items() if pid == author_id}
        if not mine:
            return None

        shared = [p for p in mine if len(corrects[p]) > 1]
        # unique_positions[puzzle] = question indexes only this player got.
        unique_positions: dict[int, set] = {}
        for p in shared:
            others = set().union(
                *(c for pid, c in corrects[p].items() if pid != author_id)
            )
            unique = corrects[p][author_id] - others
            if unique:
                unique_positions[p] = unique
        unique_solves = sum(len(positions) for positions in unique_positions.values())
        wins = sum(
            1
            for p in shared
            if mine[p] == max(scores[(pid, p)] for pid in corrects[p])
        )
        my_avg = sum(mine.values()) / len(mine)
        channel_avg = sum(scores.values()) / len(scores)

        lines = [
            f"Days played: **{len(mine)}**",
            f"🎯 Answers nobody else got: **{unique_solves}** "
            f"across {len(shared)} shared puzzles",
            f"🏆 Top score of the day: **{wins}** of {len(shared)} shared puzzles",
            f"📈 Average: **{my_avg:.2f}/10** vs the channel's {channel_avg:.2f}/10",
        ]

        # Rank the unique solves by how many players worldwide got them, and
        # show the standouts: the hardest (impressive) and the easiest (the
        # gimmes everyone else in the channel somehow missed).
        answers = []  # (rate, title, puzzle)
        if unique_positions:
            fetched = await self._puzzle_stats(list(unique_positions))
            for puzzle, positions in unique_positions.items():
                stats = fetched[puzzle]
                if stats is None:
                    continue
                titles, rates = stats
                for position in positions:
                    if position >= len(rates) or rates[position] is None:
                        continue
                    title = titles[position] if position < len(titles) else "?"
                    if len(title) > 48:
                        title = title[:47] + "…"
                    answers.append((rates[position], title, puzzle))
        if answers:
            answers.sort(key=lambda a: a[0])
            hardest = answers[:HARDEST_COUNT]
            # Easiest from the other end, never overlapping the hardest list.
            easiest = list(reversed(answers[HARDEST_COUNT:]))[:HARDEST_COUNT]
            lines.append("### Hardest answers nobody else got")
            lines += [f"• {title} — {rate:.1f}% (#{p})" for rate, title, p in hardest]
            if easiest:
                lines.append("### Easiest answers nobody else got")
                lines += [
                    f"• {title} — {rate:.1f}% (#{p})" for rate, title, p in easiest
                ]
        return lines


async def setup(bot) -> None:
    await bot.add_cog(Catfishing(bot))
