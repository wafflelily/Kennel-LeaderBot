"""Tests for each cog's compare_stats (the /mystats comparative numbers)."""

from datetime import date
from types import SimpleNamespace

import pytest

from cogs.catfishing import Catfishing
from cogs.foodguessr import FoodGuessr
from cogs.gauntle import Gauntle

P1, P2 = 1, 2


def row(author_id, played_on, payload):
    return {
        "author_id": author_id,
        "author_name": f"player{author_id}",
        "played_on": played_on,
        "payload": payload,
    }


class TestGauntleCompare:
    @pytest.fixture
    def cog(self):
        return Gauntle(SimpleNamespace())

    @pytest.fixture
    def rows(self):
        return [
            # Day A: contested, P1 fastest.
            row(P1, date(2026, 8, 1), {"total": 100.0, "categories": {"Sudoku": {"raw": 60.0, "adj": -10.0}}}),
            row(P2, date(2026, 8, 1), {"total": 120.0, "categories": {"Sudoku": {"raw": 55.0, "adj": 0.0}}}),
            # Day B: contested, P2 fastest. P2's category is an old-format row.
            row(P1, date(2026, 8, 2), {"total": 110.0, "categories": {"Mines": {"raw": 90.0, "adj": 0.0}}}),
            row(P2, date(2026, 8, 2), {"total": 105.0, "categories": {"Mines": 80.0}}),
            # Day C: P1 alone (must not count as a contested win).
            row(P1, date(2026, 8, 3), {"total": 90.0, "categories": {"Wordy": {"raw": 40.0, "adj": 0.0}}}),
        ]

    async def test_lines(self, cog, rows):
        lines = await cog.compare_stats(rows, P1)
        assert lines[0] == "Days run: **3**"
        assert "**1** of 2 contested days" in lines[1]
        # P1 average 100s = 1:40.00, channel average 105s: 5s faster.
        assert "1:40.00" in lines[2]
        assert "5.00s faster" in lines[2]
        # P1 holds Sudoku (50 effective vs 55) and Wordy (solo), not Mines.
        assert "Sudoku, Wordy" in lines[3]
        assert "(2 of 3)" in lines[3]

    async def test_old_format_category_row_can_hold_a_best(self, cog, rows):
        # P2's Mines (old-format float, 80s) beats P1's 90s.
        lines = await cog.compare_stats(rows, P2)
        assert "**Mines**" in lines[3]
        assert "(1 of 3)" in lines[3]

    async def test_no_bests_held(self, cog):
        rows = [
            row(P1, date(2026, 8, 1), {"total": 100.0, "categories": {"Sudoku": {"raw": 50.0, "adj": 0.0}}}),
            row(P2, date(2026, 8, 1), {"total": 120.0, "categories": {"Sudoku": {"raw": 55.0, "adj": 0.0}}}),
        ]
        lines = await cog.compare_stats(rows, P2)
        assert "none right now" in lines[3]

    async def test_same_day_reposts_keep_fastest_run(self, cog):
        rows = [
            row(P1, date(2026, 8, 1), {"total": 200.0, "categories": {}}),
            row(P1, date(2026, 8, 1), {"total": 100.0, "categories": {}}),
        ]
        lines = await cog.compare_stats(rows, P1)
        assert lines[0] == "Days run: **1**"
        assert "1:40.00" in lines[2]

    async def test_unknown_player_returns_none(self, cog, rows):
        assert await cog.compare_stats(rows, 999) is None

    async def test_empty_rows_return_none(self, cog):
        assert await cog.compare_stats([], P1) is None


class TestFoodGuessrCompare:
    @pytest.fixture
    def cog(self):
        return FoodGuessr(SimpleNamespace())

    @pytest.fixture
    def rows(self):
        return [
            row(P1, date(2026, 6, 1), {"score": 12000}),
            row(P2, date(2026, 6, 1), {"score": 15000}),
            row(P1, date(2026, 6, 2), {"score": 15000}),
            row(P2, date(2026, 6, 2), {"score": 10000}),
            row(P1, date(2026, 6, 3), {"score": 8000}),  # solo day
        ]

    async def test_lines(self, cog, rows):
        lines = await cog.compare_stats(rows, P1)
        assert lines[0] == "Days played: **3**"
        assert "**1** of 2 contested days" in lines[1]
        # P1 average 11,667 vs channel average 12,000.
        assert "11,667" in lines[2]
        assert "12,000" in lines[2]
        assert "-333" in lines[2]
        assert "**1** of the channel's 2" in lines[3]

    async def test_perfects_line_omitted_when_channel_has_none(self, cog):
        rows = [
            row(P1, date(2026, 6, 1), {"score": 9000}),
            row(P2, date(2026, 6, 1), {"score": 8000}),
        ]
        lines = await cog.compare_stats(rows, P1)
        assert len(lines) == 3

    async def test_same_day_reposts_keep_best_score(self, cog):
        rows = [
            row(P1, date(2026, 6, 1), {"score": 9000}),
            row(P1, date(2026, 6, 1), {"score": 14000}),
        ]
        lines = await cog.compare_stats(rows, P1)
        assert lines[0] == "Days played: **1**"
        assert "14,000" in lines[2]

    async def test_unknown_player_returns_none(self, cog, rows):
        assert await cog.compare_stats(rows, 999) is None


class TestCatfishingCompare:
    @pytest.fixture
    def cog(self, monkeypatch):
        """A Catfishing cog with the puzzle-stats API replaced by a canned map.

        Tests put ``(titles, rates)`` tuples into ``cog.stats_map`` per puzzle;
        anything missing behaves like an API failure (None).
        """
        catfishing = Catfishing(SimpleNamespace())
        catfishing.stats_map = {}

        async def fake_fetch(session, day):
            return catfishing.stats_map.get(day)

        monkeypatch.setattr(catfishing, "_fetch_puzzle", fake_fetch)
        return catfishing

    @pytest.fixture
    def rows(self):
        return [
            # Shared puzzle: P1 got questions 0 and 2 that P2 missed, and won.
            row(P1, date(2026, 8, 1), {"puzzle": 700, "score": 3.0, "correct": [0, 1, 2]}),
            row(P2, date(2026, 8, 1), {"puzzle": 700, "score": 2.0, "correct": [1, 3]}),
            # Shared puzzle: nothing unique for P1, and P2 won.
            row(P1, date(2026, 8, 2), {"puzzle": 701, "score": 1.0, "correct": [5]}),
            row(P2, date(2026, 8, 2), {"puzzle": 701, "score": 2.0, "correct": [5, 6]}),
            # Solo puzzle: excluded from every comparison.
            row(P1, date(2026, 8, 3), {"puzzle": 702, "score": 1.0, "correct": [9]}),
        ]

    async def test_lines(self, cog, rows):
        lines = await cog.compare_stats(rows, P1)
        assert lines[0] == "Days played: **3**"
        assert "**2** across 2 shared puzzles" in lines[1]
        assert "**1** of 2 shared puzzles" in lines[2]
        # P1 average (3+1+1)/3 vs channel average (3+2+1+2+1)/5.
        assert "1.67/10" in lines[3]
        assert "1.80/10" in lines[3]

    async def test_solo_play_gives_zero_unique_answers(self, cog):
        rows = [row(P1, date(2026, 8, 1), {"puzzle": 700, "score": 5.0, "correct": [0, 1, 2, 3, 4]})]
        lines = await cog.compare_stats(rows, P1)
        assert "**0** across 0 shared puzzles" in lines[1]

    async def test_reposts_of_a_puzzle_union_their_answers(self, cog):
        rows = [
            row(P1, date(2026, 8, 1), {"puzzle": 700, "score": 1.0, "correct": [0]}),
            row(P1, date(2026, 8, 1), {"puzzle": 700, "score": 1.0, "correct": [2]}),
            row(P2, date(2026, 8, 1), {"puzzle": 700, "score": 1.0, "correct": [1]}),
        ]
        lines = await cog.compare_stats(rows, P1)
        assert "**2** across 1 shared puzzles" in lines[1]

    async def test_unknown_player_returns_none(self, cog, rows):
        assert await cog.compare_stats(rows, 999) is None

    async def test_api_failure_skips_the_answer_lists(self, cog, rows):
        # stats_map is empty, so every fetch "fails"; the counts must survive.
        lines = await cog.compare_stats(rows, P1)
        assert len(lines) == 4
        assert not any("Hardest" in line for line in lines)

    async def test_hardest_and_easiest_unique_answer_lists(self, cog):
        # P1 uniquely solved all ten questions of a shared puzzle.
        rows = [
            row(P1, date(2026, 8, 1), {"puzzle": 700, "score": 10.0, "correct": list(range(10))}),
            row(P2, date(2026, 8, 1), {"puzzle": 700, "score": 0.0, "correct": []}),
        ]
        titles = [f"Answer{i}" for i in range(10)]
        rates = [5.0, 10.0, 20.0, 30.0, 40.0, 60.0, 70.0, 80.0, 90.0, 95.0]
        cog.stats_map[700] = (titles, rates)

        lines = await cog.compare_stats(rows, P1)
        hardest_at = lines.index(
            "**Hardest answers nobody else got** (global solve rate)"
        )
        easiest_at = lines.index("**Easiest answers nobody else got**")

        hardest = lines[hardest_at + 1 : easiest_at]
        easiest = lines[easiest_at + 1 :]
        assert hardest == [
            f"• Answer{i} — {rates[i]:.1f}% (#700)" for i in range(5)
        ]
        # Easiest come from the other end, most-solved first, no overlap.
        assert easiest == [
            f"• Answer{i} — {rates[i]:.1f}% (#700)" for i in (9, 8, 7, 6, 5)
        ]

    async def test_few_uniques_show_only_the_hardest_list(self, cog, rows):
        cog.stats_map[700] = (
            ["Cat A", "Cat B", "Cat C", "Cat D"],
            [50.0, 30.0, 8.0, 90.0],
        )
        lines = await cog.compare_stats(rows, P1)
        # P1's uniques on puzzle 700 are questions 0 and 2 — both fit in the
        # hardest list, so there is nothing left for an easiest list.
        assert "**Hardest answers nobody else got** (global solve rate)" in lines
        assert lines[-2] == "• Cat C — 8.0% (#700)"
        assert lines[-1] == "• Cat A — 50.0% (#700)"
        assert not any("Easiest" in line for line in lines)

    async def test_missing_rates_are_skipped(self, cog, rows):
        cog.stats_map[700] = (["Cat A", "Cat B", "Cat C"], [None, 40.0, 25.0])
        lines = await cog.compare_stats(rows, P1)
        # Question 0's rate is unknown; only question 2 can be listed.
        assert lines[-1] == "• Cat C — 25.0% (#700)"
        assert not any("Cat A" in line for line in lines)
