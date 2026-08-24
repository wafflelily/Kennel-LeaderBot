"""Tests for the Gauntle cog: parsing, year inference and time formatting."""

from datetime import date
from types import SimpleNamespace

import pytest

from cogs.gauntle import Gauntle, _fmt_solve, _fmt_time

SAMPLE = """I ran the August 20th Gauntlet in 15 minutes and 51.34 seconds!

🟩 Sudoku: 0:52.24 (−10s) ✨
🟨 Crossword: 1:44.54 (−12s)
🟩 Queens: 0:21.11 (−5s) ✨
🟩 Chromal: 0:20.54 (−10s) ✨
🟨 Wordy: 2:08.24 (+5s)
🟥 Clambers: 0:45.30 (skip +90s)
🟥 Nonogram: 4:30.78 (skip +90s)
🟩 Mines: 1:40.31 (−15s) ✨
🟨 Shapeup: 0:07.71 (+1s)
🟩 Ratiole: 0:04.63 (−7s) ✨
🟩 Paire: 0:43.21 (−10s) ✨
"""


@pytest.fixture
def cog():
    return Gauntle(SimpleNamespace())


class TestParse:
    def test_full_sample(self, cog):
        played_on, payload = cog.parse(SAMPLE, date(2026, 8, 20))
        assert played_on == date(2026, 8, 20)
        assert payload["total"] == pytest.approx(15 * 60 + 51.34)
        assert len(payload["categories"]) == 11

        sudoku = payload["categories"]["Sudoku"]
        assert sudoku["raw"] == pytest.approx(52.24)
        assert sudoku["adj"] == pytest.approx(-10.0)

        # Skip penalties are positive adjustments.
        clambers = payload["categories"]["Clambers"]
        assert clambers["raw"] == pytest.approx(45.30)
        assert clambers["adj"] == pytest.approx(90.0)

        # Seconds-only time with no minutes prefix would also parse (Ratiole
        # here has one); a big bonus can push the effective time negative.
        ratiole = payload["categories"]["Ratiole"]
        assert ratiole["raw"] + ratiole["adj"] == pytest.approx(-2.37)

    def test_ascii_hyphen_bonus(self, cog):
        message = (
            "I ran the June 5th Gauntlet in 2 minutes and 3 seconds!\n"
            "Sudoku: 0:52.24 (-10s)"
        )
        _, payload = cog.parse(message, date(2026, 6, 5))
        assert payload["categories"]["Sudoku"]["adj"] == pytest.approx(-10.0)

    def test_category_without_adjustment(self, cog):
        message = (
            "I ran the June 5th Gauntlet in 2 minutes and 3 seconds!\n"
            "Sudoku: 0:52.24"
        )
        _, payload = cog.parse(message, date(2026, 6, 5))
        assert payload["categories"]["Sudoku"]["adj"] == 0.0

    def test_duration_with_hours(self, cog):
        message = "I ran the June 5th Gauntlet in 1 hour and 1 minute and 1.5 seconds!"
        _, payload = cog.parse(message, date(2026, 6, 5))
        assert payload["total"] == pytest.approx(3661.5)

    def test_duplicate_category_keeps_best_effective_time(self, cog):
        message = (
            "I ran the June 5th Gauntlet in 2 minutes and 3 seconds!\n"
            "Sudoku: 1:00.00 (+0s)\n"
            "Sudoku: 0:30.00 (+5s)"
        )
        _, payload = cog.parse(message, date(2026, 6, 5))
        sudoku = payload["categories"]["Sudoku"]
        assert sudoku["raw"] + sudoku["adj"] == pytest.approx(35.0)

    def test_stops_after_eleven_categories(self, cog):
        lines = [f"Cat{chr(ord('A') + i)}: 0:0{i % 10}.00" for i in range(13)]
        message = (
            "I ran the June 5th Gauntlet in 2 minutes and 3 seconds!\n"
            + "\n".join(lines)
        )
        _, payload = cog.parse(message, date(2026, 6, 5))
        assert len(payload["categories"]) == 11
        assert "CatL" not in payload["categories"]

    def test_ordinary_chat_is_not_a_result(self, cog):
        assert cog.parse("gg everyone, nice runs today", date(2026, 6, 5)) is None

    def test_header_without_gauntle_is_not_a_result(self, cog):
        message = "I ran the June 5th marathon in 40 minutes and 2 seconds!"
        assert cog.parse(message, date(2026, 6, 5)) is None

    def test_header_without_duration_is_not_a_result(self, cog):
        message = "I ran the June 5th Gauntlet and it was brutal!"
        assert cog.parse(message, date(2026, 6, 5)) is None


class TestYearInference:
    def test_same_month_uses_post_year(self, cog):
        message = "I ran the August 20th Gauntlet in 2 minutes and 3 seconds!"
        played_on, _ = cog.parse(message, date(2026, 8, 21))
        assert played_on == date(2026, 8, 20)

    def test_december_result_posted_in_january_lands_in_previous_year(self, cog):
        message = "I ran the December 31st Gauntlet in 2 minutes and 3 seconds!"
        played_on, _ = cog.parse(message, date(2026, 1, 2))
        assert played_on == date(2025, 12, 31)

    def test_january_result_posted_in_december_lands_in_next_year(self, cog):
        message = "I ran the January 1st Gauntlet in 2 minutes and 3 seconds!"
        played_on, _ = cog.parse(message, date(2025, 12, 31))
        assert played_on == date(2026, 1, 1)

    def test_feb_29_picks_the_leap_year_candidate(self, cog):
        message = "I ran the February 29th Gauntlet in 2 minutes and 3 seconds!"
        # 2025 has no Feb 29; of the candidate years only 2024 does.
        played_on, _ = cog.parse(message, date(2025, 3, 1))
        assert played_on == date(2024, 2, 29)


class TestFormatting:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (42.24, "42.24s"),
            (92.54, "1:32.54"),
            (60.0, "1:00.00"),
            (0.0, "0.00s"),
            (-2.37, "-2.37s"),
            (615.3, "10:15.30"),
        ],
    )
    def test_fmt_time(self, seconds, expected):
        assert _fmt_time(seconds) == expected

    def test_fmt_solve_unknown_raw_is_blank(self):
        assert _fmt_solve(None, None) == ""

    def test_fmt_solve_without_adjustment_omits_parens(self):
        assert _fmt_solve(52.24, 0.0) == "52.24s"

    def test_fmt_solve_bonus_uses_minus_sign(self):
        assert _fmt_solve(52.24, -10.0) == "52.24s (−10s)"

    def test_fmt_solve_penalty(self):
        assert _fmt_solve(45.3, 90.0) == "45.30s (+90s)"

    def test_fmt_solve_fractional_adjustment(self):
        assert _fmt_solve(70.0, 1.5) == "1:10.00 (+1.5s)"
