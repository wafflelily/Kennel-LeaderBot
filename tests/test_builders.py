"""
Smoke tests for the extracted build_leaderboard methods, over the real
database. Runs with guild=None so names fall back to the stored ones.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from cogs.catfishing import Catfishing
from cogs.foodguessr import FoodGuessr
from cogs.gauntle import Gauntle


@pytest.fixture
def channel():
    return SimpleNamespace(id=100, guild=None)


class TestGauntleBuilder:
    async def test_empty_month_returns_none(self, db, channel):
        cog = Gauntle(SimpleNamespace(database=db))
        assert await cog.build_leaderboard(channel, (2026, 8), "August 2026") is None

    async def test_builds_ranking_and_category_table(self, db, channel):
        cog = Gauntle(SimpleNamespace(database=db))
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5),
            {"total": 951.34, "categories": {"Sudoku": {"raw": 52.24, "adj": -10.0}}},
        )
        # An old-format row (bare effective float) must still render.
        await db.upsert_leaderboard_result(
            "gauntle", 100, 2, 43, "bob", date(2026, 8, 6),
            {"total": 700.0, "categories": {"Sudoku": 60.0}},
        )
        embed = await cog.build_leaderboard(channel, (2026, 8), "August 2026")
        assert embed is not None
        # bob's 700s run is the fastest, so he leads the ranking.
        assert embed.description.index("bob") < embed.description.index("alice")

        table = embed.fields[0]
        assert table.name == "Best per category"
        # alice holds the Sudoku best: 42.24s effective from a 52.24s solve.
        assert "42.24s" in table.value
        assert "52.24s (−10s)" in table.value
        assert "alice" in table.value
        assert "August 2026" in embed.footer.text

    async def test_results_outside_the_month_are_excluded(self, db, channel):
        cog = Gauntle(SimpleNamespace(database=db))
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 7, 31),
            {"total": 100.0, "categories": {}},
        )
        assert await cog.build_leaderboard(channel, (2026, 8), "August 2026") is None


class TestFoodGuessrBuilder:
    async def test_empty_month_returns_none(self, db, channel):
        cog = FoodGuessr(SimpleNamespace(database=db))
        assert await cog.build_leaderboard(channel, (2026, 6), "June 2026") is None

    async def test_builds_ranking_and_perfects(self, db, channel):
        cog = FoodGuessr(SimpleNamespace(database=db))
        # alice posts twice the same day; only her best (a perfect) counts.
        await db.upsert_leaderboard_result(
            "foodguessr", 100, 1, 42, "alice", date(2026, 6, 18), {"score": 12000}
        )
        await db.upsert_leaderboard_result(
            "foodguessr", 100, 2, 42, "alice", date(2026, 6, 18), {"score": 15000}
        )
        await db.upsert_leaderboard_result(
            "foodguessr", 100, 3, 43, "bob", date(2026, 6, 18), {"score": 10000}
        )
        embed = await cog.build_leaderboard(channel, (2026, 6), "June 2026")
        assert embed is not None
        assert embed.description.index("alice") < embed.description.index("bob")
        assert "15,000" in embed.description

        perfects = embed.fields[0]
        assert perfects.name == "💯 Most perfects"
        assert "alice" in perfects.value
        assert "June 2026" in embed.footer.text


class TestCatfishingBuilder:
    async def test_builds_ranking_and_group_best(self, db, channel, monkeypatch):
        # Keep the "hardest answers" API fetch offline.
        async def no_stats(self, session, day):
            return None

        monkeypatch.setattr(Catfishing, "_fetch_puzzle", no_stats)
        cog = Catfishing(SimpleNamespace(database=db))
        await db.upsert_leaderboard_result(
            "catfishing", 100, 1, 42, "alice", date(2026, 8, 5),
            {"puzzle": 723, "score": 4.0, "correct": [4, 5, 6, 9]},
        )
        await db.upsert_leaderboard_result(
            "catfishing", 100, 2, 43, "bob", date(2026, 8, 5),
            {"puzzle": 723, "score": 2.0, "correct": [4, 5]},
        )
        embed = await cog.build_leaderboard(channel, (2026, 8), "August 2026")
        assert embed is not None
        assert embed.description.index("alice") < embed.description.index("bob")

        field_names = [field.name for field in embed.fields]
        assert "🤝 Group best" in field_names
        # With the API patched out there must be no hardest-answers field.
        assert "🧠 Hardest answers" not in field_names

    async def test_empty_month_returns_none(self, db, channel):
        cog = Catfishing(SimpleNamespace(database=db))
        assert await cog.build_leaderboard(channel, (2026, 8), "August 2026") is None
