"""Tests for the FoodGuessr cog's result parsing."""

from datetime import date
from types import SimpleNamespace

import pytest

from cogs.foodguessr import FoodGuessr

POSTED = date(2026, 6, 19)

FULL_SHARE = """FoodGuessr - Thursday, Jun 18, 2026 UTC
🌕🌕🌕🌑 3,500 ⋅ Round 1
🌕🌕🌕🌖 4,500 ⋅ Round 2
🌕🌕🌕🌑 3,500 ⋅ Round 3
Total score: 11,500/15,000
(+1,735 above today's average!) 🎉
"""


@pytest.fixture
def cog():
    return FoodGuessr(SimpleNamespace())


class TestTotalScoreFormat:
    def test_full_share_uses_in_message_date(self, cog):
        played_on, payload = cog.parse(FULL_SHARE, POSTED)
        assert played_on == date(2026, 6, 18)
        assert payload == {"score": 11500}

    def test_total_without_date_falls_back_to_post_date(self, cog):
        played_on, payload = cog.parse("Total score: 9,000/15,000", POSTED)
        assert played_on == POSTED
        assert payload == {"score": 9000}

    def test_invalid_in_message_date_falls_back_to_post_date(self, cog):
        message = "Wednesday, Jun 45, 2026\nTotal score: 9,000/15,000"
        played_on, _ = cog.parse(message, POSTED)
        assert played_on == POSTED


class TestIGotFormat:
    def test_i_got_line(self, cog):
        played_on, payload = cog.parse(
            "I got 12,345 on the FoodGuessr Daily!", POSTED
        )
        assert played_on == POSTED
        assert payload == {"score": 12345}

    def test_i_got_with_standalone_date_line(self, cog):
        message = "Tuesday, Jun 16, 2026\nI got 15,000 on the FoodGuessr Daily!"
        played_on, payload = cog.parse(message, POSTED)
        assert played_on == date(2026, 6, 16)
        assert payload == {"score": 15000}


class TestBareNumbersFormat:
    def test_valid_four_lines(self, cog):
        played_on, payload = cog.parse("13,000\n4,000\n5,000\n4,000", POSTED)
        assert played_on == POSTED
        assert payload == {"score": 13000}

    def test_round_at_maximum_is_allowed(self, cog):
        _, payload = cog.parse("15,000\n5,000\n5,000\n5,000", POSTED)
        assert payload == {"score": 15000}

    def test_round_over_maximum_rejected(self, cog):
        assert cog.parse("15,003\n5,001\n5,001\n5,001", POSTED) is None

    def test_total_not_matching_sum_rejected(self, cog):
        assert cog.parse("14,000\n4,000\n5,000\n4,000", POSTED) is None

    def test_wrong_line_count_rejected(self, cog):
        assert cog.parse("13,000\n4,000\n5,000", POSTED) is None

    def test_non_numeric_line_rejected(self, cog):
        assert cog.parse("13,000\n4,000\nfive thousand\n4,000", POSTED) is None

    def test_extra_chat_line_rejected(self, cog):
        assert cog.parse("13,000\n4,000\n5,000\n4,000\ngood round!", POSTED) is None


class TestNonResults:
    @pytest.mark.parametrize(
        "message",
        [
            "what did everyone get today?",
            "my total score was pretty bad",
            "42",
        ],
    )
    def test_ordinary_chat(self, cog, message):
        assert cog.parse(message, POSTED) is None
