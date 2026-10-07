"""Tests for Catfishing's stored puzzle stats: storage, refresh and throttling."""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import cogs.catfishing as catfishing_module
from cogs.catfishing import DATE_ANCHOR, Catfishing, RateLimited

UTC = timezone.utc


def make_cog(db):
    return Catfishing(SimpleNamespace(database=db, logger=logging.getLogger("test")))


def fake_api(cog, monkeypatch, stats=None):
    """Replace the network fetch; records which days were asked for."""
    asked = []

    async def fake_fetch(session, day):
        asked.append(day)
        await asyncio.sleep(0)
        if stats is not None and day not in stats:
            return None
        return stats[day] if stats else ([f"T{day}"], [float(day % 100)])

    monkeypatch.setattr(cog, "_fetch_puzzle", fake_fetch)
    return asked


def today_puzzle() -> int:
    return date.today().toordinal() - DATE_ANCHOR


class TestReleaseTime:
    def test_puzzle_comes_out_at_midnight_utc_plus_14(self):
        # #836 is the 2026-10-07 puzzle: out at 00:00 UTC+14 = 10:00 UTC the day before.
        assert Catfishing._released_at(836) == datetime(2026, 10, 6, 10, tzinfo=UTC)


class TestRefreshDue:
    released = Catfishing._released_at(836)

    def due(self, fetched_after, now_after):
        stored = {"fetched_at": (self.released + fetched_after).isoformat()}
        return Catfishing._refresh_due(836, stored, self.released + now_after)

    def test_fresh_fetch_waits_for_the_24_hour_mark(self):
        assert not self.due(timedelta(hours=2), timedelta(hours=23))
        assert self.due(timedelta(hours=2), timedelta(hours=25))

    def test_24_hour_fetch_waits_for_the_7_day_mark(self):
        assert not self.due(timedelta(hours=25), timedelta(days=6))
        assert self.due(timedelta(hours=25), timedelta(days=7, hours=1))

    def test_fetch_after_7_days_is_final(self):
        assert not self.due(timedelta(days=8), timedelta(days=400))

    def test_offline_through_both_marks_needs_one_refresh(self):
        # Fetched at 2h, bot offline until day 9: due now, then settled.
        assert self.due(timedelta(hours=2), timedelta(days=9))
        assert not self.due(timedelta(days=9), timedelta(days=10))


class TestStoredStats:
    async def test_fetched_stats_are_stored_and_survive_a_restart(self, db, monkeypatch):
        cog = make_cog(db)
        asked = fake_api(cog, monkeypatch)
        stats = await cog._puzzle_stats([700, 701])
        assert stats == {700: (["T700"], [0.0]), 701: (["T701"], [1.0])}
        assert sorted(asked) == [700, 701]
        stored = await db.get_puzzle_info("catfishing")
        assert stored[700]["titles"] == ["T700"]
        assert "fetched_at" in stored[700]

        # A "restarted" cog reads them back without touching the network.
        restarted = make_cog(db)
        asked_again = fake_api(restarted, monkeypatch)
        assert await restarted._puzzle_stats([700, 701]) == stats
        assert asked_again == []

    async def test_only_missing_puzzles_are_fetched(self, db, monkeypatch):
        cog = make_cog(db)
        asked = fake_api(cog, monkeypatch)
        await cog._puzzle_stats([700])
        asked.clear()
        await cog._puzzle_stats([700, 702])
        assert asked == [702]

    async def test_failures_are_not_stored_and_retried_later(self, db, monkeypatch):
        cog = make_cog(db)
        asked = fake_api(cog, monkeypatch, stats={})  # every fetch fails
        assert await cog._puzzle_stats([700]) == {700: None}
        assert await db.get_puzzle_info("catfishing") == {}
        await cog._puzzle_stats([700])
        assert asked == [700, 700]


class TestRefresher:
    async def test_refreshes_only_puzzles_past_a_mark(self, db, monkeypatch):
        cog = make_cog(db)
        fresh_day = today_puzzle() - 2  # out ~2 days ago, fetched in its first hour
        settled_day = today_puzzle() - 30  # fetched long after its 7-day mark
        early = Catfishing._released_at(fresh_day) + timedelta(hours=1)
        late = Catfishing._released_at(settled_day) + timedelta(days=20)
        await db.set_puzzle_info(
            "catfishing", fresh_day,
            {"titles": ["old"], "rates": [1.0], "fetched_at": early.isoformat()},
        )
        await db.set_puzzle_info(
            "catfishing", settled_day,
            {"titles": ["keep"], "rates": [2.0], "fetched_at": late.isoformat()},
        )
        asked = fake_api(cog, monkeypatch)

        await cog.stats_refresher()

        assert asked == [fresh_day]
        stored = await db.get_puzzle_info("catfishing")
        assert stored[fresh_day]["titles"] == [f"T{fresh_day}"]
        assert datetime.fromisoformat(stored[fresh_day]["fetched_at"]) > early
        assert stored[settled_day]["titles"] == ["keep"]

    async def test_failed_refresh_keeps_the_old_data(self, db, monkeypatch):
        cog = make_cog(db)
        day = today_puzzle() - 2
        early = Catfishing._released_at(day) + timedelta(hours=1)
        old = {"titles": ["old"], "rates": [1.0], "fetched_at": early.isoformat()}
        await db.set_puzzle_info("catfishing", day, old)
        fake_api(cog, monkeypatch, stats={})
        await cog.stats_refresher()
        assert (await db.get_puzzle_info("catfishing"))[day] == old


class TestThrottling:
    @pytest.fixture
    def sleeps(self, monkeypatch):
        """Record the cog's pauses, without actually waiting."""
        recorded = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay, *args, **kwargs):
            if delay:
                recorded.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(catfishing_module.asyncio, "sleep", fake_sleep)
        return recorded

    async def test_never_more_than_three_requests_at_once(self, db, monkeypatch, sleeps):
        cog = make_cog(db)
        in_flight, peak = 0, 0

        async def fake_fetch(session, day):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            in_flight -= 1
            return [f"T{day}"], [1.0]

        monkeypatch.setattr(cog, "_fetch_puzzle", fake_fetch)
        await cog._puzzle_stats(list(range(700, 730)))
        assert peak == catfishing_module.MAX_CONCURRENT_FETCHES

    async def test_big_batch_pauses_after_each_request(self, db, monkeypatch, sleeps):
        cog = make_cog(db)
        fake_api(cog, monkeypatch)
        await cog._puzzle_stats(list(range(700, 700 + catfishing_module.BIG_BATCH + 1)))
        assert sleeps == [catfishing_module.BIG_BATCH_DELAY] * (catfishing_module.BIG_BATCH + 1)

    async def test_small_batch_does_not_pause(self, db, monkeypatch, sleeps):
        cog = make_cog(db)
        fake_api(cog, monkeypatch)
        await cog._puzzle_stats(list(range(700, 707)))
        assert sleeps == []


class TestRateLimit:
    @pytest.fixture
    def sleeps(self, monkeypatch):
        recorded = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay, *args, **kwargs):
            if delay:
                recorded.append(delay)
            await real_sleep(0)

        monkeypatch.setattr(catfishing_module.asyncio, "sleep", fake_sleep)
        return recorded

    async def test_waits_as_asked_then_retries(self, db, monkeypatch, sleeps):
        cog = make_cog(db)
        attempts = []

        async def fake_fetch(session, day):
            attempts.append(day)
            if len(attempts) == 1:
                raise RateLimited(30)
            return [f"T{day}"], [1.0]

        monkeypatch.setattr(cog, "_fetch_puzzle", fake_fetch)
        assert await cog._puzzle_stats([700]) == {700: (["T700"], [1.0])}
        assert attempts == [700, 700]
        # The pause is the site's Retry-After (less a hair of elapsed time).
        assert len(sleeps) == 1 and 29 < sleeps[0] <= 30

    async def test_pause_holds_back_every_fetch(self, db, monkeypatch, sleeps):
        cog = make_cog(db)
        limited_once = False

        async def fake_fetch(session, day):
            nonlocal limited_once
            if day == 700 and not limited_once:
                limited_once = True
                raise RateLimited(30)
            await asyncio.sleep(0)
            return [f"T{day}"], [1.0]

        monkeypatch.setattr(cog, "_fetch_puzzle", fake_fetch)
        stats = await cog._puzzle_stats(list(range(700, 706)))
        assert all(v is not None for v in stats.values())
        # Fetches that started after the 429 waited out the same pause.
        assert sum(1 for s in sleeps if s > 25) >= 2

    async def test_gives_up_after_the_retries(self, db, monkeypatch, sleeps):
        cog = make_cog(db)
        attempts = []

        async def always_limited(session, day):
            attempts.append(day)
            raise RateLimited(1)

        monkeypatch.setattr(cog, "_fetch_puzzle", always_limited)
        assert await cog._puzzle_stats([700]) == {700: None}
        assert len(attempts) == catfishing_module.RATE_LIMIT_RETRIES + 1
        assert await db.get_puzzle_info("catfishing") == {}
