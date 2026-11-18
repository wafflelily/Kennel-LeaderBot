"""Tests for Krillion friends' answers: links, snapshots, narrowing, daily post."""

import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from cogs import krillion as krillion_module
from cogs.krillion import RESET_TZ, Krillion, _narrow

PROMPTS = [f"Q{i}" for i in range(1, 8)]


def rounds(answers, scores):
    return [
        {"prompt": q, "answer": a, "score": s}
        for q, a, s in zip(PROMPTS, answers, scores, strict=True)
    ]


def friend(username, answers, scores, **extra):
    return {
        "username": username,
        "score": sum(scores),
        "answers": [
            {**r, "tier": "x"} for r in rounds(answers, scores)
        ],
        "rank": 1,
        **extra,
    }


def row(author_id, payload):
    return {
        "author_id": author_id,
        "author_name": f"player{author_id}",
        "played_on": date(2026, 8, 30),
        "payload": payload,
    }


class TestLinks:
    async def test_link_is_unique_per_username(self, db):
        assert await db.set_krillion_link(1, "ShrimpFan")
        assert not await db.set_krillion_link(2, "shrimpfan")
        # Relinking your own name, or switching names, is fine.
        assert await db.set_krillion_link(1, "shrimpfan")
        assert await db.set_krillion_link(1, "other")
        assert await db.get_krillion_links() == {1: "other"}

    async def test_unlink_keeps_stored_answers(self, db):
        await db.set_krillion_link(1, "fan")
        await db.set_krillion_friend_answers(47, "Fan", {"username": "Fan"})
        assert await db.delete_krillion_link(1) == "fan"
        assert await db.get_krillion_links() == {}
        assert await db.get_krillion_friend_answers() == {47: {"fan": {"username": "Fan"}}}
        assert await db.delete_krillion_link(1) is None


class TestNarrow:
    def test_nothing_given_keeps_all(self):
        assert _narrow(["A", "B"], []) == ["A", "B"]

    def test_matches_use_the_archived_spelling(self):
        assert _narrow(["Lion", "Tiger"], ["lion "]) == ["Lion"]

    def test_unmatched_answer_is_kept_as_given(self):
        assert _narrow(["Lion", "Tiger"], ["lions", "Lion"]) == ["Lion", "lions"]

    def test_no_sheet_uses_given(self):
        assert _narrow([], ["Lion"]) == ["Lion"]


class TestFriendDay:
    def test_reshapes_shared_answers(self):
        entry = friend("Fan", ["a"] * 7, [100, 0, 30, 60, 85, 10, 15])
        entry["answers"][1]["score"] = None  # no submission
        username, payload = Krillion._friend_day(entry)
        assert username == "Fan"
        assert payload["score"] == 300
        assert payload["answers"][0] == {"prompt": "Q1", "answer": "a", "score": 100}
        assert payload["answers"][1]["score"] == 0

    def test_private_friend_is_stored_with_just_a_score(self):
        entry = {"username": "Shy", "score": 245, "answers": None, "rank": 2}
        assert Krillion._friend_day(entry) == (
            "Shy", {"username": "Shy", "score": 245, "answers": None}
        )

    @pytest.mark.parametrize(
        "entry",
        [
            {"username": "x", "score": None, "answers": None},  # not played yet
            {"username": "x", "answers": [{}] * 6},  # wrong round count
            {"answers": [{}] * 7},  # no username
            "junk",
        ],
    )
    def test_rejects_entries_without_answers(self, entry):
        assert Krillion._friend_day(entry) is None


class TestSnapshot:
    @pytest.fixture
    def cog(self, db, monkeypatch):
        monkeypatch.setenv("KRILLION_REFRESH_TOKEN", "refresh")
        monkeypatch.setenv("KRILLION_FIREBASE_API_KEY", "key")
        cog = Krillion(SimpleNamespace(database=db, logger=logging.getLogger("test")))

        async def fake_token(session, refresh_token, api_key):
            assert (refresh_token, api_key) == ("refresh", "key")
            return "id-token"

        cog._friends_id_token = fake_token
        return cog

    def board(self, **overrides):
        return {
            "profile": {"username": "Owner", "shareAnswers": True},
            "date": "2026-08-30",
            "unlocked": True,
            "leaderboard": [
                friend("Owner", ["o"] * 7, [30] * 7, self=True),
                friend("Fan", ["f"] * 7, [100] * 7),
                {"username": "Late", "score": None, "answers": None, "rank": 3},
                {"username": "Shy", "score": 300, "answers": None, "rank": 2},
            ],
            **overrides,
        }

    def serve(self, cog, board):
        calls = []

        async def fake_get_json(session, url, headers=None):
            calls.append((url, headers))
            return board

        cog._get_json = fake_get_json
        return calls

    async def test_stores_shared_answers_under_the_day(self, cog, db):
        await db.set_puzzle_info("krillion", 47, {"date": "2026-08-30", "prompts": PROMPTS})
        calls = self.serve(cog, self.board())
        await cog._snapshot_friends()
        assert calls == [(krillion_module.FRIENDS_URL, {"Authorization": "Bearer id-token"})]
        stored = await db.get_krillion_friend_answers()
        # Everyone who played, private or not; not the friend yet to play.
        assert sorted(stored[47]) == ["fan", "owner", "shy"]
        assert stored[47]["shy"] == {"username": "Shy", "score": 300, "answers": None}
        assert stored[47]["fan"]["answers"][0] == {"prompt": "Q1", "answer": "f", "score": 100}
        assert cog._owner_username == "Owner"
        assert cog._friend_usernames == {"owner", "fan", "late", "shy"}

    async def test_locked_board_stores_nothing(self, cog, db):
        await db.set_puzzle_info("krillion", 47, {"date": "2026-08-30", "prompts": PROMPTS})
        self.serve(cog, self.board(unlocked=False))
        await cog._snapshot_friends()
        assert await db.get_krillion_friend_answers() == {}

    async def test_unarchived_day_stores_nothing(self, cog, db):
        self.serve(cog, self.board())

        async def no_archive():
            pass

        cog._archive_today = no_archive
        await cog._snapshot_friends()
        assert await db.get_krillion_friend_answers() == {}

    async def test_failed_fetch_drops_the_cached_token(self, cog, db):
        self.serve(cog, None)
        cog._id_token = ("old", datetime.max.replace(tzinfo=RESET_TZ))
        await cog._snapshot_friends()
        assert cog._id_token is None

    @pytest.mark.parametrize(
        "missing", ["KRILLION_REFRESH_TOKEN", "KRILLION_FIREBASE_API_KEY"]
    )
    async def test_missing_config_means_no_requests(self, cog, monkeypatch, missing):
        monkeypatch.delenv(missing)
        calls = self.serve(cog, self.board())
        await cog._snapshot_friends()
        assert calls == []


class TestNarrowedCatches:
    @pytest.fixture
    def cog(self, db):
        return Krillion(SimpleNamespace(database=db))

    @pytest.fixture
    async def day(self, db):
        shrimp = [[f"A{i}"] for i in range(1, 8)]
        shrimp[6] = ["A7a", "A7b"]
        await db.set_puzzle_info("krillion", 47, {"prompts": PROMPTS, "shrimp": shrimp})

    async def test_mystats_names_a_linked_players_own_answer(self, cog, db, day):
        await db.set_krillion_link(1, "fan")
        await db.set_krillion_friend_answers(
            47, "fan", {"answers": rounds(["x"] * 6 + ["a7b"], [30] * 6 + [100])}
        )
        rows = [row(1, {"puzzle": 47, "score": 280, "answers": [30] * 6 + [100]})]
        lines = await cog.compare_stats(rows, 1)
        catches_at = lines.index("### Shrimp catches 🌟")
        assert lines[catches_at + 1] == ["• Q7 → **A7b** (#47)"]

    async def test_mystats_ignores_unlinked_answers(self, cog, db, day):
        await db.set_krillion_friend_answers(
            47, "fan", {"answers": rounds(["x"] * 6 + ["A7b"], [30] * 6 + [100])}
        )
        rows = [row(1, {"puzzle": 47, "score": 280, "answers": [30] * 6 + [100]})]
        lines = await cog.compare_stats(rows, 1)
        catches_at = lines.index("### Shrimp catches 🌟")
        assert lines[catches_at + 1] == ["• Q7 → **A7a / A7b** (#47)"]

    async def test_mismatched_prompt_text_is_not_trusted(self, cog, db, day):
        await db.set_krillion_link(1, "fan")
        played = rounds(["x"] * 6 + ["A7b"], [30] * 6 + [100])
        played[6]["prompt"] = "Something else"
        await db.set_krillion_friend_answers(47, "fan", {"answers": played})
        rows = [row(1, {"puzzle": 47, "score": 280, "answers": [30] * 6 + [100]})]
        lines = await cog.compare_stats(rows, 1)
        catches_at = lines.index("### Shrimp catches 🌟")
        assert lines[catches_at + 1] == ["• Q7 → **A7a / A7b** (#47)"]

    async def test_leaderboard_narrows_to_answers_given_here(self, cog, db, day):
        for message_id, author in ((1, 42), (2, 43)):
            await db.upsert_leaderboard_result(
                "krillion", 100, message_id, author, f"p{author}", date(2026, 8, 30),
                {"puzzle": 47, "score": 280, "answers": [30] * 6 + [100]},
            )
        # Only 42 is linked; 43's (unlinked) answer mustn't count.
        await db.set_krillion_link(42, "fan")
        await db.set_krillion_friend_answers(
            47, "fan", {"answers": rounds(["x"] * 6 + ["A7b"], [30] * 6 + [100])}
        )
        await db.set_krillion_friend_answers(
            47, "stranger", {"answers": rounds(["x"] * 6 + ["A7a"], [30] * 6 + [100])}
        )
        embed = await cog.build_leaderboard(
            SimpleNamespace(id=100, guild=None), (2026, 8), "August 2026"
        )
        text = "\n".join(f.value for f in embed.fields)
        assert "• Q7 — avg 100 pts (#47) — 🌟 A7b" in text


class TestDailyPost:
    CHANNEL = 100

    @pytest.fixture
    def guild(self):
        members = {
            42: SimpleNamespace(display_name="Alice"),
            43: SimpleNamespace(display_name="Bob"),
            44: SimpleNamespace(display_name="Cat"),
        }
        return SimpleNamespace(get_member=members.get)

    @pytest.fixture
    def channel(self, guild):
        return SimpleNamespace(id=self.CHANNEL, guild=guild)

    @pytest.fixture
    def cog(self, db, monkeypatch):
        self.rendered = []

        def fake_render(day, played_on, prompts, columns):
            self.rendered.append((day, prompts, columns))
            return b"png"

        monkeypatch.setattr(krillion_module, "render_day_table", fake_render)
        return Krillion(
            SimpleNamespace(database=db, intents=SimpleNamespace(members=True))
        )

    @pytest.fixture(autouse=True)
    async def yesterday(self, db):
        yesterday = (datetime.now(RESET_TZ) - timedelta(days=1)).date().isoformat()
        await db.set_puzzle_info("krillion", 47, {"date": yesterday, "prompts": PROMPTS})

    async def player(self, db, uid, name, answers, scores, *, linked=True,
                     shared=True, channel=CHANNEL, day=47):
        """A friend's stored answers, optionally linked and shared in a channel."""
        if linked:
            await db.set_krillion_link(uid, name)
        await db.set_krillion_friend_answers(
            day, name, {"username": name, "score": sum(scores),
                        "answers": rounds(answers, scores)},
        )
        if shared:
            await db.upsert_leaderboard_result(
                "krillion", channel, uid * 1000 + day, uid, name, date(2026, 8, 30),
                {"puzzle": day, "score": sum(scores), "answers": scores},
            )

    async def test_posts_linked_members_answers(self, cog, db, channel):
        await self.player(db, 42, "fan", ["Lion", None, "b", "c", "d", "e", "f"],
                          [100, 0, 30, 60, 10, 15, 30])
        await self.player(db, 43, "bob", ["g"] * 7, [10] * 7)
        await self.player(db, 99, "gone", ["g"] * 7, [10] * 7)  # not in this server
        embed, file = await cog.build_daily_post(channel)
        assert embed.title == "🦐 Krillion #47 — the catch"
        assert embed.description == "**Alice** 245 · **Bob** 70"
        assert embed.image.url == "attachment://krillion-47.png"
        assert file.filename == "krillion-47.png"
        assert file.description == "Krillion #47 answers: Alice 245 pts; Bob 70 pts"
        [(day, prompts, columns)] = self.rendered
        assert (day, prompts) == (47, PROMPTS)
        assert [c["name"] for c in columns] == ["Alice", "Bob"]
        assert columns[0]["rounds"][0] == {"prompt": "Q1", "answer": "Lion", "score": 100}
        assert columns[0]["rounds"][1]["answer"] is None

    async def test_columns_are_ordered_by_total(self, cog, db, channel):
        await self.player(db, 42, "alice", ["a"] * 7, [10] * 7)
        await self.player(db, 43, "bob", ["a"] * 7, [60] * 7)
        embed, _ = await cog.build_daily_post(channel)
        assert embed.description == "**Bob** 420 · **Alice** 70"

    async def test_needs_two_linked_players_who_shared_here(self, cog, db, channel):
        await self.player(db, 42, "alice", ["a"] * 7, [10] * 7)
        assert await cog.build_daily_post(channel) is None
        await self.player(db, 43, "bob", ["a"] * 7, [60] * 7)
        assert await cog.build_daily_post(channel) is not None

    async def test_private_players_dont_count_or_show(self, cog, db, channel):
        await self.player(db, 42, "alice", ["a"] * 7, [10] * 7)
        await self.player(db, 43, "bob", ["a"] * 7, [60] * 7)
        await db.set_krillion_link(44, "cat")
        await db.set_krillion_friend_answers(
            47, "cat", {"username": "cat", "score": 700, "answers": None}
        )
        await db.upsert_leaderboard_result(
            "krillion", self.CHANNEL, 3, 44, "cat", date(2026, 8, 30),
            {"puzzle": 47, "score": 700, "answers": None},
        )
        embed, _ = await cog.build_daily_post(channel)
        assert embed.description == "**Bob** 420 · **Alice** 70"
        # And on their own, a private player doesn't make up the pair.
        await db.delete_krillion_link(43)
        assert await cog.build_daily_post(channel) is None

    async def test_relinking_brings_history_back(self, cog, db, channel):
        await self.player(db, 42, "alice", ["a"] * 7, [10] * 7)
        await self.player(db, 43, "bob", ["a"] * 7, [60] * 7)
        await db.delete_krillion_link(43)
        assert await cog.build_daily_post(channel) is None
        await db.set_krillion_link(43, "bob")
        assert await cog.build_daily_post(channel) is not None

    async def test_unlinked_players_dont_count(self, cog, db, channel):
        await self.player(db, 42, "alice", ["a"] * 7, [10] * 7)
        await self.player(db, 43, "bob", ["a"] * 7, [60] * 7, linked=False)
        assert await cog.build_daily_post(channel) is None

    async def test_players_who_didnt_share_here_dont_count(self, cog, db, channel):
        await self.player(db, 42, "alice", ["a"] * 7, [10] * 7)
        await self.player(db, 43, "bob", ["a"] * 7, [60] * 7, shared=False)
        # Shared in another channel, or a different day here: neither counts.
        await self.player(db, 44, "cat", ["a"] * 7, [30] * 7, channel=555)
        await db.upsert_leaderboard_result(
            "krillion", self.CHANNEL, 1, 43, "bob", date(2026, 8, 30),
            {"puzzle": 46, "score": 70, "answers": None},
        )
        assert await cog.build_daily_post(channel) is None

    async def test_sharers_without_stored_answers_dont_count(self, cog, db, channel):
        await self.player(db, 42, "alice", ["a"] * 7, [10] * 7)
        await db.set_krillion_link(43, "bob")
        await db.upsert_leaderboard_result(
            "krillion", self.CHANNEL, 2, 43, "bob", date(2026, 8, 30),
            {"puzzle": 47, "score": 420, "answers": None},
        )
        assert await cog.build_daily_post(channel) is None

    async def test_nothing_to_post_when_yesterday_isnt_archived(self, cog, db, channel):
        await db.set_puzzle_info("krillion", 47, {"date": "2000-01-01", "prompts": PROMPTS})
        await self.player(db, 42, "alice", ["a"] * 7, [10] * 7)
        await self.player(db, 43, "bob", ["a"] * 7, [60] * 7)
        assert await cog.build_daily_post(channel) is None


class TestTableImage:
    def test_renders_a_png(self):
        from leaderboard.krillion_table import render_day_table

        columns = [
            {"name": "Alice", "score": 245,
             "rounds": [{"answer": "Lion", "score": 100}, None]
             + [{"answer": "a long answer " * 6, "score": s} for s in (15, 10, 30, 60, 85)]},
        ]
        png = render_day_table(47, "2026-08-30", PROMPTS, columns)
        assert png.startswith(b"\x89PNG")


class TestRenames:
    @pytest.fixture
    def cog(self, db):
        cog = Krillion(SimpleNamespace(database=db))
        cog._friend_usernames = {"newname", "someone"}
        return cog

    async def days(self, db, username, puzzles):
        for puzzle in puzzles:
            await db.set_krillion_friend_answers(
                puzzle, username, {"username": username, "score": puzzle, "answers": None}
            )

    async def test_database_rename_keeps_the_new_names_days(self, db):
        await self.days(db, "old", [1, 2])
        await self.days(db, "new", [2, 3])
        assert await db.rename_krillion_friend("Old", "New") == 1
        stored = await db.get_krillion_friend_answers()
        assert sorted(p for p, d in stored.items() if "new" in d) == [1, 2, 3]
        assert stored[2]["new"]["score"] == 2  # the new name's own row stood
        assert stored[2]["old"]["score"] == 2  # the clashing old row is left

    async def test_moves_history_after_a_rename(self, cog, db):
        await self.days(db, "oldname", [1, 2])
        await self.days(db, "newname", [3])
        assert await cog._carry_history("oldname", "NewName") == 2
        stored = await db.get_krillion_friend_answers()
        assert all(list(day) == ["newname"] for day in stored.values())

    async def test_no_move_while_the_old_name_is_still_a_friend(self, cog, db):
        await self.days(db, "someone", [1, 2])
        assert await cog._carry_history("someone", "newname") == 0

    async def test_no_move_when_both_names_played_the_same_day(self, cog, db):
        await self.days(db, "oldname", [1, 2])
        await self.days(db, "newname", [2])
        assert await cog._carry_history("oldname", "newname") == 0

    async def test_fetches_the_friends_list_if_unknown(self, cog, db):
        cog._friend_usernames = set()
        fetched = []

        async def fake_snapshot():
            fetched.append(True)
            cog._friend_usernames = {"newname"}

        cog._snapshot_friends = fake_snapshot
        await self.days(db, "oldname", [1])
        assert await cog._carry_history("oldname", "newname") == 1
        assert fetched == [True]

    async def test_no_move_if_the_friends_list_cant_be_fetched(self, cog, db):
        cog._friend_usernames = set()

        async def failed_snapshot():
            pass

        cog._snapshot_friends = failed_snapshot
        await self.days(db, "oldname", [1])
        assert await cog._carry_history("oldname", "newname") == 0


class TestStartup:
    async def test_archives_then_snapshots_friends_once(self):
        cog = Krillion(SimpleNamespace())
        calls = []

        async def archive():
            calls.append("archive")

        async def snapshot():
            calls.append("friends")

        cog._archive_today, cog._snapshot_friends = archive, snapshot
        await cog.on_ready()
        await cog.on_ready()  # a reconnect fires on_ready again
        assert calls == ["archive", "friends"]
