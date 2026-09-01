"""Tests for /import: scanning one channel's results into another's data."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from cogs.gauntle import Gauntle
from cogs.owner import Owner

SOURCE_ID = 111
TARGET_ID = 222


def make_message(mid, content, *, author_id=1, is_bot=False, when=None):
    return SimpleNamespace(
        id=mid,
        content=content,
        author=SimpleNamespace(id=author_id, display_name=f"p{author_id}", bot=is_bot),
        channel=SimpleNamespace(id=SOURCE_ID),
        created_at=when or datetime(2026, 8, 5, tzinfo=timezone.utc),
    )


class FakeSource:
    def __init__(self, messages):
        self.id = SOURCE_ID
        self.messages = messages

    def history(self, *, limit=None, oldest_first=False, **kwargs):
        async def generate():
            for message in sorted(
                self.messages, key=lambda m: m.id, reverse=not oldest_first
            ):
                yield message

        return generate()


RUN = "I ran the August 5th Gauntlet in 15 minutes and 0 seconds!"
FASTER_RUN = "I ran the August 6th Gauntlet in 14 minutes and 0 seconds!"


@pytest.fixture
def gauntle(db):
    return Gauntle(SimpleNamespace(database=db))


async def target_rows(db):
    return await db.get_leaderboard_results(
        "gauntle", TARGET_ID, "0000-01-01", "9999-12-31"
    )


class TestImportHistory:
    async def test_results_land_under_the_target_channel(self, db, gauntle):
        source = FakeSource(
            [
                make_message(1, RUN),
                make_message(2, "nice run!"),  # chat: skipped
                make_message(3, FASTER_RUN, is_bot=True),  # bot: skipped
                make_message(4, FASTER_RUN, author_id=2),
            ]
        )
        imported = await Owner._import_history(gauntle, source, TARGET_ID)
        assert imported == 2

        rows = await target_rows(db)
        assert [(row["message_id"], row["author_name"]) for row in rows] == [
            (1, "p1"),
            (4, "p2"),
        ]
        # Nothing was stored under the source channel, and it isn't tracked.
        assert (
            await db.get_leaderboard_results(
                "gauntle", SOURCE_ID, "0000-01-01", "9999-12-31"
            )
            == []
        )
        assert await db.get_leaderboard_scan("gauntle", SOURCE_ID) == (None, None)

    async def test_reimport_is_idempotent(self, db, gauntle):
        source = FakeSource([make_message(1, RUN)])
        await Owner._import_history(gauntle, source, TARGET_ID)
        await Owner._import_history(gauntle, source, TARGET_ID)
        assert len(await target_rows(db)) == 1

    async def test_merges_with_the_target_channels_own_results(self, db, gauntle):
        await db.upsert_leaderboard_result(
            "gauntle", TARGET_ID, 99, 3, "local", date(2026, 8, 7), {"total": 1.0}
        )
        await Owner._import_history(gauntle, FakeSource([make_message(1, RUN)]), TARGET_ID)
        rows = await target_rows(db)
        assert sorted(row["message_id"] for row in rows) == [1, 99]

    async def test_imported_dates_come_from_the_parser(self, db, gauntle):
        await Owner._import_history(gauntle, FakeSource([make_message(1, RUN)]), TARGET_ID)
        rows = await target_rows(db)
        assert rows[0]["played_on"] == date(2026, 8, 5)
        assert rows[0]["payload"]["total"] == 900.0
