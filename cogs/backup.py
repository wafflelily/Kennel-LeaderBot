"""
Daily database backups.

The leaderboard cache is rebuildable from Discord history, but the rest of
database.db is not: intro channels, invite aliases and overrides, and autopost
opt-ins are hand-curated state that would be gone for good with the file. A
daily task snapshots the database into ``database/backups/`` (inside the same
Docker volume as the database itself) keeping the last ``KEEP`` days, a
catch-up snapshot is taken on startup if today's is missing, and the
owner-only /backup command takes one on demand.

Snapshots use SQLite's online backup API rather than a file copy, so backing
up a live database that's mid-write can never produce a torn file.
"""

import pathlib
from datetime import datetime, time, timezone

import aiosqlite
import discord
from discord.ext import commands, tasks
from discord.ext.commands import Context

# When the daily snapshot is taken (UTC).
BACKUP_AT = time(hour=4, tzinfo=timezone.utc)
# How many daily snapshots to keep.
KEEP = 7
BACKUP_DIR = pathlib.Path(__file__).resolve().parents[1] / "database" / "backups"


class Backup(commands.Cog, name="backup"):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.backup_dir = BACKUP_DIR
        self._startup_backup_done = False

    async def cog_load(self) -> None:
        self.daily_backup.start()

    async def cog_unload(self) -> None:
        self.daily_backup.cancel()

    async def _backup_now(self) -> pathlib.Path:
        """Snapshot the live database; one file per day, re-runs overwrite."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        target = (
            self.backup_dir / f"database-{datetime.now(timezone.utc):%Y-%m-%d}.db"
        )
        async with aiosqlite.connect(target) as destination:
            await self.bot.database.connection.backup(destination)
        self._prune()
        return target

    def _prune(self) -> None:
        """Drop all but the newest KEEP snapshots (dated names sort by age)."""
        for old in sorted(self.backup_dir.glob("database-*.db"))[:-KEEP]:
            old.unlink()

    async def _try_backup(self) -> None:
        try:
            target = await self._backup_now()
            self.bot.logger.info(f"Database backed up to {target}")
        except Exception:
            self.bot.logger.error("Database backup failed", exc_info=True)

    @tasks.loop(time=BACKUP_AT)
    async def daily_backup(self) -> None:
        await self._try_backup()

    @daily_backup.before_loop
    async def before_daily_backup(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Catch-up snapshot on startup, so a bot that's restarted often and
        # never awake at BACKUP_AT still gets one per day.
        if self._startup_backup_done:
            return
        self._startup_backup_done = True
        today = (
            self.backup_dir / f"database-{datetime.now(timezone.utc):%Y-%m-%d}.db"
        )
        if not today.exists():
            await self._try_backup()

    @commands.hybrid_command(
        name="backup",
        description="Take a database backup now.",
    )
    @commands.is_owner()
    async def backup(self, context: Context) -> None:
        """
        Snapshot the database on demand.

        :param context: The hybrid command context.
        """
        try:
            target = await self._backup_now()
        except Exception:
            self.bot.logger.error("Database backup failed", exc_info=True)
            await context.send(
                embed=discord.Embed(
                    title="Error!",
                    description="Backup failed — check the log for details.",
                    color=0xE02B2B,
                )
            )
            return
        size_kb = target.stat().st_size / 1024
        await context.send(
            embed=discord.Embed(
                title="💾 Backup complete",
                description=(
                    f"Saved `{target.name}` ({size_kb:,.0f} KB).\n"
                    f"The last {KEEP} daily backups are kept in `database/backups/`."
                ),
                color=0xBEBEFE,
            )
        )


async def setup(bot) -> None:
    await bot.add_cog(Backup(bot))
