"""Tests for the slash-command autocomplete helpers in leaderboard.base."""

from datetime import datetime, timezone

from leaderboard.base import LeaderboardCog, game_choices, month_choices


class TestGameChoices:
    def test_all_games_sorted_when_nothing_typed(self):
        choices = game_choices({"gauntle", "catfishing", "foodguessr"}, "")
        assert [c.value for c in choices] == ["catfishing", "foodguessr", "gauntle"]

    def test_filters_by_typed_prefix(self):
        choices = game_choices({"gauntle", "catfishing"}, "GaU")
        assert [c.value for c in choices] == ["gauntle"]

    def test_no_match(self):
        assert game_choices({"gauntle"}, "zzz") == []


class TestMonthChoices:
    def test_last_twelve_months_newest_first(self):
        choices = month_choices("")
        assert len(choices) == 12
        now = datetime.now(timezone.utc)
        assert choices[0].value == f"{now.year}-{now.month:02d}"
        assert choices[0].name == f"{datetime(now.year, now.month, 1):%B %Y}"

    def test_values_are_accepted_by_resolve_window(self):
        for choice in month_choices(""):
            window = LeaderboardCog._resolve_window(choice.value)
            assert window is not None
            _, label, _ = window
            assert label == choice.name

    def test_filters_by_typed_text(self):
        target = month_choices("")[3]
        filtered = month_choices(target.name.lower()[:5])
        assert target.value in [c.value for c in filtered]
        assert len(filtered) < 12
