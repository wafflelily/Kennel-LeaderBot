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
from collections import Counter, defaultdict
from datetime import date

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
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
# How many of the hardest answers to show.
HARDEST_COUNT = 5


def _fmt(score: float) -> str:
    """Format a score without a trailing ``.0`` (e.g. 4, 3.5)."""
    return f"{score:g}"


class Catfishing(LeaderboardCog, name="catfishing"):
    GAME = "catfishing"

    def __init__(self, bot) -> None:
        super().__init__(bot)
        # Cache of puzzle stats keyed by puzzle number: {day: (titles, rates)}.
        # Historical puzzle stats don't change, so caching across invocations
        # is safe and avoids re-fetching.
        self._puzzle_cache: dict[int, tuple] = {}

    async def _fetch_puzzle(self, session: aiohttp.ClientSession, day: int):
        """
        Fetch a puzzle's stats from catfishing.net.

        Returns ``(titles, rates)`` — parallel lists where ``titles[i]`` is the
        answer for question ``i`` and ``rates[i]`` is the global percentage of
        players who got it right (lower = harder). Returns None on any failure.
        """
        if day in self._puzzle_cache:
            return self._puzzle_cache[day]
        try:
            async with session.get(
                API_URL.format(day=day),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

        articles = data.get("articles") or []
        stats_articles = (data.get("stats") or {}).get("articles") or []
        titles = [a.get("title") for a in articles]
        rates = [sa.get("correctRate") for sa in stats_articles]
        result = (titles, rates)
        self._puzzle_cache[day] = result
        return result

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
        # Emoji grid.
        symbols = [ch for ch in content if ch in SYMBOLS]
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

    @staticmethod
    def _date_anchor(rows) -> int | None:
        """
        Work out the offset that maps a puzzle number to its real date.

        Catfishing results carry only a puzzle number, not a date, so the post
        date is an unreliable guide near month boundaries (a puzzle can be posted
        just after midnight, or a day or two late). But the puzzles are a daily
        sequence, so ``real_date = puzzle_number + anchor`` for a fixed anchor.

        We recover that anchor from the data: for a same-day post,
        ``post_ordinal - puzzle_number`` equals the anchor, so the most common
        value of that difference across all cached results is the anchor. Late
        or catch-up posts are outvoted. Returns None if there's nothing to go on.
        """
        offsets = Counter(
            row["played_on"].toordinal() - row["payload"]["puzzle"] for row in rows
        )
        if not offsets:
            return None
        return offsets.most_common(1)[0][0]

    @classmethod
    def _puzzle_date(cls, puzzle: int, anchor: int | None, fallback: date) -> date:
        """Map a puzzle number to its real date, falling back to the post date."""
        if anchor is None:
            return fallback
        try:
            return date.fromordinal(puzzle + anchor)
        except (ValueError, OverflowError):
            return fallback

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
        async with aiohttp.ClientSession() as session:
            fetched = await asyncio.gather(
                *(self._fetch_puzzle(session, day) for day in solvers)
            )
        puzzle_stats = dict(zip(solvers, fetched, strict=True))

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
            async with aiohttp.ClientSession() as session:
                fetched = await asyncio.gather(
                    *(self._fetch_puzzle(session, day) for day in unique_positions)
                )
            for (puzzle, positions), stats in zip(
                unique_positions.items(), fetched, strict=True
            ):
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
            lines.append("**Hardest answers nobody else got** (global solve rate)")
            lines += [f"• {title} — {rate:.1f}% (#{p})" for rate, title, p in hardest]
            if easiest:
                lines.append("**Easiest answers nobody else got**")
                lines += [
                    f"• {title} — {rate:.1f}% (#{p})" for rate, title, p in easiest
                ]
        return lines


async def setup(bot) -> None:
    await bot.add_cog(Catfishing(bot))
