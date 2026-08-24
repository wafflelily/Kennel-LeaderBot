"""Tests for month-window resolution and bounds in leaderboard.base."""

from datetime import datetime, timedelta, timezone

import pytest

from leaderboard.base import MONTHS, LeaderboardCog

resolve = LeaderboardCog._resolve_window
bounds = LeaderboardCog._month_bounds


class TestResolveWindowExplicit:
    def test_full_month_name_uses_current_year(self):
        year = datetime.now(timezone.utc).year
        after, label, month_filter = resolve("june")
        assert month_filter == (year, 6)
        assert after == datetime(year, 6, 1, tzinfo=timezone.utc)
        assert label == f"June {year}"

    def test_name_with_year(self):
        after, label, month_filter = resolve("Jun 2026")
        assert month_filter == (2026, 6)
        assert after == datetime(2026, 6, 1, tzinfo=timezone.utc)
        assert label == "June 2026"

    def test_full_name_with_year_and_mixed_case(self):
        _, label, month_filter = resolve("DECEMBER 2025")
        assert month_filter == (2025, 12)
        assert label == "December 2025"

    @pytest.mark.parametrize("text", ["2026-06", "2026/6", "2026-6"])
    def test_iso_formats(self, text):
        after, label, month_filter = resolve(text)
        assert month_filter == (2026, 6)
        assert after == datetime(2026, 6, 1, tzinfo=timezone.utc)

    def test_bare_month_number(self):
        year = datetime.now(timezone.utc).year
        _, _, month_filter = resolve("3")
        assert month_filter == (year, 3)

    def test_surrounding_whitespace_is_tolerated(self):
        _, _, month_filter = resolve("  august 2026  ")
        assert month_filter == (2026, 8)

    @pytest.mark.parametrize(
        "text", ["waffles", "13", "0", "2026-13", "2026-0", "junk 2026", "", "   "]
    )
    def test_unparseable_month_returns_none(self, text):
        assert resolve(text) is None


class TestResolveWindowDefault:
    @staticmethod
    def _expected(now):
        # Re-derive the documented rule: previous calendar month, unless today
        # is the last day of the current month.
        if (now + timedelta(days=1)).month != now.month:
            return now.year, now.month
        if now.month == 1:
            return now.year - 1, 12
        return now.year, now.month - 1

    def test_default_is_previous_month(self):
        before = datetime.now(timezone.utc)
        result = resolve(None)
        after_call = datetime.now(timezone.utc)
        if self._expected(before) != self._expected(after_call):
            pytest.skip("test straddled a month boundary")
        year, month = self._expected(before)

        after, label, month_filter = result
        assert month_filter == (year, month)
        assert after == datetime(year, month, 1, tzinfo=timezone.utc)
        assert label == f"{datetime(year, month, 1):%B %Y}"


class TestMonthBounds:
    def test_regular_month(self):
        assert bounds((2026, 8)) == ("2026-08-01", "2026-09-01")

    def test_december_rolls_into_next_year(self):
        assert bounds((2026, 12)) == ("2026-12-01", "2027-01-01")

    def test_january(self):
        assert bounds((2026, 1)) == ("2026-01-01", "2026-02-01")


class TestMonths:
    def test_all_twelve_months_present(self):
        assert sorted(set(MONTHS.values())) == list(range(1, 13))

    @pytest.mark.parametrize(
        "name,number",
        [("jan", 1), ("january", 1), ("sep", 9), ("sept", 9), ("september", 9),
         ("dec", 12), ("may", 5)],
    )
    def test_abbreviations_and_full_names(self, name, number):
        assert MONTHS[name] == number
