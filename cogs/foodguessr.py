"""
FoodGuessr leaderboard cog for Kennel-LeaderBot.

Tallies shared FoodGuessr results in the channel/thread the command is invoked
in — each person's best daily score over a period — and posts a leaderboard.

Expected message format (as copy-pasted from FoodGuessr):

    FoodGuessr - Thursday, Jun 18, 2026 UTC
    🌕🌕🌕🌑 3,500 ⋅ Round 1
    🌕🌕🌕🌖 4,500 ⋅ Round 2
    🌕🌕🌕🌑 3,500 ⋅ Round 3
    Total score: 11,500/15,000
    (+1,735 above today's average!) 🎉

Parsed results are cached in the database (see ``leaderboard.base``); the
command does an incremental catch-up scan and reads its aggregates from there
rather than re-scanning the whole channel each time.
"""

import re
from collections import defaultdict
from datetime import date

import discord
from discord import app_commands
from discord.ext import commands
from discord.ext.commands import Context

from leaderboard.base import MONTHS, LeaderboardCog

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


class FoodGuessr(LeaderboardCog, name="foodguessr"):
    GAME = "foodguessr"

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
            return date(int(year), month, int(day))
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

    def parse(self, content: str, posted_on: date):
        """
        Parse a FoodGuessr result into ``(played_on, payload)`` for the cache.

        Supports the "Total score: X/Y" format, the "I got X on the FoodGuessr
        Daily!" format, and the bare four-line numbers format. When the message
        has no in-text date, the post date is used. ``payload`` holds the day's
        ``score``. Returns None if the message isn't a recognised result.
        """
        total = TOTAL_RE.search(content) or GOT_RE.search(content)
        if total is not None:
            played_on = self._parse_date(content) or posted_on
            score = int(total.group(1).replace(",", ""))
            return played_on, {"score": score}

        # Bare numbers-only format: no date available, so use the post date.
        score = self._parse_numbers_only(content)
        if score is not None:
            return posted_on, {"score": score}
        return None

    @commands.hybrid_command(
        name="foodguessr",
        description="Tally FoodGuessr scores in this channel and show a leaderboard.",
    )
    @app_commands.describe(
        month="Month to tally, e.g. 'June', 'Jun 2026' or '2026-06'. Defaults to the previous full month.",
    )
    async def foodguessr(self, context: Context, *, month: str = None) -> None:
        """
        Tally the current channel/thread's FoodGuessr scores and post a leaderboard.

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
            # Scan a little before the month starts so a result dated on the 1st
            # but posted just beforehand is still cached. Counting stays exact:
            # dated results are tallied by their in-message date, so they land in
            # the right month regardless of when they were posted.
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

        rows = await self._load_window(context.channel.id, month_filter)

        # Resolve each player's current server nickname from their stored id, so
        # names stay right even after someone renames (stored name is fallback).
        names = await self._resolve_names(
            context.guild,
            [row["author_id"] for row in rows],
            {row["author_id"]: row["author_name"] for row in rows},
        )

        # totals[author_id] = {"name": str, "total": int, "days": {date: score}}
        totals: dict[int, dict] = defaultdict(
            lambda: {"name": "", "total": 0, "days": {}}
        )

        for row in rows:
            played_on = row["played_on"]
            score = row["payload"]["score"]
            entry = totals[row["author_id"]]
            entry["name"] = names[row["author_id"]]
            # Keep the best score if someone posts the same day more than once.
            existing = entry["days"].get(played_on)
            if existing is None or score > existing:
                if existing is not None:
                    entry["total"] -= existing
                entry["days"][played_on] = score
                entry["total"] += score

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

        days_tallied = sum(len(e["days"]) for e in ranking)
        embed.set_footer(
            text=f"Period: {label} • {len(ranking)} players • {days_tallied} results tallied"
        )
        await context.send(embed=embed)


async def setup(bot) -> None:
    await bot.add_cog(FoodGuessr(bot))
