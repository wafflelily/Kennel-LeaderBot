"""
Shared base class for the leaderboard cogs (gauntle, foodguessr, catfishing).

Historically each cog re-scanned the entire channel history from the start of
the requested month on every command invocation. That is O(all messages) per
call, slow, and rate-limit prone. This base class replaces that with a
DB-backed cache:

  * Results are parsed once and stored in the ``leaderboard_results`` table
    (see ``database`` / ``database/schema.sql``).
  * An ``on_message`` listener captures new results live as they're posted, so
    the cache stays current with no scanning at all.
  * Each command does an *incremental* catch-up scan — it only fetches the
    messages it hasn't already seen (anything newer than the last scan, plus a
    one-off backfill when an older month is requested for the first time) — and
    then builds the leaderboard by reading aggregated rows out of the cache.
  * Edits and deletions are reconciled via ``on_raw_message_edit`` /
    ``on_raw_message_delete`` so stale results don't linger.

Subclasses provide three things:

  * ``GAME``      - a unique short name used to namespace the cache rows.
  * ``parse()``   - turn a message's text into ``(played_on, payload)`` or None.
  * the command   - resolve a window, call ``_sync_channel`` + ``_load_window``
                    and render an embed from the returned rows.
"""

import asyncio
import re
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

# Matches an ISO-ish "YYYY-MM" / "YYYY/MM" month argument.
_ISO_MONTH_RE = re.compile(r"(\d{4})[-/](\d{1,2})")

# Month names accepted in the `month` command argument, mapped to their number.
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


def game_choices(games, current: str) -> list[app_commands.Choice[str]]:
    """Autocomplete choices for a ``game`` argument from the loaded game names."""
    current = current.strip().lower()
    return [
        app_commands.Choice(name=game, value=game)
        for game in sorted(games)
        if current in game
    ][:25]


def month_choices(current: str) -> list[app_commands.Choice[str]]:
    """
    Autocomplete choices for a ``month`` argument: the last 12 months, newest
    first, filtered by whatever the user has typed so far. Values use the
    ``YYYY-MM`` form, which ``_resolve_window`` accepts.
    """
    current = current.strip().lower()
    now = datetime.now(timezone.utc)
    year, month = now.year, now.month
    choices = []
    for _ in range(12):
        label = f"{datetime(year, month, 1):%B %Y}"
        value = f"{year}-{month:02d}"
        if current in label.lower() or current in value:
            choices.append(app_commands.Choice(name=label, value=value))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return choices


class LeaderboardCog(commands.Cog):
    """Base class factoring out window parsing, caching and scanning."""

    #: Unique short name for this game; namespaces its rows in the cache.
    GAME: str = ""

    #: How far *before* the requested month to start scanning history. A result
    #: played on the 1st is sometimes posted a little before the month begins
    #: (late-night posts, timezone offsets), so we cache a small margin either
    #: side of the boundary. This only widens what's *cached*, never what's
    #: *counted* — the month filter still runs on each result's own date.
    SCAN_BUFFER = timedelta(days=2)

    def __init__(self, bot) -> None:
        self.bot = bot
        # One lock per channel so two concurrent commands don't both run the
        # same (possibly long) history scan; the second waits and then sees
        # the first's scan state, making its own scan a cheap no-op.
        self._sync_locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    # Hooks for subclasses
    # ------------------------------------------------------------------ #

    def parse(self, content: str, posted_on: date):
        """
        Parse a message's text into ``(played_on: date, payload: dict)``.

        Returns None if the message isn't a result for this game. ``payload`` is
        a JSON-serialisable dict of whatever the leaderboard needs.
        """
        raise NotImplementedError

    async def on_result_captured(
        self, message: discord.Message, played_on: date, payload: dict
    ) -> None:
        """
        Hook: a *new* result was just captured live from chat (never during a
        history scan or an edit, so reacting here can't replay old news).
        Called after the result is stored. No-op by default.
        """

    # ------------------------------------------------------------------ #
    # Window resolution (identical across all three games)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _resolve_window(month: str | None):
        """
        Work out the ``(after, label, month_filter)`` for a scan.

        - ``after`` is the UTC datetime to start fetching history from.
        - ``label`` describes the period for the embed title.
        - ``month_filter`` is a ``(year, month)`` tuple.

        Returns None if a supplied month string couldn't be parsed.
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

        iso = _ISO_MONTH_RE.fullmatch(text)
        if iso:
            year, month_num = int(iso.group(1)), int(iso.group(2))
        else:
            parts = text.split()
            if not parts:
                return None  # blank or whitespace-only argument
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

    @staticmethod
    def _month_bounds(month_filter) -> tuple[str, str]:
        """Return the half-open ``[start, end)`` ISO date strings for a month."""
        year, month = month_filter
        start = date(year, month, 1)
        # First day of the following month.
        end = date(year + (month == 12), (month % 12) + 1, 1)
        return start.isoformat(), end.isoformat()

    # ------------------------------------------------------------------ #
    # Cache access
    # ------------------------------------------------------------------ #

    async def _store(self, message: discord.Message) -> bool:
        """
        Parse ``message`` and upsert it into the cache. Returns True if it was a
        result for this game (and therefore stored), False otherwise.
        """
        parsed = self.parse(message.content or "", message.created_at.date())
        if parsed is None:
            return False
        played_on, payload = parsed
        await self.bot.database.upsert_leaderboard_result(
            self.GAME,
            message.channel.id,
            message.id,
            message.author.id,
            message.author.display_name,
            played_on,
            payload,
        )
        return True

    async def _load_window(self, channel_id: int, month_filter) -> list[dict]:
        """Read the cached results for a channel within a month (by stored date)."""
        start_iso, end_iso = self._month_bounds(month_filter)
        return await self.bot.database.get_leaderboard_results(
            self.GAME, channel_id, start_iso, end_iso
        )

    async def _load_all(self, channel_id: int) -> list[dict]:
        """
        Read *every* cached result for a channel, ignoring the stored date.

        Used by games that attribute results to a month by something other than
        the post date (e.g. Catfishing, which derives each puzzle's real date
        from its puzzle number).
        """
        return await self.bot.database.get_leaderboard_results(
            self.GAME, channel_id, "0000-01-01", "9999-12-31"
        )

    async def _resolve_names(
        self, guild: discord.Guild | None, author_ids, fallbacks: dict[int, str]
    ) -> dict[int, str]:
        """
        Map each author id to their *current* server nickname.

        Discord nicknames/usernames change over time, so we resolve them at
        render time from the live guild rather than trusting the name captured
        when the result was posted. ``fallbacks`` (the stored names) is used for
        anyone who has since left the server, or in DMs where there's no guild.

        When the ``members`` privileged intent is enabled the member cache is
        authoritative and used directly; otherwise we fetch the (small) set of
        board members from the API, which works without that intent.
        """
        ids = list(dict.fromkeys(author_ids))  # de-dupe, keep order
        if guild is None:
            return {aid: fallbacks.get(aid, "Unknown") for aid in ids}

        names: dict[int, str] = {}
        to_fetch: list[tuple[int, discord.Member | None]] = []
        members_cached = self.bot.intents.members
        for aid in ids:
            member = guild.get_member(aid)
            if member is not None and members_cached:
                names[aid] = member.display_name
            else:
                to_fetch.append((aid, member))

        if to_fetch:
            fetched = await asyncio.gather(
                *(guild.fetch_member(aid) for aid, _ in to_fetch),
                return_exceptions=True,
            )
            for (aid, cached), result in zip(to_fetch, fetched, strict=True):
                if isinstance(result, discord.Member):
                    names[aid] = result.display_name
                elif cached is not None:
                    names[aid] = cached.display_name
                else:
                    names[aid] = fallbacks.get(aid, "Unknown")

        return names

    async def _sync_channel(self, channel: discord.abc.Messageable, after: datetime) -> None:
        """
        Bring the cache up to date for ``channel``, fetching only messages we
        haven't already scanned.

        On the first scan of a channel this pulls everything from ``after`` to
        now. Afterwards it does a cheap forward catch-up (anything newer than the
        last scan) plus a one-off backfill if an earlier month than we've seen
        before is being requested.

        May raise ``discord.Forbidden`` if the bot can't read history — callers
        handle that and show a permissions error.
        """
        lock = self._sync_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            db = self.bot.database
            newest_id, oldest_after = await db.get_leaderboard_scan(
                self.GAME, channel.id
            )
            after_iso = after.isoformat()
            max_seen = newest_id

            async def scan(history) -> None:
                nonlocal max_seen
                async for message in history:
                    if max_seen is None or message.id > max_seen:
                        max_seen = message.id
                    if message.author.bot or not message.content:
                        continue
                    await self._store(message)

            if oldest_after is None:
                # Never scanned: pull everything from `after` onwards.
                await scan(channel.history(limit=None, after=after))
                new_oldest = after_iso
            else:
                new_oldest = oldest_after
                if after_iso < oldest_after:
                    # Requested month predates what we've scanned; backfill the gap.
                    before_dt = datetime.fromisoformat(oldest_after)
                    await scan(
                        channel.history(limit=None, after=after, before=before_dt)
                    )
                    new_oldest = after_iso
                # Forward catch-up for anything posted since the last scan.
                if newest_id is not None:
                    await scan(
                        channel.history(limit=None, after=discord.Object(id=newest_id))
                    )

            await db.set_leaderboard_scan(self.GAME, channel.id, max_seen, new_oldest)

    # ------------------------------------------------------------------ #
    # Live capture / reconciliation
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Cache results as they're posted, so commands rarely need to scan."""
        if self.bot.database is None or message.author.bot or not message.content:
            return
        parsed = self.parse(message.content, message.created_at.date())
        if parsed is None:
            return
        # Only track channels a command has already initialised, so we never
        # create a half-populated window that a later query would trust.
        newest_id, oldest_after = await self.bot.database.get_leaderboard_scan(
            self.GAME, message.channel.id
        )
        if oldest_after is None:
            return
        played_on, payload = parsed
        await self.bot.database.upsert_leaderboard_result(
            self.GAME,
            message.channel.id,
            message.id,
            message.author.id,
            message.author.display_name,
            played_on,
            payload,
        )
        # Advance the scan pointer so a later command doesn't re-scan this tail.
        if newest_id is None or message.id > newest_id:
            await self.bot.database.set_leaderboard_scan(
                self.GAME, message.channel.id, message.id, oldest_after
            )
        await self.on_result_captured(message, played_on, payload)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Re-parse an edited message; drop it if it's no longer a result."""
        if self.bot.database is None:
            return
        _, oldest_after = await self.bot.database.get_leaderboard_scan(
            self.GAME, payload.channel_id
        )
        if oldest_after is None:
            return  # channel not initialised yet
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return
        if message.author.bot:
            return
        if not await self._store(message):
            await self.bot.database.delete_leaderboard_result(
                self.GAME, payload.message_id
            )

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Drop a cached result when its message is deleted."""
        if self.bot.database is None:
            return
        await self.bot.database.delete_leaderboard_result(
            self.GAME, payload.message_id
        )
