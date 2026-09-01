"""Tests for DatabaseManager against a real in-memory SQLite database."""

from datetime import date


class TestLeaderboardResults:
    async def test_upsert_and_read_back(self, db):
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5), {"total": 951.34}
        )
        rows = await db.get_leaderboard_results(
            "gauntle", 100, "2026-08-01", "2026-09-01"
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["author_id"] == 42
        assert row["author_name"] == "alice"
        assert row["played_on"] == date(2026, 8, 5)
        assert row["payload"] == {"total": 951.34}

    async def test_nested_payload_round_trips(self, db):
        payload = {"total": 1.5, "categories": {"Sudoku": {"raw": 52.24, "adj": -10}}}
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5), payload
        )
        rows = await db.get_leaderboard_results(
            "gauntle", 100, "2026-08-01", "2026-09-01"
        )
        assert rows[0]["payload"] == payload

    async def test_upsert_same_message_updates_instead_of_duplicating(self, db):
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5), {"total": 100}
        )
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice-renamed", date(2026, 8, 6), {"total": 90}
        )
        rows = await db.get_leaderboard_results(
            "gauntle", 100, "2026-08-01", "2026-09-01"
        )
        assert len(rows) == 1
        assert rows[0]["author_name"] == "alice-renamed"
        assert rows[0]["played_on"] == date(2026, 8, 6)
        assert rows[0]["payload"] == {"total": 90}

    async def test_games_are_namespaced(self, db):
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5), {"total": 1}
        )
        await db.upsert_leaderboard_result(
            "foodguessr", 100, 1, 42, "alice", date(2026, 8, 5), {"score": 2}
        )
        gauntle = await db.get_leaderboard_results(
            "gauntle", 100, "2026-08-01", "2026-09-01"
        )
        food = await db.get_leaderboard_results(
            "foodguessr", 100, "2026-08-01", "2026-09-01"
        )
        assert gauntle[0]["payload"] == {"total": 1}
        assert food[0]["payload"] == {"score": 2}

    async def test_window_is_half_open(self, db):
        for i, day in enumerate([date(2026, 7, 31), date(2026, 8, 1), date(2026, 9, 1)]):
            await db.upsert_leaderboard_result(
                "gauntle", 100, i, 42, "alice", day, {"n": i}
            )
        rows = await db.get_leaderboard_results(
            "gauntle", 100, "2026-08-01", "2026-09-01"
        )
        assert [row["played_on"] for row in rows] == [date(2026, 8, 1)]

    async def test_other_channels_excluded(self, db):
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5), {"n": 1}
        )
        await db.upsert_leaderboard_result(
            "gauntle", 200, 2, 42, "alice", date(2026, 8, 5), {"n": 2}
        )
        rows = await db.get_leaderboard_results(
            "gauntle", 100, "2026-08-01", "2026-09-01"
        )
        assert len(rows) == 1
        assert rows[0]["payload"] == {"n": 1}

    async def test_results_ordered_by_date(self, db):
        await db.upsert_leaderboard_result(
            "gauntle", 100, 2, 42, "alice", date(2026, 8, 20), {"n": 2}
        )
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5), {"n": 1}
        )
        rows = await db.get_leaderboard_results(
            "gauntle", 100, "2026-08-01", "2026-09-01"
        )
        assert [row["payload"]["n"] for row in rows] == [1, 2]

    async def test_delete_removes_only_that_games_row(self, db):
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5), {"n": 1}
        )
        await db.upsert_leaderboard_result(
            "foodguessr", 100, 1, 42, "alice", date(2026, 8, 5), {"n": 2}
        )
        await db.delete_leaderboard_result("gauntle", 1)
        assert (
            await db.get_leaderboard_results("gauntle", 100, "2026-08-01", "2026-09-01")
            == []
        )
        assert (
            len(
                await db.get_leaderboard_results(
                    "foodguessr", 100, "2026-08-01", "2026-09-01"
                )
            )
            == 1
        )


class TestClearGame:
    async def test_clears_results_and_scan_state_for_one_game(self, db):
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5), {"n": 1}
        )
        await db.upsert_leaderboard_result(
            "gauntle", 100, 2, 42, "alice", date(2026, 8, 6), {"n": 2}
        )
        await db.upsert_leaderboard_result(
            "foodguessr", 100, 3, 42, "alice", date(2026, 8, 5), {"n": 3}
        )
        await db.set_leaderboard_scan("gauntle", 100, 2, "2026-08-01")
        await db.set_leaderboard_scan("foodguessr", 100, 3, "2026-08-01")

        removed = await db.clear_leaderboard_game("gauntle")
        assert removed == 2
        assert (
            await db.get_leaderboard_results("gauntle", 100, "0000-01-01", "9999-12-31")
            == []
        )
        assert await db.get_leaderboard_scan("gauntle", 100) == (None, None)
        # The other game is untouched.
        assert len(
            await db.get_leaderboard_results(
                "foodguessr", 100, "0000-01-01", "9999-12-31"
            )
        ) == 1
        assert await db.get_leaderboard_scan("foodguessr", 100) == (3, "2026-08-01")


class TestPuzzleInfo:
    async def test_round_trip_and_overwrite(self, db):
        await db.set_puzzle_info("krillion", 48, {"date": "2026-09-01", "prompts": ["a"]})
        assert await db.get_puzzle_info("krillion") == {
            48: {"date": "2026-09-01", "prompts": ["a"]}
        }
        await db.set_puzzle_info("krillion", 48, {"prompts": ["b"]})
        assert (await db.get_puzzle_info("krillion"))[48] == {"prompts": ["b"]}

    async def test_namespaced_per_game(self, db):
        await db.set_puzzle_info("krillion", 48, {"prompts": ["a"]})
        assert await db.get_puzzle_info("catfishing") == {}


class TestScanState:
    async def test_unscanned_channel_returns_none_pair(self, db):
        assert await db.get_leaderboard_scan("gauntle", 100) == (None, None)

    async def test_set_and_get(self, db):
        await db.set_leaderboard_scan("gauntle", 100, 12345, "2026-08-01T00:00:00+00:00")
        assert await db.get_leaderboard_scan("gauntle", 100) == (
            12345,
            "2026-08-01T00:00:00+00:00",
        )

    async def test_update_overwrites(self, db):
        await db.set_leaderboard_scan("gauntle", 100, 1, "2026-08-01")
        await db.set_leaderboard_scan("gauntle", 100, 2, "2026-07-01")
        assert await db.get_leaderboard_scan("gauntle", 100) == (2, "2026-07-01")

    async def test_scan_state_is_per_game_and_channel(self, db):
        await db.set_leaderboard_scan("gauntle", 100, 1, "a")
        assert await db.get_leaderboard_scan("gauntle", 200) == (None, None)
        assert await db.get_leaderboard_scan("foodguessr", 100) == (None, None)


class TestAutopost:
    async def test_defaults_are_empty(self, db):
        assert await db.get_autopost_channels("gauntle") == []
        assert await db.get_autopost_games(100) == []

    async def test_opt_in_and_out(self, db):
        await db.set_autopost("gauntle", 100, True)
        await db.set_autopost("gauntle", 200, True)
        await db.set_autopost("foodguessr", 100, True)
        assert sorted(await db.get_autopost_channels("gauntle")) == [100, 200]
        assert sorted(await db.get_autopost_games(100)) == ["foodguessr", "gauntle"]

        await db.set_autopost("gauntle", 100, False)
        assert await db.get_autopost_channels("gauntle") == [200]
        assert await db.get_autopost_games(100) == ["foodguessr"]

    async def test_double_opt_in_is_idempotent(self, db):
        await db.set_autopost("gauntle", 100, True)
        await db.set_autopost("gauntle", 100, True)
        assert await db.get_autopost_channels("gauntle") == [100]

    async def test_opt_out_when_not_opted_in_is_a_noop(self, db):
        await db.set_autopost("gauntle", 100, False)
        assert await db.get_autopost_channels("gauntle") == []


class TestIntroChannels:
    async def test_unset_returns_none(self, db):
        assert await db.get_intro_channel(1) is None

    async def test_set_get_and_overwrite(self, db):
        await db.set_intro_channel(1, 555)
        assert await db.get_intro_channel(1) == 555
        await db.set_intro_channel(1, 777)
        assert await db.get_intro_channel(1) == 777

    async def test_siloed_per_server(self, db):
        await db.set_intro_channel(1, 555)
        assert await db.get_intro_channel(2) is None


class TestInviteAliases:
    async def test_set_get_and_update(self, db):
        await db.set_invite_alias(1, "batty", "id:42")
        await db.set_invite_alias(1, "gem", "name:gemma")
        assert await db.get_invite_aliases(1) == {"batty": "id:42", "gem": "name:gemma"}
        await db.set_invite_alias(1, "batty", "id:43")
        assert (await db.get_invite_aliases(1))["batty"] == "id:43"

    async def test_siloed_per_server(self, db):
        await db.set_invite_alias(1, "batty", "id:42")
        assert await db.get_invite_aliases(2) == {}


class TestInviteOverrides:
    async def test_set_get_none_and_delete(self, db):
        await db.set_invite_override(1, 42, "id:7")
        await db.set_invite_override(1, 43, None)  # explicitly nobody
        assert await db.get_invite_overrides(1) == {42: "id:7", 43: None}

        await db.delete_invite_override(1, 42)
        assert await db.get_invite_overrides(1) == {43: None}

    async def test_overwrite(self, db):
        await db.set_invite_override(1, 42, "id:7")
        await db.set_invite_override(1, 42, "name:gem")
        assert await db.get_invite_overrides(1) == {42: "name:gem"}

    async def test_siloed_per_server(self, db):
        await db.set_invite_override(1, 42, "id:7")
        assert await db.get_invite_overrides(2) == {}
