"""
Gauntle leaderboard cog for Kennel-LeaderBot.

Tallies shared Gauntle(t) results in the channel/thread the command is invoked
in, then posts a leaderboard showing the fastest overall runs and the best time
recorded in each individual category over a period.

Expected message format (as copy-pasted from Gauntle):

    I ran the October 7th Gauntle(t) in 5 minutes and 59.80 seconds!

    🟩 Sudoku: 0:46.84 (−10s) ✨
    🟨 Paire: 1:17.31 (+30s)
    🟩 Wordy: 0:45.00 (−5s) ✨
    🟩 Conduit: 0:13.64 (−15s) ✨
    🟩 Clambers: 0:21.91 (−8s) ✨
    🟩 Queens: 0:25.03 (−5s) ✨
    🟩 Angle: 0:16.51 (−1s) ✨
    🟩 Mines: 0:51.53 (−15s) ✨
    🟩 Nonogram: 1:29.83 (−30s) ✨
    🟩 Crossword: 0:29.43 (−20s) ✨
    🟩 Ratiole: 0:04.87 (−19s) ✨

The header line gives the run's date and total time. Each category line gives a
raw solve time and a bonus (e.g. ``−10s``) or penalty (e.g. ``+5s``, ``skip
+90s``). A category's *effective* time — used for the per-category bests — is
the raw time plus that adjustment, so a fast solve with a big bonus can even go
negative.

Gauntle occasionally swaps games in and out (e.g. Chromal and Shapeup were
replaced by Conduit and Angle), though a run always has 11 of them. Category
names are therefore never hard-coded: the leaderboard shows whichever games
were played in the requested month, and /mystats treats the games in the
channel's most recent run as the current lineup.

At the end of each day, any channel where two or more people posted a run for
that day gets a small team score: the sum of the day's best time in each game
across all players.

Parsed results are cached in the database (see ``leaderboard.base``); the
command does an incremental catch-up scan and reads its aggregates from there
rather than re-scanning the whole channel each time.
"""

import re
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ext.commands import Context

from leaderboard.base import MONTHS, LeaderboardCog, month_choices

# Matches the header line, e.g. "I ran the August 20th Gauntle(t) in ...".
# Group 1 is the month name, group 2 is the day. The trailing ".*?gauntle"
# confirms the message really is a Gauntle result and not ordinary chat.
HEADER_RE = re.compile(
    r"ran the\s+([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b.*?gauntle",
    re.IGNORECASE,
)

# Matches a category line, e.g. "🟩 Sudoku: 0:52.24 (−10s) ✨".
# Groups: name, optional minutes, seconds, optional adjustment text.
CATEGORY_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9]*)\s*:\s*"        # category name
    r"(?:(\d+):)?(\d+(?:\.\d+)?)"           # time, optional M: prefix
    r"(?:\s*\(([^)]*)\))?"                  # optional (adjustment)
)

# Matches a bonus/penalty inside the parentheses, e.g. "−10s", "+5s",
# "skip +90s". Handles both the ASCII hyphen and the Unicode minus sign (−).
ADJ_RE = re.compile(r"([+\-−])\s*(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)

# A full Gauntle run has exactly this many categories. Once we've tallied that
# many from a message we stop, so trailing text people add doesn't get parsed
# as extra categories.
CATEGORIES_PER_RUN = 11

# When the daily team score goes out (UTC): the end of the UTC day, which is
# just before midnight in winter and just after it in summer for UK players.
# It covers runs whose header date is that day; if the bot is offline at this
# moment, that day's team score is skipped.
TEAM_SCORE_AT = time(hour=23, minute=55, tzinfo=timezone.utc)


def _fmt_time(seconds: float) -> str:
    """Format a duration in seconds as ``M:SS.ss`` (or ``SS.ss s`` under a minute)."""
    negative = seconds < 0
    seconds = abs(seconds)
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    if minutes:
        text = f"{minutes}:{secs:05.2f}"
    else:
        text = f"{secs:.2f}s"
    return f"-{text}" if negative else text


def _effective(info) -> float:
    """
    Effective time of a stored category entry.

    Handles both the current ``{"raw": ..., "adj": ...}`` payload shape and the
    bare effective-time float from rows cached before raw/adj were stored.
    """
    if isinstance(info, dict):
        return info["raw"] + info["adj"]
    return info


def _fmt_solve(raw: float | None, adj: float | None) -> str:
    """
    Format a raw solve time with its adjustment, e.g. ``0:50.58 (−10s)``.

    Returns "" for results cached before raw/adj were stored (fixable with
    ``/rebuild gauntle``). The adjustment is omitted when there wasn't one.
    """
    if raw is None:
        return ""
    text = _fmt_time(raw)
    if adj:
        sign = "−" if adj < 0 else "+"
        text += f" ({sign}{abs(adj):g}s)"
    return text


class Gauntle(LeaderboardCog, name="gauntle"):
    GAME = "gauntle"

    async def cog_load(self) -> None:
        self.daily_team_score.start()

    async def cog_unload(self) -> None:
        self.daily_team_score.cancel()

    @staticmethod
    def _parse_duration(text: str):
        """Parse "X minutes and Y seconds" (any of hours/minutes/seconds) to seconds."""
        hours = re.search(r"(\d+)\s*hours?", text, re.IGNORECASE)
        minutes = re.search(r"(\d+)\s*minutes?", text, re.IGNORECASE)
        seconds = re.search(r"(\d+(?:\.\d+)?)\s*seconds?", text, re.IGNORECASE)
        if not (hours or minutes or seconds):
            return None
        total = 0.0
        if hours:
            total += int(hours.group(1)) * 3600
        if minutes:
            total += int(minutes.group(1)) * 60
        if seconds:
            total += float(seconds.group(1))
        return total

    @staticmethod
    def _parse_adjustment(adj: str | None) -> float:
        """Parse a bonus/penalty string to seconds (negative for a bonus)."""
        if not adj:
            return 0.0
        match = ADJ_RE.search(adj)
        if match is None:
            return 0.0
        sign = -1.0 if match.group(1) in ("-", "−") else 1.0
        return sign * float(match.group(2))

    @staticmethod
    def _closest_date(month: int, day: int, posted_on: date):
        """
        Resolve the played-on date from a month/day (the header has no year).

        Picks the year that puts the date closest to when the message was posted,
        which handles December/January boundaries gracefully.
        """
        best = None
        for year in (posted_on.year - 1, posted_on.year, posted_on.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if best is None or abs((candidate - posted_on).days) < abs(
                (best - posted_on).days
            ):
                best = candidate
        return best

    def parse(self, content: str, posted_on: date):
        """
        Parse a Gauntle result into ``(played_on, payload)`` for the cache.

        ``payload`` holds the run's ``total`` seconds and a ``categories`` map of
        category name to ``{"raw": solve seconds, "adj": bonus/penalty seconds}``
        (effective time = raw + adj). Returns None if the message isn't a
        Gauntle result.
        """
        header = HEADER_RE.search(content)
        if header is None:
            return None
        month = MONTHS.get(header.group(1).lower())
        if month is None:
            return None
        played_on = self._closest_date(month, int(header.group(2)), posted_on)
        if played_on is None:
            return None

        # The total time lives on the header line ("... in 15 minutes and ...").
        header_line = content[header.start():].split("\n", 1)[0]
        total = self._parse_duration(header_line)
        if total is None:
            return None

        categories: dict[str, dict] = {}
        for line in content.splitlines():
            match = CATEGORY_RE.search(line)
            if match is None:
                continue
            name = match.group(1)
            minutes = int(match.group(2)) if match.group(2) else 0
            raw = minutes * 60 + float(match.group(3))
            adj = self._parse_adjustment(match.group(4))
            # Keep the person's best (lowest) effective time per category if a
            # category somehow appears twice in one message.
            current = categories.get(name)
            if current is None or raw + adj < current["raw"] + current["adj"]:
                categories[name] = {"raw": raw, "adj": adj}
            # A run only has CATEGORIES_PER_RUN categories; stop once we've got
            # them all so trailing text isn't parsed as extra categories.
            if len(categories) >= CATEGORIES_PER_RUN:
                break

        return played_on, {"total": total, "categories": categories}

    async def on_result_captured(self, message, played_on, payload) -> None:
        """
        Celebrate new personal bests the moment they're posted.

        Compares a freshly posted run against the poster's *own* cached history
        in this channel — the overall time and each category's effective time —
        and replies with a small note for a new overall best and/or new
        category bests. A first-ever run (or first time playing a category)
        just sets the baseline silently. Only fires for live messages, so
        history scans can't replay old bests.
        """
        rows = [
            row
            for row in await self._load_all(message.channel.id)
            if row["author_id"] == message.author.id
            and row["message_id"] != message.id
        ]
        if not rows:
            return  # first run: nothing to compare against

        best_total = min(row["payload"]["total"] for row in rows)
        best_categories: dict[str, float] = {}
        for row in rows:
            for name, info in row["payload"].get("categories", {}).items():
                effective = _effective(info)
                if name not in best_categories or effective < best_categories[name]:
                    best_categories[name] = effective

        parts = []
        if payload["total"] < best_total:
            parts.append(f"🏆 New personal best: {_fmt_time(payload['total'])}!")
        improved = [
            (name, _effective(info))
            for name, info in payload.get("categories", {}).items()
            if name in best_categories and _effective(info) < best_categories[name]
        ]
        if improved:
            listed = ", ".join(
                f"{name} ({_fmt_time(effective)})" for name, effective in improved
            )
            parts.append(f"✨ New personal category best: {listed}")
        if not parts:
            return
        try:
            await message.reply("\n".join(parts), mention_author=False)
        except discord.HTTPException:
            pass  # can't reply here; the leaderboard still counts it

    # ------------------------------------------------------------------ #
    # Daily team score
    # ------------------------------------------------------------------ #

    async def build_team_score(self, channel, day: date) -> str | None:
        """
        The day's team score: a sum of bests across everyone who played.

        For each game, takes the fastest effective time anyone in the channel
        posted for ``day``'s Gauntle, and sums them — the run the group would
        have had if each game went to whoever did it best. Returns None unless
        at least two people posted a run for that day.
        """
        rows = await self.bot.database.get_leaderboard_results(
            self.GAME, channel.id, day.isoformat(), (day + timedelta(days=1)).isoformat()
        )
        if len({row["author_id"] for row in rows}) < 2:
            return None

        names = await self._resolve_names(
            getattr(channel, "guild", None),
            [row["author_id"] for row in rows],
            {row["author_id"]: row["author_name"] for row in rows},
        )

        # best[game] = (effective time, author_id)
        best: dict[str, tuple[float, int]] = {}
        for row in rows:
            for name, info in row["payload"].get("categories", {}).items():
                effective = _effective(info)
                if name not in best or effective < best[name][0]:
                    best[name] = (effective, row["author_id"])
        if not best:
            return None

        team = sum(effective for effective, _ in best.values())
        # How much faster the team is than the day's fastest solo run.
        saved = min(row["payload"]["total"] for row in rows) - team

        header = f"🤝 **Team Gauntle for {day:%b %d}: {_fmt_time(team)}**"
        if saved > 0:
            header += f" — teamwork saved {_fmt_time(saved)}"
        lines = [header]
        # Who held the best time in how many games, most first (ties by name).
        held = Counter(author_id for _, author_id in best.values())
        ranked = sorted(held.items(), key=lambda item: (-item[1], names[item[0]].lower()))
        lines.append(
            " · ".join(f"{names[author_id]} ×{count}" for author_id, count in ranked)
        )
        return "\n".join(lines)

    @tasks.loop(time=TEAM_SCORE_AT)
    async def daily_team_score(self) -> None:
        """Post today's team score in every channel where Gauntle is tracked."""
        today = datetime.now(timezone.utc).date()
        start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
        for channel_id in await self.bot.database.get_leaderboard_channels(self.GAME):
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                continue
            try:
                # Catch up on anything live capture missed before tallying.
                await self._sync_channel(channel, start - self.SCAN_BUFFER)
                text = await self.build_team_score(channel, today)
                if text is not None:
                    await channel.send(text)
            except discord.Forbidden:
                self.bot.logger.warning(
                    f"Team score: missing permissions in channel {channel_id}"
                )
            except Exception:
                self.bot.logger.error(
                    f"Team score failed in channel {channel_id}", exc_info=True
                )

    @daily_team_score.before_loop
    async def before_daily_team_score(self) -> None:
        await self.bot.wait_until_ready()

    @commands.hybrid_command(
        name="gauntle",
        description="Show the fastest Gauntle runs and best category times in this channel.",
    )
    @app_commands.describe(
        month="Month to tally, e.g. 'August', 'Aug 2026' or '2026-08'. Defaults to the previous full month.",
    )
    async def gauntle(self, context: Context, *, month: str = None) -> None:
        """
        Tally the current channel/thread's Gauntle results and post a leaderboard.

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
            # Scan a little before the month starts so a run played on the 1st
            # but posted just beforehand is still cached. Counting stays exact:
            # results are tallied by their header date, so only August runs land
            # in August regardless of when they were posted.
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
                    title="⚔️ Gauntle Leaderboard",
                    description=(
                        f"No Gauntle results found for **{label}** in this channel.\n\n"
                        "Make sure results are posted here and that I can read message "
                        "history (the `message_content` intent must be enabled)."
                    ),
                    color=0xE02B2B,
                )
            )
            return
        await context.send(embed=embed)

    @gauntle.autocomplete("month")
    async def gauntle_month_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return month_choices(current)

    async def build_leaderboard(
        self, channel, month_filter, label: str
    ) -> discord.Embed | None:
        """
        Build the month's leaderboard embed from the cache.

        Shared by the /gauntle command and the monthly auto-poster. Returns
        None when the channel has no results for the month.
        """
        rows = await self._load_window(channel.id, month_filter)

        # Resolve each player's current server nickname from their stored id, so
        # names stay right even after someone renames (stored name is fallback).
        names = await self._resolve_names(
            getattr(channel, "guild", None),
            [row["author_id"] for row in rows],
            {row["author_id"]: row["author_name"] for row in rows},
        )

        # runs[author_id] = {"name", "best": float, "date": date}
        runs: dict[int, dict] = {}
        # category_best[name] = {"time": float, "name": str, "date": date}
        # A plain dict preserves the order categories are first seen in.
        category_best: dict[str, dict] = {}

        for row in rows:
            played_on = row["played_on"]
            total = row["payload"]["total"]
            categories = row["payload"]["categories"]
            display = names[row["author_id"]]

            # Track each person's fastest overall run.
            entry = runs.get(row["author_id"])
            if entry is None:
                entry = {"name": display, "best": total, "date": played_on}
                runs[row["author_id"]] = entry
            entry["name"] = display
            if total < entry["best"]:
                entry["best"] = total
                entry["date"] = played_on

            # Track the best (lowest) effective time in each category.
            for name, info in categories.items():
                if isinstance(info, dict):
                    raw, adj = info["raw"], info["adj"]
                    effective = raw + adj
                else:
                    # Row cached before raw/adj were stored separately; only the
                    # effective time is known (/rebuild gauntle re-parses these).
                    raw, adj = None, None
                    effective = info
                current = category_best.get(name)
                if current is None or effective < current["time"]:
                    category_best[name] = {
                        "time": effective,
                        "raw": raw,
                        "adj": adj,
                        "name": display,
                        "date": played_on,
                    }

        if not runs:
            return None

        # Fastest 3 overall times (each person's best run).
        ranking = sorted(runs.values(), key=lambda e: e["best"])[:3]
        medals = ["🥇", "🥈", "🥉"]
        lines = [
            f"{medals[i]} **{entry['name']}** — {_fmt_time(entry['best'])} "
            f"(on {entry['date']:%b %d})"
            for i, entry in enumerate(ranking)
        ]

        embed = discord.Embed(
            title="⚔️ Gauntle Leaderboard",
            description="\n".join(lines),
            color=0xBEBEFE,
        )

        # Best effective time in each category, alongside the actual solve time
        # and its bonus/penalty, laid out as an aligned monospace table:
        # Category | Best | Solve (adj) | Player.
        header = ("Category", "Best", "Solve (adj)", "Player")
        cat_rows = [
            (
                name,
                _fmt_time(info["time"]),
                _fmt_solve(info["raw"], info["adj"]),
                info["name"],
            )
            for name, info in category_best.items()
        ]
        widths = [
            max(len(row[col]) for row in [header, *cat_rows]) for col in range(3)
        ]
        table = [
            f"{cat:<{widths[0]}}  {time_text:>{widths[1]}}  "
            f"{solve_text:<{widths[2]}}  {player}"
            for cat, time_text, solve_text, player in [header, *cat_rows]
        ]

        value = "```\n" + "\n".join(table) + "\n```"
        embed.add_field(
            name="Best per category",
            value=value,
            inline=False,
        )

        embed.set_footer(
            text=f"Period: {label} • {len(runs)} players • {len(rows)} runs tallied"
        )
        return embed

    async def compare_stats(self, rows: list[dict], author_id: int) -> list[str] | None:
        """
        Comparative stats for one player against everyone in ``rows``.

        Returns formatted lines for the /mystats embed, or None if the player
        has no cached Gauntle runs.
        """
        # Best (fastest) run per player per day.
        best: dict[tuple, float] = {}
        for row in rows:
            key = (row["author_id"], row["played_on"])
            total = row["payload"]["total"]
            if key not in best or total < best[key]:
                best[key] = total
        mine = {day: total for (pid, day), total in best.items() if pid == author_id}
        if not mine:
            return None

        by_day: dict = defaultdict(dict)
        for (pid, day), total in best.items():
            by_day[day][pid] = total
        contested = [day for day in mine if len(by_day[day]) > 1]
        wins = sum(1 for day in contested if mine[day] == min(by_day[day].values()))

        best_day = min(mine, key=mine.get)

        # Per-category personal bests, and the channel's best for the crown.
        channel_best: dict[str, float] = {}
        my_best: dict[str, float] = {}
        for row in rows:
            for name, info in row["payload"].get("categories", {}).items():
                effective = _effective(info)
                if name not in channel_best or effective < channel_best[name]:
                    channel_best[name] = effective
                if row["author_id"] == author_id and (
                    name not in my_best or effective < my_best[name]
                ):
                    my_best[name] = effective

        lines = [
            f"Days run: **{len(mine)}**",
            f"🏆 Fastest of the day: **{wins}** of {len(contested)} contested days",
            f"⚡ Personal best run: **{_fmt_time(mine[best_day])}** "
            f"(on {best_day:%b %d})",
        ]

        # Gauntle swaps games in and out over time, so split the player's bests
        # into the current lineup (the games in the channel's most recent run)
        # and retired games that no longer appear.
        latest = max(row["played_on"] for row in rows)
        lineup = {
            name
            for row in rows
            if row["played_on"] == latest
            for name in row["payload"].get("categories", {})
        }
        current = sorted(name for name in my_best if name in lineup)
        retired = sorted(name for name in my_best if name not in lineup)

        def best_line(name: str) -> str:
            crown = " 👑" if my_best[name] == channel_best[name] else ""
            return f"• {name}: {_fmt_time(my_best[name])}{crown}"

        # "### " lines become their own embed field in /mystats.
        if current:
            lines.append("### Personal category bests (👑 = channel record)")
            lines.extend(best_line(name) for name in current)
            # A "dream run": the sum of every current game's personal best, as
            # if all their best category times had landed in one run. Only
            # meaningful once they've a time for every game in the lineup.
            missing = sorted(lineup - my_best.keys())
            if missing:
                lines.append(f"Sum of bests: needs a time for {', '.join(missing)}")
            else:
                total = sum(my_best[name] for name in current)
                lines.append(f"**Sum of bests: {_fmt_time(total)}**")
        if retired:
            lines.append("### Retired category bests")
            lines.extend(best_line(name) for name in retired)
        return lines


async def setup(bot) -> None:
    await bot.add_cog(Gauntle(bot))
