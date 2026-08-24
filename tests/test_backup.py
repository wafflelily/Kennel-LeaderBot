"""Tests for the database backup cog, over the real in-memory database."""

import logging
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

from cogs.backup import KEEP, Backup


@pytest.fixture
def cog(db, tmp_path):
    backup_cog = Backup(
        SimpleNamespace(database=db, logger=logging.getLogger("test"))
    )
    backup_cog.backup_dir = tmp_path
    return backup_cog


class TestBackupNow:
    async def test_creates_a_valid_snapshot(self, cog, db, tmp_path):
        await db.upsert_leaderboard_result(
            "gauntle", 100, 1, 42, "alice", date(2026, 8, 5), {"total": 1.0}
        )
        await db.set_invite_alias(1, "batty", "id:42")

        target = await cog._backup_now()
        assert target.parent == tmp_path
        assert target.name.startswith("database-")

        # The snapshot is a complete, readable SQLite database.
        connection = sqlite3.connect(target)
        try:
            results = connection.execute(
                "SELECT game, author_name FROM leaderboard_results"
            ).fetchall()
            aliases = connection.execute(
                "SELECT alias, target FROM invite_aliases"
            ).fetchall()
        finally:
            connection.close()
        assert results == [("gauntle", "alice")]
        assert aliases == [("batty", "id:42")]

    async def test_same_day_backup_overwrites_with_fresh_data(self, cog, db):
        first = await cog._backup_now()
        await db.set_intro_channel(1, 555)
        second = await cog._backup_now()
        assert first == second  # one file per day

        connection = sqlite3.connect(second)
        try:
            rows = connection.execute(
                "SELECT server_id, channel_id FROM intro_channels"
            ).fetchall()
        finally:
            connection.close()
        assert rows == [("1", "555")]

    async def test_creates_the_backup_directory(self, cog, tmp_path):
        cog.backup_dir = tmp_path / "nested" / "backups"
        target = await cog._backup_now()
        assert target.exists()


class TestPrune:
    async def test_keeps_only_the_newest_snapshots(self, cog, tmp_path):
        for day in range(1, 11):
            (tmp_path / f"database-2026-08-{day:02d}.db").write_bytes(b"old")
        cog._prune()
        remaining = sorted(path.name for path in tmp_path.glob("database-*.db"))
        assert len(remaining) == KEEP
        assert remaining[0] == "database-2026-08-04.db"
        assert remaining[-1] == "database-2026-08-10.db"

    async def test_other_files_are_untouched(self, cog, tmp_path):
        (tmp_path / "notes.txt").write_text("keep me")
        for day in range(1, 10):
            (tmp_path / f"database-2026-08-{day:02d}.db").write_bytes(b"old")
        cog._prune()
        assert (tmp_path / "notes.txt").exists()
