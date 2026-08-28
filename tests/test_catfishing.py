"""Tests for the Catfishing cog: grid parsing, scoring and date anchoring."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from cogs.catfishing import GROUP_COMPLETE_MESSAGE, Catfishing, _fmt

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


class RecordingChannel:
    def __init__(self):
        self.id = 100
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append(content)


def live_message(mid, content, channel, author_id=1):
    return SimpleNamespace(
        id=mid,
        content=content,
        author=SimpleNamespace(id=author_id, display_name=f"p{author_id}", bot=False),
        channel=channel,
        created_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
    )


NINE_CORRECT = "#700 - 9/10\n🐈🐈🐈🐈🐈\n🐈🐈🐈🐈🐟"
LAST_PIECE = "#700 - 1/10\n🐟🐟🐟🐟🐟\n🐟🐟🐟🐟🐈"
ALL_TEN = "#701 - 10/10\n🐈🐈🐈🐈🐈\n🐈🐈🐈🐈🐈"


class TestGroupCompleteCelebration:
    """The live 10/10 celebration, through the real on_message listener."""

    @pytest.fixture
    def channel(self):
        return RecordingChannel()

    @pytest.fixture
    async def cog(self, db):
        catfishing = Catfishing(SimpleNamespace(database=db))
        # Live capture only runs for channels a command has initialised.
        await db.set_leaderboard_scan(
            "catfishing", 100, None, "2026-08-01T00:00:00+00:00"
        )
        return catfishing

    async def test_completing_message_triggers_one_celebration(self, cog, channel):
        await cog.on_message(live_message(1, NINE_CORRECT, channel, author_id=1))
        assert channel.sent == []  # 9/10 covered: not yet

        await cog.on_message(live_message(2, LAST_PIECE, channel, author_id=2))
        assert channel.sent == [GROUP_COMPLETE_MESSAGE]

    async def test_no_second_celebration_once_complete(self, cog, channel):
        await cog.on_message(live_message(1, NINE_CORRECT, channel, author_id=1))
        await cog.on_message(live_message(2, LAST_PIECE, channel, author_id=2))
        # A third result re-covering already-covered questions changes nothing.
        await cog.on_message(live_message(3, LAST_PIECE, channel, author_id=3))
        assert channel.sent == [GROUP_COMPLETE_MESSAGE]

    async def test_solo_perfect_counts(self, cog, channel):
        await cog.on_message(live_message(1, ALL_TEN, channel))
        assert channel.sent == [GROUP_COMPLETE_MESSAGE]

    async def test_history_scans_never_celebrate(self, cog, channel):
        # _store is the path history scans take; old completions stay quiet.
        await cog._store(live_message(1, ALL_TEN, channel))
        assert channel.sent == []

    async def test_non_results_do_nothing(self, cog, channel):
        await cog.on_message(live_message(1, "gg everyone", channel))
        assert channel.sent == []
