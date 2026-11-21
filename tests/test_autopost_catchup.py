"""Tests for recorded automatic posts and the startup catch-up."""

import datetime as real_dt
import logging
from types import SimpleNamespace

import pytest

from cogs import autopost as autopost_module
from cogs.autopost import AutoPost
from leaderboard.base import LeaderboardCog

CHANNEL = 100


class FakeGame:
    GAME = "krillion"
    SCAN_BUFFER = real_dt.timedelta(days=2)

    def __init__(self, day=47, daily=True):
        self.day = day
        self.daily = daily

    async def _sync_channel(self, channel, after):
        pass

    async def daily_post_day(self):
        return self.day

    async def build_daily_post(self, channel, day):
        return ("recap", "file") if self.daily else None

    async def build_leaderboard(self, channel, month_filter, label):
        return "board"


def clock(monkeypatch, when):
    """Freeze the autopost cog's clock at ``when`` (UTC)."""

    class Frozen(real_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return when

    monkeypatch.setattr(
        autopost_module,
        "dt",
        SimpleNamespace(
            datetime=Frozen,
            timezone=real_dt.timezone,
            timedelta=real_dt.timedelta,
            time=real_dt.time,
        ),
    )


def at(day, hour):
    return real_dt.datetime(2026, 11, day, hour, tzinfo=real_dt.timezone.utc)


@pytest.fixture
def sent():
    return []


@pytest.fixture
def make_cog(db, sent):
    """A fresh AutoPost (like a bot restart) sharing the same database."""

    async def send(embed=None, file=None):
        sent.append(embed)
        return SimpleNamespace(id=len(sent))

    channel = SimpleNamespace(id=CHANNEL, send=send)

    async def make(game=None):
        await db.set_autopost("krillion", CHANNEL, True)
        bot = SimpleNamespace(
            database=db,
            logger=logging.getLogger("test"),
            get_channel={CHANNEL: channel}.get,
        )
        cog = AutoPost(bot)
        fake = game or FakeGame()
        cog._games = lambda: {"krillion": fake}
        return cog

    return make


class TestDailyCatchUp:
    async def test_posts_a_missed_recap_once_its_time_has_passed(
        self, make_cog, monkeypatch, sent, db
    ):
        clock(monkeypatch, at(22, 14))  # 08:00 UTC has passed
        cog = await make_cog()
        await cog.on_ready()
        assert sent == ["recap"]
        assert await db.has_autopost("krillion", CHANNEL, "daily", "47")

    async def test_leaves_it_to_the_schedule_before_the_post_time(
        self, make_cog, monkeypatch, sent
    ):
        clock(monkeypatch, at(22, 6))
        cog = await make_cog()
        await cog.on_ready()
        assert sent == []

    async def test_runs_once_per_start_not_on_reconnect(self, make_cog, monkeypatch, sent):
        clock(monkeypatch, at(22, 14))
        cog = await make_cog(FakeGame(day=47))
        await cog.on_ready()
        cog._games()["krillion"].day = 48  # even if there were something new
        await cog.on_ready()
        assert sent == ["recap"]

    async def test_restart_after_the_scheduled_post_doesnt_repost(
        self, make_cog, monkeypatch, sent
    ):
        clock(monkeypatch, at(22, 8))
        scheduled = await make_cog()
        await scheduled.monthly_post.coro(scheduled)  # the 08:00 run
        clock(monkeypatch, at(22, 14))
        await (await make_cog()).on_ready()
        assert sent == ["recap"]

    async def test_nothing_to_post_isnt_recorded(self, make_cog, monkeypatch, sent, db):
        clock(monkeypatch, at(22, 14))
        await (await make_cog(FakeGame(daily=False))).on_ready()
        assert sent == []
        assert not await db.has_autopost("krillion", CHANNEL, "daily", "47")

    async def test_no_archived_day_means_no_post(self, make_cog, monkeypatch, sent):
        clock(monkeypatch, at(22, 14))
        await (await make_cog(FakeGame(day=None))).on_ready()
        assert sent == []


class TestMonthlyCatchUp:
    def period(self):
        _, _, (year, month) = LeaderboardCog._resolve_window(None)
        return f"{year:04d}-{month:02d}"

    async def board_posts(self, make_cog, monkeypatch, sent, when):
        clock(monkeypatch, when)
        # No daily recap, so only the monthly board can post.
        await (await make_cog(FakeGame(daily=False))).on_ready()
        return sent.count("board")

    async def test_posted_on_the_2nd_whatever_the_time(self, make_cog, monkeypatch, sent):
        assert await self.board_posts(make_cog, monkeypatch, sent, at(2, 6)) == 1

    async def test_posted_on_the_1st_after_the_post_time(self, make_cog, monkeypatch, sent):
        assert await self.board_posts(make_cog, monkeypatch, sent, at(1, 9)) == 1

    async def test_left_to_the_schedule_before_the_post_time_on_the_1st(
        self, make_cog, monkeypatch, sent
    ):
        assert await self.board_posts(make_cog, monkeypatch, sent, at(1, 6)) == 0

    async def test_dropped_after_the_catch_up_window(self, make_cog, monkeypatch, sent):
        assert await self.board_posts(make_cog, monkeypatch, sent, at(4, 9)) == 0

    async def test_not_reposted_once_recorded(self, make_cog, monkeypatch, sent, db):
        await db.record_autopost("krillion", CHANNEL, "monthly", self.period(), 1)
        assert await self.board_posts(make_cog, monkeypatch, sent, at(2, 9)) == 0
