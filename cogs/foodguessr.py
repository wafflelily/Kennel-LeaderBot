"""
FoodGuessr leaderboard cog for Kennel-LeaderBot.

Scans the channel/thread the command is invoked in for shared FoodGuessr
results, tallies each person's daily total scores over a period, and posts
a leaderboard.

Expected message format (as copy-pasted from FoodGuessr):

    FoodGuessr - Thursday, Jun 18, 2026 UTC
    🌕🌕🌕🌑 3,500 ⋅ Round 1
    🌕🌕🌕🌖 4,500 ⋅ Round 2
    🌕🌕🌕🌑 3,500 ⋅ Round 3
    Total score: 11,500/15,000
    (+1,735 above today's average!) 🎉
"""

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

# Matches the header line, capturing month abbreviation, day and year.
# Matches a date with an optional leading weekday, capturing month abbreviation,
# day and year. Works both inside the header line ("FoodGuessr - Thursday,
# Jun 18, 2026 UTC") and as a standalone line ("Tuesday, Jun 16, 2026").
DATE_RE = re.compile(r"(?:[A-Za-z]+,\s*)?([A-Za-z]{3})[a-z]*\s+(\d{1,2}),\s*(\d{4})")
# Matches the "Total score: X/Y" line. e.g. "Total score: 11,500/15,000"
TOTAL_RE = re.compile(r"Total score:\s*([\d,]+)\s*/\s*[\d,]+")
# Matches the "I got X on the FoodGuessr Daily!" line.
GOT_RE = re.compile(r"I got\s*([\d,]+)\s*on the FoodGuessr", re.IGNORECASE)

# A perfect game is 5,000 in all three rounds.
PERFECT_SCORE = 15000

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


class FoodGuessr(commands.Cog, name="foodguessr"):
    def __init__(self, bot) -> None:
        self.bot = bot

    @staticmethod
    def _parse_date(content: str):
        """Return the played-on date found in the message, or None."""
        match = DATE_RE.search(content)
        if match is None:
            return None
        month_abbr, day, year = match.groups()
        month = MONTHS.get(month_abbr.lower())
        if month is None:
            return None
        try:
            return datetime(int(year), month, int(day)).date()
        except ValueError:
            return None

    @staticmethod
    def _parse_numbers_only(content: str):
        """
        Parse the bare four-line format (total then three round scores), e.g.

            13,000
            4,000
            5,000
            4,000

        Returns the total, or None if the message isn't exactly this format.
        Validated strictly (every line numeric, total == sum of three rounds,
        each round 0-5,000) to avoid matching ordinary chat messages.
        """
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        if len(lines) != 4:
            return None
        values = []
        for line in lines:
            if not re.fullmatch(r"[\d,]+", line):
                return None
            values.append(int(line.replace(",", "")))

        total, rounds = values[0], values[1:]
        if any(not 0 <= r <= 5000 for r in rounds):
            return None
        if total != sum(rounds):
            return None
        return total

    @classmethod
    def _parse_message(cls, content: str, posted_on):
        """
        Extract (date, score) from a FoodGuessr result message.

        Supports the "Total score: X/Y" format, the "I got X on the FoodGuessr
        Daily!" format, and the bare four-line numbers format. When the message
        has no in-text date, `posted_on` (the message's post date) is used.

        Returns a tuple of (datetime.date, int) or None if the message is not a
        recognised FoodGuessr result.
        """
        total = TOTAL_RE.search(content) or GOT_RE.search(content)
        if total is not None:
            played_on = cls._parse_date(content) or posted_on
            score = int(total.group(1).replace(",", ""))
            return played_on, score

        # Bare numbers-only format: no date available, so use the post date.
        score = cls._parse_numbers_only(content)
        if score is not None:
            return posted_on, score
        return None

    @staticmethod
    def _resolve_window(month: str | None):
        """
        Work out the (after, label, month_filter) for the scan.

        - `after` is the UTC datetime to start fetching history from.
        - `label` describes the period for the embed title.
        - `month_filter` is a (year, month) tuple to keep only matching days, or
          None to keep everything in the window.
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
        name="foodguessr",
        description="Tally FoodGuessr scores in this channel and show a leaderboard.",
    )
    @app_commands.describe(
        month="Month to tally, e.g. 'June', 'Jun 2026' or '2026-06'. Defaults to the last 30 days.",
    )
    async def foodguessr(self, context: Context, *, month: str = None) -> None:
        """
        Scan the current channel/thread for FoodGuessr results and post a leaderboard.

        :param context: The hybrid command context.
        :param month: Optional month to tally; defaults to the last 30 days.
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

        # totals[author_id] = {"name": str, "total": int, "days": {date: score}}
        totals: dict[int, dict] = defaultdict(
            lambda: {"name": "", "total": 0, "days": {}}
        )
        parsed_messages = 0

        async for message in context.channel.history(limit=None, after=after):
            if not message.content:
                continue
            result = self._parse_message(
                message.content, message.created_at.date()
            )
            if result is None:
                continue
            played_on, score = result

            if month_filter is not None and (
                played_on.year,
                played_on.month,
            ) != month_filter:
                continue

            entry = totals[message.author.id]
            entry["name"] = message.author.display_name
            # Keep the best score if someone posts the same day more than once.
            existing = entry["days"].get(played_on)
            if existing is None or score > existing:
                if existing is not None:
                    entry["total"] -= existing
                entry["days"][played_on] = score
                entry["total"] += score
                parsed_messages += 1

        if not totals:
            await context.send(
                embed=discord.Embed(
                    title="🍽️ FoodGuessr Leaderboard",
                    description=(
                        f"No FoodGuessr results found for **{label}** in this channel.\n\n"
                        "Make sure results are posted here and that I can read message "
                        "history (the `message_content` intent must be enabled)."
                    ),
                    color=0xE02B2B,
                )
            )
            return

        ranking = sorted(
            totals.values(),
            key=lambda e: (e["total"], len(e["days"])),
            reverse=True,
        )

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, entry in enumerate(ranking):
            rank = medals[i] if i < len(medals) else f"`#{i + 1}`"
            days = len(entry["days"])
            average = entry["total"] / days
            lines.append(
                f"{rank} **{entry['name']}** — {entry['total']:,} pts "
                f"({days} day{'s' if days != 1 else ''}, avg {average:,.0f})"
            )

        embed = discord.Embed(
            title="🍽️ FoodGuessr Leaderboard",
            description="\n".join(lines),
            color=0xBEBEFE,
        )

        # Most perfect games (15,000 — 5,000 in all three rounds).
        perfect_counts = {
            entry["name"]: sum(
                1 for s in entry["days"].values() if s == PERFECT_SCORE
            )
            for entry in ranking
        }
        most_perfect = max(perfect_counts.values())
        if most_perfect == 0:
            perfect_text = f"Nobody scored a perfect {PERFECT_SCORE:,} this period."
        else:
            leaders = [n for n, c in perfect_counts.items() if c == most_perfect]
            names = ", ".join(f"**{n}**" for n in leaders)
            perfect_text = (
                f"{names} — {most_perfect} perfect"
                f"{'s' if most_perfect != 1 else ''} ({PERFECT_SCORE:,})"
            )
        embed.add_field(name="💯 Most perfects", value=perfect_text, inline=False)

        embed.set_footer(
            text=f"Period: {label} • {len(ranking)} players • {parsed_messages} results tallied"
        )
        await context.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(FoodGuessr(bot))
