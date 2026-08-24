"""Tests for the Catfishing cog: grid parsing, scoring and date anchoring."""

from datetime import date
from types import SimpleNamespace

import pytest

from cogs.catfishing import Catfishing, _fmt

POSTED = date(2026, 8, 22)

EMOJI_SHARE = """catfishing.net
#723 - 4/10
🐟🐟🐟🐟🐈
🐈🐈🐟🐟🐈
"""

TEXT_SHARE = """723 - 4/10
FFFFC
CCFFC
"""


@pytest.fixture
def cog():
    return Catfishing(SimpleNamespace())


class TestParse:
    def test_emoji_grid(self, cog):
        played_on, payload = cog.parse(EMOJI_SHARE, POSTED)
        assert played_on == POSTED
        assert payload["puzzle"] == 723
        assert payload["score"] == 4.0
        assert payload["correct"] == [4, 5, 6, 9]

    def test_text_grid_gives_same_result(self, cog):
        _, payload = cog.parse(TEXT_SHARE, POSTED)
        assert payload["puzzle"] == 723
        assert payload["score"] == 4.0
        assert payload["correct"] == [4, 5, 6, 9]

    def test_eggs_score_half_a_point(self, cog):
        message = "#700 - 1.5/10\n🐈🥚🐟🐟🐟\n🐟🐟🐟🐟🐟"
        _, payload = cog.parse(message, POSTED)
        assert payload["score"] == 1.5
        assert payload["correct"] == [0, 1]

    def test_hashless_decimal_score_line(self, cog):
        message = "694 - 3.5/10\n🐟🐟🐟🥚🐈\n🐟🐈🐟🐟🐈"
        _, payload = cog.parse(message, POSTED)
        assert payload["puzzle"] == 694
        assert payload["score"] == 3.5

    def test_score_derived_from_grid_not_stated_score(self, cog):
        # The stated "9/10" is wrong; the grid (1 cat) is authoritative.
        message = "#701 - 9/10\n🐈🐟🐟🐟🐟\n🐟🐟🐟🐟🐟"
        _, payload = cog.parse(message, POSTED)
        assert payload["score"] == 1.0

    def test_score_line_without_grid_is_not_a_result(self, cog):
        assert cog.parse("#723 - 4/10 what a day", POSTED) is None

    def test_grid_without_score_line_is_not_a_result(self, cog):
        assert cog.parse("🐟🐟🐟🐟🐈\n🐈🐈🐟🐟🐈", POSTED) is None

    def test_wrong_symbol_count_is_not_a_result(self, cog):
        message = "#723 - 4/10\n🐟🐟🐟🐟🐈\n🐈🐈🐟🐟"  # nine symbols
        assert cog.parse(message, POSTED) is None

    def test_text_grid_lines_with_other_words_are_ignored(self, cog):
        message = "#723 - 4/10\nFFFFC nice\nCCFFC"
        assert cog.parse(message, POSTED) is None

    def test_ordinary_chat(self, cog):
        assert cog.parse("I scored 4 out of 10 today", POSTED) is None


class TestDateAnchor:
    @staticmethod
    def _row(puzzle, played_on):
        return {"played_on": played_on, "payload": {"puzzle": puzzle}}

    def test_majority_offset_wins(self):
        rows = [
            self._row(720, date(2026, 8, 20)),
            self._row(721, date(2026, 8, 21)),
            self._row(722, date(2026, 8, 22)),
            # A late catch-up post two days after the puzzle's real day.
            self._row(720, date(2026, 8, 22)),
        ]
        anchor = Catfishing._date_anchor(rows)
        assert Catfishing._puzzle_date(721, anchor, date(2000, 1, 1)) == date(
            2026, 8, 21
        )
        # The late post is re-attributed to the puzzle's real date.
        assert Catfishing._puzzle_date(720, anchor, date(2000, 1, 1)) == date(
            2026, 8, 20
        )

    def test_no_rows_gives_no_anchor(self):
        assert Catfishing._date_anchor([]) is None

    def test_puzzle_date_without_anchor_falls_back_to_post_date(self):
        fallback = date(2026, 8, 22)
        assert Catfishing._puzzle_date(720, None, fallback) == fallback

    def test_puzzle_date_out_of_range_falls_back(self):
        fallback = date(2026, 8, 22)
        assert Catfishing._puzzle_date(10**9, 10**9, fallback) == fallback


class TestFormatting:
    def test_whole_number_has_no_decimal(self):
        assert _fmt(4.0) == "4"

    def test_half_point_kept(self):
        assert _fmt(3.5) == "3.5"
