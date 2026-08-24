"""
Tests for LeaderboardCog's scan engine and live-capture listeners.

Uses a minimal concrete cog (parses "score N" messages), fake Discord
message/channel objects, and the real DatabaseManager over in-memory SQLite.
"""

import asyncio
import re
from datetime import datetime, timezone
from types import SimpleNamespace

import discord
import pytest

from leaderboard.base import LeaderboardCog


class ScoreGame(LeaderboardCog, name="scoregame"):
    GAME = "scoregame"

    def parse(self, content, posted_on):
        match = re.fullmatch(r"score (\d+)", content)
        if match is None:
            return None
        return posted_on, {"value": int(match.group(1))}


CHANNEL_ID = 100
AUG_1 = datetime(2026, 8, 1, tzinfo=timezone.utc)
JUL_1 = datetime(2026, 7, 1, tzinfo=timezone.utc)


def make_message(mid, content, *, when, author_id=1, name="alice", is_bot=False):
    return SimpleNamespace(
        id=mid,
        content=content,
        author=SimpleNamespace(id=author_id, display_name=name, bot=is_bot),
        channel=SimpleNamespace(id=CHANNEL_ID),
        created_at=when,
    )


class FakeChannel:
    """Just enough of a TextChannel for _sync_channel: id + history()."""

    def __init__(self, messages):
        self.id = CHANNEL_ID
        self.messages = messages
        self.history_calls = []

    def history(self, *, limit=None, after=None, before=None):
        self.history_calls.append((after, before))

        async def generate():
            for message in sorted(self.messages, key=lambda m: m.id):
                if isinstance(after, datetime) and message.created_at <= after:
                    continue
                if isinstance(after, discord.Object) and message.id <= after.id:
                    continue
                if isinstance(before, datetime) and message.created_at >= before:
                    continue
                await asyncio.sleep(0)  # yield control, like a real API call
                yield message

        return generate()

    def initial_scan_count(self):
        """History calls that fetched from a datetime with no upper bound."""
        return sum(
            1
            for after, before in self.history_calls
            if isinstance(after, datetime) and before is None
        )


@pytest.fixture
def cog(db):
    return ScoreGame(SimpleNamespace(database=db))


async def stored_values(db):
    rows = await db.get_leaderboard_results(
        "scoregame", CHANNEL_ID, "0000-01-01", "9999-12-31"
    )
    return [row["payload"]["value"] for row in rows]


class TestSyncChannel:
    async def test_first_scan_stores_results_and_scan_state(self, cog, db):
        channel = FakeChannel(
            [
                make_message(10, "score 5", when=datetime(2026, 8, 2, tzinfo=timezone.utc)),
                make_message(11, "just chatting", when=datetime(2026, 8, 3, tzinfo=timezone.utc)),
                make_message(12, "score 7", when=datetime(2026, 8, 4, tzinfo=timezone.utc), is_bot=True),
                make_message(13, "score 9", when=datetime(2026, 8, 5, tzinfo=timezone.utc)),
            ]
        )
        await cog._sync_channel(channel, AUG_1)

        # Bot messages and chat are skipped, results stored.
        assert await stored_values(db) == [5, 9]
        # Scan state: newest covers *everything* seen (even non-results), and
        # the backfill boundary is the requested `after`.
        newest_id, oldest_after = await db.get_leaderboard_scan("scoregame", CHANNEL_ID)
        assert newest_id == 13
        assert oldest_after == AUG_1.isoformat()

    async def test_second_sync_is_forward_only(self, cog, db):
        channel = FakeChannel(
            [make_message(10, "score 5", when=datetime(2026, 8, 2, tzinfo=timezone.utc))]
        )
        await cog._sync_channel(channel, AUG_1)

        channel.messages.append(
            make_message(20, "score 8", when=datetime(2026, 8, 10, tzinfo=timezone.utc))
        )
        channel.history_calls.clear()
        await cog._sync_channel(channel, AUG_1)

        # No full re-scan: the only call is a forward catch-up from the last
        # seen message id.
        assert channel.initial_scan_count() == 0
        assert len(channel.history_calls) == 1
        after, before = channel.history_calls[0]
        assert isinstance(after, discord.Object) and after.id == 10
        assert before is None

        assert await stored_values(db) == [5, 8]
        newest_id, _ = await db.get_leaderboard_scan("scoregame", CHANNEL_ID)
        assert newest_id == 20

    async def test_requesting_an_older_month_backfills_once(self, cog, db):
        channel = FakeChannel(
            [
                make_message(5, "score 3", when=datetime(2026, 7, 10, tzinfo=timezone.utc)),
                make_message(10, "score 5", when=datetime(2026, 8, 2, tzinfo=timezone.utc)),
            ]
        )
        # First sync only covers August; the July result is not cached.
        await cog._sync_channel(channel, AUG_1)
        assert await stored_values(db) == [5]

        # Asking for July triggers a backfill of exactly the July→August gap.
        channel.history_calls.clear()
        await cog._sync_channel(channel, JUL_1)
        assert await stored_values(db) == [3, 5]
        backfills = [
            (after, before)
            for after, before in channel.history_calls
            if before is not None
        ]
        assert len(backfills) == 1
        after, before = backfills[0]
        assert after == JUL_1
        assert before == datetime.fromisoformat(AUG_1.isoformat())

        _, oldest_after = await db.get_leaderboard_scan("scoregame", CHANNEL_ID)
        assert oldest_after == JUL_1.isoformat()

        # A third sync for July must not backfill again.
        channel.history_calls.clear()
        await cog._sync_channel(channel, JUL_1)
        assert channel.initial_scan_count() == 0
        assert all(before is None for _, before in channel.history_calls)

    async def test_concurrent_syncs_only_scan_history_once(self, cog, db):
        channel = FakeChannel(
            [
                make_message(i, f"score {i}", when=datetime(2026, 8, 2, i % 24, tzinfo=timezone.utc))
                for i in range(10, 15)
            ]
        )
        await asyncio.gather(
            cog._sync_channel(channel, AUG_1),
            cog._sync_channel(channel, AUG_1),
        )
        # The lock makes the second call wait; it then sees the first call's
        # scan state and does at most a cheap forward catch-up.
        assert channel.initial_scan_count() == 1
        assert await stored_values(db) == [10, 11, 12, 13, 14]


class TestOnMessage:
    async def test_uninitialised_channel_is_ignored(self, cog, db):
        message = make_message(10, "score 5", when=datetime(2026, 8, 2, tzinfo=timezone.utc))
        await cog.on_message(message)
        assert await stored_values(db) == []

    async def test_initialised_channel_captures_live_results(self, cog, db):
        await db.set_leaderboard_scan("scoregame", CHANNEL_ID, 10, AUG_1.isoformat())
        message = make_message(20, "score 5", when=datetime(2026, 8, 2, tzinfo=timezone.utc))
        await cog.on_message(message)
        assert await stored_values(db) == [5]
        # Scan pointer advances so a later command doesn't re-fetch this tail.
        newest_id, _ = await db.get_leaderboard_scan("scoregame", CHANNEL_ID)
        assert newest_id == 20

    async def test_non_results_and_bots_are_ignored(self, cog, db):
        await db.set_leaderboard_scan("scoregame", CHANNEL_ID, 10, AUG_1.isoformat())
        when = datetime(2026, 8, 2, tzinfo=timezone.utc)
        await cog.on_message(make_message(20, "hello!", when=when))
        await cog.on_message(make_message(21, "score 5", when=when, is_bot=True))
        assert await stored_values(db) == []
        newest_id, _ = await db.get_leaderboard_scan("scoregame", CHANNEL_ID)
        assert newest_id == 10


class TestEditAndDelete:
    @staticmethod
    def _wire_channel(cog, message):
        """Point bot.get_channel at a stub whose fetch_message returns `message`."""

        async def fetch_message(_mid):
            return message

        cog.bot.get_channel = lambda _cid: SimpleNamespace(fetch_message=fetch_message)

    async def test_edit_updates_the_cached_result(self, cog, db):
        await db.set_leaderboard_scan("scoregame", CHANNEL_ID, 10, AUG_1.isoformat())
        when = datetime(2026, 8, 2, tzinfo=timezone.utc)
        await cog._store(make_message(10, "score 5", when=when))

        self._wire_channel(cog, make_message(10, "score 6", when=when))
        await cog.on_raw_message_edit(
            SimpleNamespace(channel_id=CHANNEL_ID, message_id=10)
        )
        assert await stored_values(db) == [6]

    async def test_edit_away_from_a_result_drops_the_row(self, cog, db):
        await db.set_leaderboard_scan("scoregame", CHANNEL_ID, 10, AUG_1.isoformat())
        when = datetime(2026, 8, 2, tzinfo=timezone.utc)
        await cog._store(make_message(10, "score 5", when=when))

        self._wire_channel(cog, make_message(10, "never mind, miscounted", when=when))
        await cog.on_raw_message_edit(
            SimpleNamespace(channel_id=CHANNEL_ID, message_id=10)
        )
        assert await stored_values(db) == []

    async def test_edit_in_uninitialised_channel_is_ignored(self, cog, db):
        # No scan state at all: listener must bail before fetching anything.
        cog.bot.get_channel = lambda _cid: pytest.fail("should not fetch")
        await cog.on_raw_message_edit(
            SimpleNamespace(channel_id=CHANNEL_ID, message_id=10)
        )

    async def test_delete_removes_the_cached_result(self, cog, db):
        when = datetime(2026, 8, 2, tzinfo=timezone.utc)
        await cog._store(make_message(10, "score 5", when=when))
        assert await stored_values(db) == [5]

        await cog.on_raw_message_delete(SimpleNamespace(message_id=10))
        assert await stored_values(db) == []
