"""Tests for the Krillion cog: parsing, leaderboard building and /mystats."""

import logging
from datetime import date
from types import SimpleNamespace

import pytest

from cogs.krillion import Krillion

POSTED = date(2026, 8, 30)

FULL_SHARE = """Krillion #47 🦐
260

🦑🦑🫧🦑🐟🫧🐟
"""

BLACK_SQUARE_SHARE = """Krillion #46 🦐
220

🐟🫧🦑⬛🐟🐟🦑
"""

PACK_SHARE = """Krillion 🎬 #1 🦐
380

🐟🦑🐟🐟🏮🦑🏮
"""


@pytest.fixture
def cog():
    return Krillion(SimpleNamespace())


class TestParse:
    def test_full_share(self, cog):
        played_on, payload = cog.parse(FULL_SHARE, POSTED)
        assert played_on == POSTED
        assert payload["puzzle"] == 47
        assert payload["score"] == 260
        assert payload["answers"] == [60, 60, 10, 60, 30, 10, 30]

    def test_black_square_scores_zero(self, cog):
        _, payload = cog.parse(BLACK_SQUARE_SHARE, POSTED)
        assert payload["score"] == 220
        assert payload["answers"] == [30, 10, 60, 0, 30, 30, 60]

    def test_lanternfish_and_shrimp_values(self, cog):
        message = "Krillion #10 🦐\n385\n\n🌟🏮🦑🐟🫧⬛🌟"
        _, payload = cog.parse(message, POSTED)
        assert payload["answers"] == [100, 85, 60, 30, 10, 0, 100]
        assert payload["score"] == 385

    def test_too_smart_value(self, cog):
        message = "Krillion #51 🦐\n400\n\n🦑🤡🦑🦑🦑🦑🏮"
        _, payload = cog.parse(message, POSTED)
        assert payload["answers"] == [60, 15, 60, 60, 60, 60, 85]
        assert payload["score"] == 400

    def test_grid_is_authoritative_over_stated_score(self, cog):
        message = "Krillion #10 🦐\n999\n\n🦑🦑🫧🦑🐟🫧🐟"
        _, payload = cog.parse(message, POSTED)
        assert payload["score"] == 260

    def test_variation_selectors_are_tolerated(self, cog):
        message = "Krillion #12 🦐\n220\n\n🐟️🫧🦑⬛️🐟🐟🦑"
        _, payload = cog.parse(message, POSTED)
        assert payload["answers"] == [30, 10, 60, 0, 30, 30, 60]

    def test_header_without_grid_uses_stated_score(self, cog):
        _, payload = cog.parse("Krillion #47 🦐\n260", POSTED)
        assert payload == {"puzzle": 47, "score": 260, "answers": None}

    def test_bare_form(self, cog):
        played_on, payload = cog.parse("#47\n265", POSTED)
        assert played_on == POSTED
        assert payload == {"puzzle": 47, "score": 265, "answers": None}

    def test_bare_form_with_blank_line(self, cog):
        _, payload = cog.parse("#47\n\n265", POSTED)
        assert payload["score"] == 265

    def test_pack_results_are_ignored(self, cog):
        assert cog.parse(PACK_SHARE, POSTED) is None

    @pytest.mark.parametrize(
        "message",
        [
            "#47\n263",  # not a multiple of 5
            "#47\n705",  # above the 700 maximum
            "#47\n265\nnice one",  # bare form must be the whole message
            "Krillion #47 🦐\n263",  # implausible stated score
            "Krillion #47 🦐\ngreat dive today!",  # no score at all
            "what's krillion?",  # ordinary chat
            "I got 265 today",  # ordinary chat with a number
        ],
    )
    def test_non_results(self, cog, message):
        assert cog.parse(message, POSTED) is None

    def test_comment_after_grid_is_ignored(self, cog):
        message = "Krillion #49 🦐\n280\n\n🏮⬛🫧🫧🐟🦑🏮 got screwed on a couple today"
        _, payload = cog.parse(message, POSTED)
        assert payload["answers"] == [85, 0, 10, 10, 30, 60, 85]
        assert payload["score"] == 280

    def test_comment_without_a_space_is_ignored(self, cog):
        message = "Krillion #49 🦐\n280\n\n🏮⬛🫧🫧🐟🦑🏮!!"
        _, payload = cog.parse(message, POSTED)
        assert payload["answers"] == [85, 0, 10, 10, 30, 60, 85]

    def test_eight_leading_symbols_is_not_a_grid(self, cog):
        # A trailing comment can't hide an extra answer symbol.
        message = "Krillion #49 🦐\n280\n\n🏮⬛🫧🫧🐟🦑🏮 🦑 oops"
        _, payload = cog.parse(message, POSTED)
        assert payload == {"puzzle": 49, "score": 280, "answers": None}

    def test_text_before_grid_is_not_a_grid(self, cog):
        message = "Krillion #49 🦐\n280\n\ntoday: 🏮⬛🫧🫧🐟🦑🏮"
        _, payload = cog.parse(message, POSTED)
        assert payload["answers"] is None

    def test_wrong_grid_size_falls_back_to_stated_score(self, cog):
        message = "Krillion #47 🦐\n260\n\n🦑🦑🫧🦑🐟🫧"  # six symbols
        _, payload = cog.parse(message, POSTED)
        assert payload == {"puzzle": 47, "score": 260, "answers": None}


def row(author_id, played_on, payload):
    return {
        "author_id": author_id,
        "author_name": f"player{author_id}",
        "played_on": played_on,
        "payload": payload,
    }


P1, P2 = 1, 2


class TestCompareStats:
    @pytest.fixture
    def cog(self, db):
        # compare_stats reads the prompt archive, so it needs the database.
        return Krillion(SimpleNamespace(database=db))

    @pytest.fixture
    def rows(self):
        return [
            row(P1, date(2026, 8, 29), {"puzzle": 46, "score": 385, "answers": [100, 85, 60, 30, 10, 0, 100]}),
            row(P2, date(2026, 8, 29), {"puzzle": 46, "score": 220, "answers": [30, 10, 60, 0, 30, 30, 60]}),
            row(P1, date(2026, 8, 30), {"puzzle": 47, "score": 260, "answers": [60, 60, 10, 60, 30, 10, 30]}),
            row(P2, date(2026, 8, 30), {"puzzle": 47, "score": 265, "answers": None}),
            row(P1, date(2026, 8, 31), {"puzzle": 48, "score": 100, "answers": None}),  # solo day
        ]

    async def test_lines(self, cog, rows):
        lines = await cog.compare_stats(rows, P1)
        assert lines[0] == "Days played: **3**"
        # P1 wins day 46 (385 vs 220), loses day 47 (260 vs 265).
        assert lines[1] == "🏆 Top score of the day: **1** of 2 shared days"
        # P1 average (385+260+100)/3 ≈ 248 vs channel (385+220+260+265+100)/5 = 246.
        assert lines[2] == "📈 Average: **248** vs the channel's 246"

    async def test_answer_counts(self, cog, rows):
        lines = await cog.compare_stats(rows, P1)
        counts_at = lines.index("### Answer counts")
        # P1's grids: [100,85,60,30,10,0,100] and [60,60,10,60,30,10,30].
        assert lines[counts_at + 1 :] == [
            "🌟 100 pts — **2**",
            "🏮 85 pts — **1**",
            "🦑 60 pts — **4**",
            "🐟 30 pts — **3**",
            "🫧 10 pts — **3**",
        ]

    async def test_answer_counts_include_too_smart_once_caught(self, cog):
        rows = [
            row(P1, date(2026, 9, 4), {"puzzle": 51, "score": 400, "answers": [60, 15, 60, 60, 60, 60, 85]}),
        ]
        lines = await cog.compare_stats(rows, P1)
        counts_at = lines.index("### Answer counts")
        assert lines[counts_at + 1 : counts_at + 7] == [
            "🌟 100 pts — **0**",
            "🏮 85 pts — **1**",
            "🦑 60 pts — **5**",
            "🐟 30 pts — **0**",
            "🤡 15 pts — **1**",
            "🫧 10 pts — **0**",
        ]

    async def test_too_smart_catches_name_the_answer(self, cog, db):
        rows = [
            row(P1, date(2026, 9, 4), {"puzzle": 51, "score": 400, "answers": [60, 15, 60, 60, 60, 60, 85]}),
        ]
        too_smart = [[] for _ in range(7)]
        too_smart[1] = ["Human"]
        await db.set_puzzle_info(
            "krillion",
            51,
            {
                "prompts": [f"Q{i}" for i in range(1, 8)],
                "shrimp": [[f"A{i}"] for i in range(1, 8)],
                "too_smart": too_smart,
            },
        )
        lines = await cog.compare_stats(rows, P1)
        assert "### Shrimp catches 🌟" not in lines
        catches_at = lines.index("### Too smart catches 🤡")
        assert lines[catches_at + 1 :] == ["• Q2 → **Human** (#51)"]

    async def test_no_too_smart_section_without_a_catch(self, cog, db, rows):
        await db.set_puzzle_info(
            "krillion", 46, {"prompts": [f"Q{i}" for i in range(1, 8)]}
        )
        lines = await cog.compare_stats(rows, P1)
        assert "### Too smart catches 🤡" not in lines

    async def test_no_grids_means_no_counts_section(self, cog):
        rows = [row(P1, date(2026, 8, 30), {"puzzle": 47, "score": 265, "answers": None})]
        lines = await cog.compare_stats(rows, P1)
        assert "### Answer counts" not in lines

    async def test_unknown_player_returns_none(self, cog, rows):
        assert await cog.compare_stats(rows, 999) is None

    async def test_shrimp_catches_name_the_specific_answer(self, cog, db, rows):
        await db.set_puzzle_info(
            "krillion",
            46,
            {
                "prompts": [f"Q{i}" for i in range(1, 8)],
                "shrimp": [[f"A{i}"] for i in range(1, 8)],
            },
        )
        lines = await cog.compare_stats(rows, P1)
        catches_at = lines.index("### Shrimp catches 🌟")
        # P1's day-46 grid has shrimps in slots 0 and 6 → answers A1 and A7;
        # day 47 isn't archived, so its shrimp-free grid adds nothing.
        assert lines[catches_at + 1 :] == [
            "• Q1 → **A1** (#46)",
            "• Q7 → **A7** (#46)",
        ]

    async def test_shrimp_catches_list_multiple_star_answers(self, cog, db, rows):
        shrimp = [[f"A{i}"] for i in range(1, 8)]
        shrimp[0] = ["A1a", "A1b"]  # slot 0 (Q1) has two star answers
        await db.set_puzzle_info(
            "krillion", 46, {"prompts": [f"Q{i}" for i in range(1, 8)], "shrimp": shrimp}
        )
        lines = await cog.compare_stats(rows, P1)
        catches_at = lines.index("### Shrimp catches 🌟")
        assert lines[catches_at + 1 :] == [
            "• Q1 → **A1a / A1b** (#46)",
            "• Q7 → **A7** (#46)",
        ]

    async def test_shrimp_catches_tolerate_legacy_single_string_form(
        self, cog, db, rows
    ):
        # Days archived by the earlier single-answer format still resolve.
        await db.set_puzzle_info(
            "krillion",
            46,
            {"prompts": [f"Q{i}" for i in range(1, 8)], "shrimp": [f"A{i}" for i in range(1, 8)]},
        )
        lines = await cog.compare_stats(rows, P1)
        catches_at = lines.index("### Shrimp catches 🌟")
        assert lines[catches_at + 1 :] == ["• Q1 → **A1** (#46)", "• Q7 → **A7** (#46)"]

    async def test_shrimp_catches_without_answer_data_omit_the_arrow(self, cog, db, rows):
        # Prompts archived but no shrimp answers (older-format archive).
        await db.set_puzzle_info(
            "krillion", 46, {"prompts": [f"Q{i}" for i in range(1, 8)]}
        )
        lines = await cog.compare_stats(rows, P1)
        catches_at = lines.index("### Shrimp catches 🌟")
        assert lines[catches_at + 1 :] == ["• Q1 (#46)", "• Q7 (#46)"]

    async def test_no_shrimp_section_without_archived_prompts(self, cog, rows):
        lines = await cog.compare_stats(rows, P1)
        assert "### Shrimp catches 🌟" not in lines


class TestPromptArchive:
    @pytest.fixture
    def cog(self, db):
        return Krillion(SimpleNamespace(database=db))

    @staticmethod
    def _today(day=48):
        return {
            "date": "2026-09-01",
            "dayNumber": day,
            "prompts": [{"id": f"p{i}", "text": f"Prompt {i}"} for i in range(1, 8)],
        }

    @staticmethod
    def _reveal():
        return {
            "date": "2026-09-01",
            "prompts": [
                {
                    "text": f"Prompt {i}",
                    "answers": [
                        {"answer": f"Shrimp{i}", "score": 100},
                        {"answer": f"Squid{i}", "score": 60},
                    ],
                }
                for i in range(1, 8)
            ],
        }

    async def test_stores_prompts_and_shrimp_answers(self, cog, db):
        assert await cog._store_today(self._today(), self._reveal()) is True
        stored = await db.get_puzzle_info("krillion")
        assert stored[48]["date"] == "2026-09-01"
        assert stored[48]["prompts"] == [f"Prompt {i}" for i in range(1, 8)]
        # Each prompt's star answers are stored as a list (usually one).
        assert stored[48]["shrimp"] == [[f"Shrimp{i}"] for i in range(1, 8)]

    async def test_stores_too_smart_answers(self, cog, db):
        reveal = self._reveal()
        reveal["prompts"][1]["answers"].append({"answer": "Human", "score": 15})
        await cog._store_today(self._today(), reveal)
        stored = await db.get_puzzle_info("krillion")
        # Most prompts have no too smart answer, so most slots are empty.
        assert stored[48]["too_smart"] == [[], ["Human"], [], [], [], [], []]

    async def test_reveal_failure_stores_prompts_without_shrimp(self, cog, db):
        assert await cog._store_today(self._today(), None) is True
        stored = await db.get_puzzle_info("krillion")
        assert stored[48]["prompts"] == [f"Prompt {i}" for i in range(1, 8)]
        assert "shrimp" not in stored[48]
        assert "too_smart" not in stored[48]

    async def test_prompt_missing_shrimp_answer_stores_empty_list(self, cog, db):
        reveal = self._reveal()
        reveal["prompts"][2]["answers"] = [{"answer": "Squid3", "score": 60}]
        await cog._store_today(self._today(), reveal)
        stored = await db.get_puzzle_info("krillion")
        assert stored[48]["shrimp"][2] == []
        assert stored[48]["shrimp"][0] == ["Shrimp1"]

    def test_shrimp_from_reveal_extracts_one_per_prompt(self, cog):
        assert cog._answers_from_reveal(self._reveal()) == [
            [f"Shrimp{i}"] for i in range(1, 8)
        ]

    def test_shrimp_from_reveal_keeps_multiple_star_answers(self, cog):
        reveal = self._reveal()
        reveal["prompts"][0]["answers"] = [
            {"answer": "Star A", "score": 100},
            {"answer": "Star B", "score": 100},
            {"answer": "Squid", "score": 60},
        ]
        assert cog._answers_from_reveal(reveal)[0] == ["Star A", "Star B"]

    def test_answers_from_reveal_picks_too_smart_by_points(self, cog):
        reveal = self._reveal()
        reveal["prompts"][3]["answers"].append({"answer": "Clever", "score": 15})
        too_smart = cog._answers_from_reveal(reveal, 15)
        assert too_smart[3] == ["Clever"]
        assert too_smart[0] == []

    @pytest.mark.parametrize(
        "reveal",
        [None, {}, {"prompts": []}, {"prompts": [{"answers": []}] * 6}],
    )
    def test_shrimp_from_reveal_rejects_bad_shapes(self, cog, reveal):
        assert cog._answers_from_reveal(reveal) is None

    @pytest.mark.parametrize(
        "today",
        [
            None,
            {},
            {"dayNumber": "48", "prompts": [{"text": "x"}] * 7},  # day not an int
            {"dayNumber": 48, "prompts": [{"text": "x"}] * 6},  # wrong count
            {"dayNumber": 48, "prompts": [{"id": "p1"}] * 7},  # no texts
        ],
    )
    async def test_junk_today_payloads_are_rejected(self, cog, db, today):
        assert await cog._store_today(today, self._reveal()) is False
        assert await db.get_puzzle_info("krillion") == {}


class TestArchiveBackfill:
    @pytest.fixture
    def cog(self, db, monkeypatch):
        monkeypatch.setattr("cogs.krillion.ARCHIVE_DELAY", 0)
        monkeypatch.setenv("KRILLION_UNLOCK_TOKEN", "cs_live_x.sig")
        return Krillion(
            SimpleNamespace(database=db, logger=logging.getLogger("test"))
        )

    @staticmethod
    def _archive(day):
        """The archive's day and reveal payloads for ``day``."""
        meta = {
            "game": day,
            "date": f"2026-07-{day:02d}",
            "prompts": [{"id": f"p{i}", "text": f"D{day} Q{i}"} for i in range(1, 8)],
        }
        # Rounds come back keyed by prompt id, deliberately out of order.
        sheet = {
            "rounds": [
                {
                    "round": f"p{i}",
                    "text": f"D{day} Q{i}",
                    "answers": [
                        {"answer": f"Shrimp{i}", "score": 100},
                        {"answer": f"Clever{i}", "score": 15},
                    ],
                }
                for i in reversed(range(1, 8))
            ]
        }
        return meta, sheet

    @pytest.fixture
    def fetched(self, cog):
        """Serve fake archive responses; records each (url, headers) asked for."""
        calls = []
        fail_days: set[int] = set()

        async def fake_get_json(session, url, headers=None):
            calls.append((url, headers))
            day = int(url.split("/api/archive/")[1].split("/")[0])
            if day in fail_days:
                return None
            meta, sheet = self._archive(day)
            return sheet if url.endswith("/reveal") else meta

        cog._get_json = fake_get_json
        return SimpleNamespace(calls=calls, fail_days=fail_days)

    async def test_fills_missing_and_incomplete_days_only(self, cog, db, fetched):
        complete = {"prompts": ["x"] * 7, "shrimp": [[]] * 7, "too_smart": [[]] * 7}
        await db.set_puzzle_info("krillion", 1, complete)
        # Day 2 was stored without its answer sheet (reveal fetch failed).
        await db.set_puzzle_info("krillion", 2, {"prompts": ["x"] * 7})
        # Day 4 is "today": not in the archive yet, so never asked for.
        await cog._backfill_missing(None, 4)

        stored = await db.get_puzzle_info("krillion")
        assert stored[1] == complete
        assert sorted(stored) == [1, 2, 3]
        assert stored[3]["date"] == "2026-07-03"
        assert stored[3]["prompts"] == [f"D3 Q{i}" for i in range(1, 8)]
        # Rounds are matched back to prompts by id, not by position.
        assert stored[3]["shrimp"] == [[f"Shrimp{i}"] for i in range(1, 8)]
        assert stored[3]["too_smart"] == [[f"Clever{i}"] for i in range(1, 8)]
        assert stored[2]["shrimp"][0] == ["Shrimp1"]
        asked = [url for url, _ in fetched.calls]
        assert asked == [
            "https://krillion.io/api/archive/2",
            "https://krillion.io/api/archive/2/reveal",
            "https://krillion.io/api/archive/3",
            "https://krillion.io/api/archive/3/reveal",
        ]
        assert all(h == {"x-krillion-unlock": "cs_live_x.sig"} for _, h in fetched.calls)

    async def test_no_token_means_no_requests(self, cog, db, fetched, monkeypatch):
        monkeypatch.delenv("KRILLION_UNLOCK_TOKEN")
        await cog._backfill_missing(None, 4)
        assert fetched.calls == []
        assert await db.get_puzzle_info("krillion") == {}

    async def test_failure_stops_and_keeps_earlier_days(self, cog, db, fetched):
        fetched.fail_days.add(2)
        await cog._backfill_missing(None, 4)
        stored = await db.get_puzzle_info("krillion")
        # Day 1 filled; day 2 failed so the run stopped before day 3.
        assert sorted(stored) == [1]
        assert not any("/archive/3" in url for url, _ in fetched.calls)

    @pytest.mark.parametrize(
        "meta, sheet",
        [
            (None, None),
            ({"game": 5}, {"rounds": []}),  # no prompts
        ],
    )
    def test_from_archive_rejects_unusable_days(self, cog, meta, sheet):
        assert cog._from_archive(meta, sheet) == (None, None)

    def test_from_archive_without_matching_rounds_has_no_reveal(self, cog):
        meta, sheet = self._archive(5)
        sheet["rounds"] = sheet["rounds"][1:]  # one prompt's round missing
        today, reveal = cog._from_archive(meta, sheet)
        assert today["dayNumber"] == 5
        assert reveal is None


class TestBuilder:
    @pytest.fixture
    def channel(self):
        return SimpleNamespace(id=100, guild=None)

    async def test_empty_month_returns_none(self, db, channel):
        cog = Krillion(SimpleNamespace(database=db))
        assert await cog.build_leaderboard(channel, (2026, 8), "August 2026") is None

    async def test_ranking_counts_and_month_attribution(self, db, channel):
        cog = Krillion(SimpleNamespace(database=db))
        await db.upsert_leaderboard_result(
            "krillion", 100, 1, 42, "alice", date(2026, 8, 30),
            {"puzzle": 47, "score": 385, "answers": [100, 85, 60, 30, 10, 0, 100]},
        )
        await db.upsert_leaderboard_result(
            "krillion", 100, 2, 43, "bob", date(2026, 8, 30),
            {"puzzle": 47, "score": 220, "answers": [30, 10, 60, 0, 30, 30, 60]},
        )
        # A late catch-up post: day 10 really belongs to July, so the anchor
        # (majority vote from the day-47 posts) must exclude it from August.
        await db.upsert_leaderboard_result(
            "krillion", 100, 3, 42, "alice", date(2026, 8, 30),
            {"puzzle": 10, "score": 700, "answers": None},
        )

        embed = await cog.build_leaderboard(channel, (2026, 8), "August 2026")
        assert embed is not None
        first, second = embed.description.splitlines()
        assert "🥇 **alice** — 385 pts (1 day, avg 385) · 🌟2 🏮1 🦑1" == first
        assert "🥈 **bob** — 220 pts (1 day, avg 220) · 🌟0 🏮0 🦑2" == second
        assert "August 2026" in embed.footer.text

    async def test_question_averages_with_archived_prompts(self, db, channel):
        cog = Krillion(SimpleNamespace(database=db))
        await db.upsert_leaderboard_result(
            "krillion", 100, 1, 42, "alice", date(2026, 8, 30),
            {"puzzle": 47, "score": 385, "answers": [100, 85, 60, 30, 10, 0, 100]},
        )
        await db.upsert_leaderboard_result(
            "krillion", 100, 2, 43, "bob", date(2026, 8, 30),
            {"puzzle": 47, "score": 220, "answers": [30, 10, 60, 0, 30, 30, 60]},
        )
        await db.set_puzzle_info(
            "krillion",
            47,
            {
                "prompts": [f"Q{i}" for i in range(1, 8)],
                "shrimp": [[f"A{i}"] for i in range(1, 8)],
            },
        )
        embed = await cog.build_leaderboard(channel, (2026, 8), "August 2026")
        fields = {field.name: field.value for field in embed.fields}

        # Slot averages: 65, 47.5, 60, 15, 20, 15, 80. Shrimps landed only on
        # slots 0 (Q1, alice) and 6 (Q7, alice), so only those lines name the
        # shrimp answer — questions nobody here shrimped stay unnamed.
        best = fields["💪 Best questions (group average)"].splitlines()
        assert best == [
            "• Q7 — avg 80 pts (#47) — 🌟 A7",
            "• Q1 — avg 65 pts (#47) — 🌟 A1",
            "• Q3 — avg 60 pts (#47)",
        ]
        worst = fields["😰 Worst questions (group average)"].splitlines()
        assert worst == [
            "• Q4 — avg 15 pts (#47)",
            "• Q6 — avg 15 pts (#47)",
            "• Q5 — avg 20 pts (#47)",
        ]

    async def test_question_field_lists_multiple_star_answers(self, db, channel):
        cog = Krillion(SimpleNamespace(database=db))
        # Both players shrimp slot 6 (Q7); it has two star answers.
        await db.upsert_leaderboard_result(
            "krillion", 100, 1, 42, "alice", date(2026, 8, 30),
            {"puzzle": 47, "score": 400, "answers": [30, 30, 30, 30, 30, 30, 100]},
        )
        await db.upsert_leaderboard_result(
            "krillion", 100, 2, 43, "bob", date(2026, 8, 30),
            {"puzzle": 47, "score": 400, "answers": [30, 30, 30, 30, 30, 30, 100]},
        )
        shrimp = [[f"A{i}"] for i in range(1, 8)]
        shrimp[6] = ["A7a", "A7b"]
        await db.set_puzzle_info(
            "krillion", 47, {"prompts": [f"Q{i}" for i in range(1, 8)], "shrimp": shrimp}
        )
        embed = await cog.build_leaderboard(channel, (2026, 8), "August 2026")
        text = "\n".join(f.value for f in embed.fields)
        assert "• Q7 — avg 100 pts (#47) — 🌟 A7a / A7b" in text

    async def test_shrimp_answer_hidden_when_nobody_here_got_it(self, db, channel):
        cog = Krillion(SimpleNamespace(database=db))
        # Two players, neither shrimping slot 0; only slot 6 gets a shrimp.
        await db.upsert_leaderboard_result(
            "krillion", 100, 1, 42, "alice", date(2026, 8, 30),
            {"puzzle": 47, "score": 400, "answers": [85, 85, 60, 60, 30, 30, 100]},
        )
        await db.upsert_leaderboard_result(
            "krillion", 100, 2, 43, "bob", date(2026, 8, 30),
            {"puzzle": 47, "score": 340, "answers": [85, 60, 60, 30, 30, 30, 60]},
        )
        await db.set_puzzle_info(
            "krillion",
            47,
            {"prompts": [f"Q{i}" for i in range(1, 8)], "shrimp": [[f"A{i}"] for i in range(1, 8)]},
        )
        embed = await cog.build_leaderboard(channel, (2026, 8), "August 2026")
        text = "\n".join(f.value for f in embed.fields)
        # Q7 was shrimped → its answer shows; Q1 (highest avg after Q7) wasn't.
        assert "• Q7 — avg 80 pts (#47) — 🌟 A7" in text
        assert "• Q1 — avg 85 pts (#47)" in text
        assert "🌟 A1" not in text

    async def test_worst_questions_name_too_smart_answer_someone_got(
        self, db, channel
    ):
        cog = Krillion(SimpleNamespace(database=db))
        # alice gets the too smart answer on Q2; nobody gets one on Q5.
        await db.upsert_leaderboard_result(
            "krillion", 100, 1, 42, "alice", date(2026, 9, 4),
            {"puzzle": 51, "score": 400, "answers": [60, 15, 60, 60, 60, 60, 85]},
        )
        await db.upsert_leaderboard_result(
            "krillion", 100, 2, 43, "bob", date(2026, 9, 4),
            {"puzzle": 51, "score": 285, "answers": [60, 10, 60, 60, 10, 0, 85]},
        )
        too_smart = [[] for _ in range(7)]
        too_smart[1] = ["Human"]
        too_smart[4] = ["Unclaimed"]
        await db.set_puzzle_info(
            "krillion",
            51,
            {
                "prompts": [f"Q{i}" for i in range(1, 8)],
                "shrimp": [[f"A{i}"] for i in range(1, 8)],
                "too_smart": too_smart,
            },
        )
        embed = await cog.build_leaderboard(channel, (2026, 9), "September 2026")
        fields = {field.name: field.value for field in embed.fields}
        # Slot averages: 60, 12.5, 60, 60, 35, 30, 85.
        worst = fields["😰 Worst questions (group average)"].splitlines()
        assert worst == [
            "• Q2 — avg 12 pts (#51) — 🤡 Human",
            "• Q6 — avg 30 pts (#51)",
            "• Q5 — avg 35 pts (#51)",
        ]
        assert "Unclaimed" not in "\n".join(fields.values())

    async def test_best_questions_omit_too_smart_answer(self, db, channel):
        cog = Krillion(SimpleNamespace(database=db))
        # Everyone's too smart answer on Q1 is still the best average here.
        for mid, uid in enumerate((42, 43), start=1):
            await db.upsert_leaderboard_result(
                "krillion", 100, mid, uid, f"p{uid}", date(2026, 9, 4),
                {"puzzle": 51, "score": 15, "answers": [15, 0, 0, 0, 0, 0, 0]},
            )
        too_smart = [[] for _ in range(7)]
        too_smart[0] = ["Human"]
        await db.set_puzzle_info(
            "krillion",
            51,
            {"prompts": [f"Q{i}" for i in range(1, 8)], "too_smart": too_smart},
        )
        embed = await cog.build_leaderboard(channel, (2026, 9), "September 2026")
        fields = {field.name: field.value for field in embed.fields}
        assert fields["💪 Best questions (group average)"].splitlines()[0] == (
            "• Q1 — avg 15 pts (#51)"
        )

    async def test_no_question_fields_without_two_grids(self, db, channel):
        cog = Krillion(SimpleNamespace(database=db))
        await db.upsert_leaderboard_result(
            "krillion", 100, 1, 42, "alice", date(2026, 8, 30),
            {"puzzle": 47, "score": 385, "answers": [100, 85, 60, 30, 10, 0, 100]},
        )
        # Second player has no grid, just a bare score.
        await db.upsert_leaderboard_result(
            "krillion", 100, 2, 43, "bob", date(2026, 8, 30),
            {"puzzle": 47, "score": 220, "answers": None},
        )
        await db.set_puzzle_info(
            "krillion", 47, {"prompts": [f"Q{i}" for i in range(1, 8)]}
        )
        embed = await cog.build_leaderboard(channel, (2026, 8), "August 2026")
        assert embed.fields == []

    async def test_no_question_fields_without_archived_prompts(self, db, channel):
        cog = Krillion(SimpleNamespace(database=db))
        for mid, (uid, name, answers) in enumerate(
            [
                (42, "alice", [100, 85, 60, 30, 10, 0, 100]),
                (43, "bob", [30, 10, 60, 0, 30, 30, 60]),
            ],
            start=1,
        ):
            await db.upsert_leaderboard_result(
                "krillion", 100, mid, uid, name, date(2026, 8, 30),
                {"puzzle": 47, "score": sum(answers), "answers": answers},
            )
        embed = await cog.build_leaderboard(channel, (2026, 8), "August 2026")
        assert embed.fields == []

    async def test_same_day_reposts_keep_best_score(self, db, channel):
        cog = Krillion(SimpleNamespace(database=db))
        await db.upsert_leaderboard_result(
            "krillion", 100, 1, 42, "alice", date(2026, 8, 30),
            {"puzzle": 47, "score": 220, "answers": [30, 10, 60, 0, 30, 30, 60]},
        )
        await db.upsert_leaderboard_result(
            "krillion", 100, 2, 42, "alice", date(2026, 8, 30),
            {"puzzle": 47, "score": 260, "answers": [60, 60, 10, 60, 30, 10, 30]},
        )
        embed = await cog.build_leaderboard(channel, (2026, 8), "August 2026")
        assert "260 pts (1 day" in embed.description
        # The counts come from the kept (best) share's grid: three squids.
        assert "🌟0 🏮0 🦑3" in embed.description
