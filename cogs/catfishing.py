"""
Catfishing leaderboard cog for Kennel-LeaderBot.

Scans the channel/thread the command is invoked in for shared Catfishing
results, tallies each person's monthly scores, and posts a leaderboard with
totals, averages and personal bests. It also reports on how many days the
group collectively got every question right.

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
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

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

# Month names accepted in the `month` argument, mapped to their number.
MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _fmt(score: float) -> str:
    """Format a score without a trailing ``.0`` (e.g. 4, 3.5)."""
    return f"{score:g}"


class Catfishing(commands.Cog, name="catfishing"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @staticmethod
    def _score_grid(grid, cat, egg):
        """
        Score a sequence of result markers.

        Returns ``(score, correct_positions)`` where ``score`` is one point per
        cat plus half a point per egg, and ``correct_positions`` is the set of
        indexes that were a cat or egg.
        """
        score = sum(1 for s in grid if s == cat) + 0.5 * sum(1 for s in grid if s == egg)
        correct = {i for i, s in enumerate(grid) if s in (cat, egg)}
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

    @classmethod
    def _parse_message(cls, content: str, posted_on):
        """
        Extract a Catfishing result from a message.

        Returns ``(puzzle, posted_on, score, correct_positions)`` where
        ``puzzle`` is the puzzle number, ``score`` is cats + half-eggs, and
        ``correct_positions`` is the set of question indexes (0-9) the player
        got right (cat or egg). Returns None if the message isn't a result.
        """
        match = SCORE_RE.search(content)
        if match is None:
            return None

        grid = cls._extract_grid(content)
        if grid is None:
            return None

        puzzle = int(match.group(1))
        score, correct = grid
        return puzzle, posted_on, score, correct

    @staticmethod
    def _resolve_window(month: str | None):
        """
        Work out the (after, label, month_filter) for the scan.

        - `after` is the UTC datetime to start fetching history from.
        - `label` describes the period for the embed title.
        - `month_filter` is a (year, month) tuple to keep only matching days.
        """
        now = datetime.now(timezone.utc)
        if month is None:
            # Default: the previous calendar month, unless today is the final
            # day of the current month (in which case use the current month).
            is_last_day_of_month = (now + timedelta(days=1)).month != now.month
            if is_last_day_of_month:
                year, month_num = now.year, now.month
            elif now.month == 1:
                year, month_num = now.year - 1, 12
            else:
                year, month_num = now.year, now.month - 1

            after = datetime(year, month_num, 1, tzinfo=timezone.utc)
            label = f"{datetime(year, month_num, 1):%B %Y}"
            return after, label, (year, month_num)

        # Accept "June", "Jun", "June 2026", "2026-06", "6".
        text = month.strip().lower()
        year = now.year
        month_num = None

        iso = re.fullmatch(r"(\d{4})[-/](\d{1,2})", text)
        if iso:
            year, month_num = int(iso.group(1)), int(iso.group(2))
        else:
            parts = text.split()
            name = parts[0]
            month_num = MONTHS.get(name)
            if month_num is None and name.isdigit():
                month_num = int(name)
            if len(parts) > 1 and parts[1].isdigit():
                year = int(parts[1])

        if month_num is None or not 1 <= month_num <= 12:
            return None  # signals "could not parse"

        after = datetime(year, month_num, 1, tzinfo=timezone.utc)
        label = f"{datetime(year, month_num, 1):%B %Y}"
        return after, label, (year, month_num)

    @commands.hybrid_command(
        name="catfishing",
        description="Tally Catfishing scores in this channel and show a leaderboard.",
    )
    @app_commands.describe(
        month="Month to tally, e.g. 'June', 'Jun 2026' or '2026-06'. Defaults to last full month.",
    )
    async def catfishing(self, context: Context, *, month: str = None) -> None:
        """
        Scan the current channel/thread for Catfishing results and post a leaderboard.

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

        # Scanning history can take a while, so let Discord know we're working.
        await context.defer()

        # players[author_id] = {"name": str, "scores": {puzzle: score}}
        players: dict[int, dict] = defaultdict(
            lambda: {"name": "", "scores": {}}
        )
        # group_correct[puzzle] = set of question indexes the group got (cat/egg)
        group_correct: dict[int, set] = defaultdict(set)
        # puzzle_date[puzzle] = the (earliest) date that puzzle was posted
        puzzle_date: dict[int, "datetime.date"] = {}

        async for message in context.channel.history(limit=None, after=after):
            if not message.content:
                continue
            result = self._parse_message(message.content, message.created_at.date())
            if result is None:
                continue
            puzzle, played_on, score, correct = result

            if (played_on.year, played_on.month) != month_filter:
                continue

            entry = players[message.author.id]
            entry["name"] = message.author.display_name
            # Keep the best score if someone posts the same puzzle twice.
            existing = entry["scores"].get(puzzle)
            if existing is None or score > existing:
                entry["scores"][puzzle] = score
            # The group "gets" a question if anyone got it right.
            group_correct[puzzle] |= correct
            existing_date = puzzle_date.get(puzzle)
            if existing_date is None or played_on < existing_date:
                puzzle_date[puzzle] = played_on

        if not players:
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

        embed.set_footer(
            text=f"Period: {label} • {len(ranking)} players • {len(aggregates)} days played"
        )
        await context.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(Catfishing(bot))
