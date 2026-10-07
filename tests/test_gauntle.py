"""Tests for the Gauntle cog: parsing, year inference and time formatting."""

from datetime import date, datetime, timezone
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

# After Gauntle swapped Chromal and Shapeup out for Conduit and Angle.
NEW_LINEUP_SAMPLE = """I ran the October 7th Gauntle(t) in 5 minutes and 59.80 seconds!

🟩 Sudoku: 0:46.84 (−10s) ✨
🟨 Paire: 1:17.31 (+30s)
🟩 Wordy: 0:45.00 (−5s) ✨
🟩 Conduit: 0:13.64 (−15s) ✨
🟩 Clambers: 0:21.91 (−8s) ✨
🟩 Queens: 0:25.03 (−5s) ✨
🟩 Angle: 0:16.51 (−1s) ✨
🟩 Mines: 0:51.53 (−15s) ✨
🟩 Nonogram: 1:29.83 (−30s) ✨
🟩 Crossword: 0:29.43 (−20s) ✨
🟩 Ratiole: 0:04.87 (−19s) ✨
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

    @pytest.mark.parametrize(
        "message",
        [
            NEW_LINEUP_SAMPLE,
            # Emoji left as Discord shortcodes rather than rendered.
            NEW_LINEUP_SAMPLE.replace("🟩", ":green_square:")
            .replace("🟨", ":yellow_square:")
            .replace("✨", ":sparkles:"),
            # The share text's link line, left on instead of trimmed off.
            NEW_LINEUP_SAMPLE + "\nRun it yourself at https://gauntle.com/",
        ],
    )
    def test_new_lineup(self, cog, message):
        played_on, payload = cog.parse(message, date(2026, 10, 7))
        assert played_on == date(2026, 10, 7)
        assert payload["total"] == pytest.approx(5 * 60 + 59.80)
        assert sorted(payload["categories"]) == [
            "Angle", "Clambers", "Conduit", "Crossword", "Mines", "Nonogram",
            "Paire", "Queens", "Ratiole", "Sudoku", "Wordy",
        ]
        conduit = payload["categories"]["Conduit"]
        assert conduit["raw"] == pytest.approx(13.64)
        assert conduit["adj"] == pytest.approx(-15.0)

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


def run_text(day, minutes, categories=""):
    return (
        f"I ran the August {day}th Gauntlet in {minutes} minutes and 0 seconds!\n"
        + categories
    )


def live_message(mid, content, replies, author_id=1):
    async def reply(text, **kwargs):
        replies.append(text)

    return SimpleNamespace(
        id=mid,
        content=content,
        author=SimpleNamespace(id=author_id, display_name=f"p{author_id}", bot=False),
        channel=SimpleNamespace(id=100),
        created_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        reply=reply,
    )


class TestPersonalBestCelebration:
    """The live PB reply, through the real on_message listener."""

    @pytest.fixture
    def replies(self):
        return []

    @pytest.fixture
    async def cog(self, db):
        gauntle = Gauntle(SimpleNamespace(database=db))
        # Live capture only runs for channels a command has initialised.
        await db.set_leaderboard_scan(
            "gauntle", 100, None, "2026-08-01T00:00:00+00:00"
        )
        return gauntle

    async def test_first_run_sets_baseline_silently(self, cog, replies):
        await cog.on_message(live_message(1, run_text(5, 15), replies))
        assert replies == []

    async def test_overall_pb_gets_a_reply(self, cog, replies):
        await cog.on_message(live_message(1, run_text(5, 15), replies))
        await cog.on_message(live_message(2, run_text(6, 14), replies))
        assert replies == ["🏆 New personal best: 14:00.00!"]

    async def test_slower_run_stays_quiet(self, cog, replies):
        await cog.on_message(live_message(1, run_text(5, 14), replies))
        await cog.on_message(live_message(2, run_text(6, 15), replies))
        assert replies == []

    async def test_category_best_without_overall_best(self, cog, replies):
        await cog.on_message(
            live_message(1, run_text(5, 14, "Sudoku: 1:00.00 (−10s)"), replies)
        )
        # Slower run overall, but Sudoku improves from 50s to 40s effective.
        await cog.on_message(
            live_message(2, run_text(6, 16, "Sudoku: 0:50.00 (−10s)"), replies)
        )
        assert replies == ["✨ New personal category best: Sudoku (40.00s)"]

    async def test_first_time_category_is_silent(self, cog, replies):
        await cog.on_message(
            live_message(1, run_text(5, 14, "Sudoku: 1:00.00"), replies)
        )
        # Wordy has no history for this player; slower run overall too.
        await cog.on_message(
            live_message(2, run_text(6, 16, "Wordy: 2:00.00"), replies)
        )
        assert replies == []

    async def test_overall_and_category_bests_combine(self, cog, replies):
        await cog.on_message(
            live_message(1, run_text(5, 15, "Sudoku: 1:00.00"), replies)
        )
        await cog.on_message(
            live_message(2, run_text(6, 14, "Sudoku: 0:50.00"), replies)
        )
        assert replies == [
            "🏆 New personal best: 14:00.00!\n✨ New personal category best: Sudoku (50.00s)"
        ]

    async def test_bests_are_per_player(self, cog, replies):
        await cog.on_message(live_message(1, run_text(5, 14), replies, author_id=1))
        # Player 2's first run is slower than player 1's — still their baseline.
        await cog.on_message(live_message(2, run_text(6, 20), replies, author_id=2))
        assert replies == []

    async def test_history_scans_never_celebrate(self, cog, replies):
        await cog._store(live_message(1, run_text(5, 15), replies))
        # _store is the scan path; a faster old run must stay quiet.
        await cog._store(live_message(2, run_text(6, 10), replies))
        assert replies == []


class TestTeamScore:
    """The end-of-day sum of bests across every player."""

    CHANNEL = SimpleNamespace(id=100)  # no guild: stored names are used

    @pytest.fixture
    def cog(self, db):
        return Gauntle(SimpleNamespace(database=db))

    @staticmethod
    async def post(cog, mid, author_id, text):
        await cog._store(live_message(mid, text, [], author_id=author_id))

    async def test_needs_two_players(self, cog):
        await self.post(cog, 1, 1, run_text(5, 5, "Sudoku: 1:00.00"))
        await self.post(cog, 2, 1, run_text(5, 4, "Sudoku: 0:50.00"))
        assert await cog.build_team_score(self.CHANNEL, date(2026, 8, 5)) is None

    async def test_sums_each_games_best_across_players(self, cog):
        # Sudoku best is p2's 45s; Wordy best is p1's 100s -> team 2:25.
        await self.post(cog, 1, 1, run_text(5, 5, "Sudoku: 1:00.00 (−10s)\nWordy: 1:40.00"))
        await self.post(cog, 2, 2, run_text(5, 6, "Sudoku: 0:45.00\nWordy: 2:30.00 (−40s)"))
        text = await cog.build_team_score(self.CHANNEL, date(2026, 8, 5))
        assert text == (
            "🤝 **Team Gauntle for Aug 05: 2:25.00** — teamwork saved 2:35.00\n"
            "p1 ×1 · p2 ×1"
        )

    async def test_only_counts_that_days_runs(self, cog):
        await self.post(cog, 1, 1, run_text(5, 5, "Sudoku: 1:00.00"))
        await self.post(cog, 2, 2, run_text(6, 5, "Sudoku: 0:30.00"))
        assert await cog.build_team_score(self.CHANNEL, date(2026, 8, 5)) is None

    async def test_daily_task_posts_in_tracked_channels(self, cog, db):
        today = datetime.now(timezone.utc).date()
        header = f"I ran the {today:%B} {today.day}th Gauntlet in 5 minutes and 0 seconds!\n"
        sent = []

        async def send(text):
            sent.append(text)

        async def no_history():
            return
            yield

        channel = SimpleNamespace(
            id=100, send=send, history=lambda **kwargs: no_history()
        )
        cog.bot.get_channel = lambda cid: channel if cid == 100 else None
        await db.set_leaderboard_scan("gauntle", 100, None, "2026-01-01T00:00:00+00:00")
        for mid, author_id, sudoku in ((1, 1, "1:00.00"), (2, 2, "0:40.00")):
            message = live_message(mid, header + f"Sudoku: {sudoku}", [], author_id)
            message.created_at = datetime.now(timezone.utc)
            await cog._store(message)

        await cog.daily_team_score()
        assert len(sent) == 1
        assert sent[0].startswith(f"🤝 **Team Gauntle for {today:%b %d}: 40.00s**")
