"""
Copyright © Krypton 2019-Present - https://github.com/kkrypt0nn (https://krypton.ninja)
Description:
🐍 A simple template to start to code your own and personalized Discord bot in Python

Version: 6.5.0
"""

import json
from datetime import date

import aiosqlite


class DatabaseManager:
    def __init__(self, *, connection: aiosqlite.Connection) -> None:
        self.connection = connection

    # ------------------------------------------------------------------ #
    # Leaderboard cache (shared by the gauntle/foodguessr/catfishing cogs)
    # ------------------------------------------------------------------ #

    async def upsert_leaderboard_result(
        self,
        game: str,
        channel_id: int,
        message_id: int,
        author_id: int,
        author_name: str,
        played_on: date,
        payload: dict,
    ) -> None:
        """
        Store (or overwrite) a single parsed leaderboard result.

        Keyed on (game, message_id), so re-scanning a message or reacting to an
        edit updates the existing row instead of creating a duplicate.
        """
        await self.connection.execute(
            "INSERT INTO leaderboard_results "
            "(game, channel_id, message_id, author_id, author_name, played_on, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(game, message_id) DO UPDATE SET "
            "channel_id=excluded.channel_id, author_id=excluded.author_id, "
            "author_name=excluded.author_name, played_on=excluded.played_on, "
            "payload=excluded.payload",
            (
                game,
                str(channel_id),
                str(message_id),
                str(author_id),
                author_name,
                played_on.isoformat(),
                json.dumps(payload),
            ),
        )
        await self.connection.commit()

    async def delete_leaderboard_result(self, game: str, message_id: int) -> None:
        """Remove a cached result (e.g. its message was deleted or edited away)."""
        await self.connection.execute(
            "DELETE FROM leaderboard_results WHERE game=? AND message_id=?",
            (game, str(message_id)),
        )
        await self.connection.commit()

    async def get_leaderboard_results(
        self, game: str, channel_id: int, start_iso: str, end_iso: str
    ) -> list[dict]:
        """
        Return the cached results for a channel whose played-on date falls in
        the half-open range ``[start_iso, end_iso)`` (ISO date strings).
        """
        rows = await self.connection.execute(
            "SELECT author_id, author_name, played_on, payload, message_id "
            "FROM leaderboard_results "
            "WHERE game=? AND channel_id=? AND played_on >= ? AND played_on < ? "
            "ORDER BY played_on ASC",
            (game, str(channel_id), start_iso, end_iso),
        )
        async with rows as cursor:
            result = await cursor.fetchall()
        return [
            {
                "author_id": int(row[0]),
                "author_name": row[1],
                "played_on": date.fromisoformat(row[2]),
                "payload": json.loads(row[3]),
                "message_id": int(row[4]),
            }
            for row in result
        ]

    async def clear_leaderboard_game(self, game: str) -> int:
        """
        Drop all cached results and scan state for a game.

        Used to invalidate the cache after the parsing logic changes: the next
        leaderboard command re-scans each channel from scratch and re-parses
        everything under the new rules. Returns the number of result rows removed.
        """
        cursor = await self.connection.execute(
            "DELETE FROM leaderboard_results WHERE game=?", (game,)
        )
        removed = cursor.rowcount
        await self.connection.execute(
            "DELETE FROM leaderboard_scan WHERE game=?", (game,)
        )
        await self.connection.commit()
        return removed

    async def set_autopost(self, game: str, channel_id: int, enabled: bool) -> None:
        """Opt a channel in or out of the monthly automatic leaderboard post."""
        if enabled:
            await self.connection.execute(
                "INSERT OR IGNORE INTO leaderboard_autopost (game, channel_id) "
                "VALUES (?, ?)",
                (game, str(channel_id)),
            )
        else:
            await self.connection.execute(
                "DELETE FROM leaderboard_autopost WHERE game=? AND channel_id=?",
                (game, str(channel_id)),
            )
        await self.connection.commit()

    async def get_autopost_channels(self, game: str) -> list[int]:
        """Return the channels opted in to monthly auto-posts for a game."""
        rows = await self.connection.execute(
            "SELECT channel_id FROM leaderboard_autopost WHERE game=?", (game,)
        )
        async with rows as cursor:
            result = await cursor.fetchall()
        return [int(row[0]) for row in result]

    async def get_autopost_games(self, channel_id: int) -> list[str]:
        """Return the games a channel is opted in to monthly auto-posts for."""
        rows = await self.connection.execute(
            "SELECT game FROM leaderboard_autopost WHERE channel_id=?",
            (str(channel_id),),
        )
        async with rows as cursor:
            result = await cursor.fetchall()
        return [row[0] for row in result]

    # ------------------------------------------------------------------ #
    # Introductions / invite map (all keyed by server so data stays siloed)
    # ------------------------------------------------------------------ #

    async def set_intro_channel(self, server_id: int, channel_id: int) -> None:
        """Remember which channel holds a server's introduction messages."""
        await self.connection.execute(
            "INSERT INTO intro_channels (server_id, channel_id) VALUES (?, ?) "
            "ON CONFLICT(server_id) DO UPDATE SET channel_id=excluded.channel_id",
            (str(server_id), str(channel_id)),
        )
        await self.connection.commit()

    async def get_intro_channel(self, server_id: int) -> int | None:
        """Return a server's introduction channel id, or None if unset."""
        rows = await self.connection.execute(
            "SELECT channel_id FROM intro_channels WHERE server_id=?",
            (str(server_id),),
        )
        async with rows as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row is not None else None

    async def set_invite_alias(self, server_id: int, alias: str, target: str) -> None:
        """Map an unresolved "invited by" text to a target ('id:...' or 'name:...')."""
        await self.connection.execute(
            "INSERT INTO invite_aliases (server_id, alias, target) VALUES (?, ?, ?) "
            "ON CONFLICT(server_id, alias) DO UPDATE SET target=excluded.target",
            (str(server_id), alias, target),
        )
        await self.connection.commit()

    async def get_invite_aliases(self, server_id: int) -> dict[str, str]:
        """Return a server's alias map: normalized text -> target key."""
        rows = await self.connection.execute(
            "SELECT alias, target FROM invite_aliases WHERE server_id=?",
            (str(server_id),),
        )
        async with rows as cursor:
            result = await cursor.fetchall()
        return {row[0]: row[1] for row in result}

    async def set_invite_override(
        self, server_id: int, member_id: int, inviter: str | None
    ) -> None:
        """
        Manually pin a member's inviter ('id:...', 'name:...', or None for
        "explicitly nobody"). Wins over whatever their intro message says.
        """
        await self.connection.execute(
            "INSERT INTO invite_overrides (server_id, member_id, inviter) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(server_id, member_id) DO UPDATE SET inviter=excluded.inviter",
            (str(server_id), str(member_id), inviter),
        )
        await self.connection.commit()

    async def delete_invite_override(self, server_id: int, member_id: int) -> None:
        """Remove a manual override, returning the member to automatic resolution."""
        await self.connection.execute(
            "DELETE FROM invite_overrides WHERE server_id=? AND member_id=?",
            (str(server_id), str(member_id)),
        )
        await self.connection.commit()

    async def get_invite_overrides(self, server_id: int) -> dict[int, str | None]:
        """Return a server's overrides: member id -> inviter target (or None)."""
        rows = await self.connection.execute(
            "SELECT member_id, inviter FROM invite_overrides WHERE server_id=?",
            (str(server_id),),
        )
        async with rows as cursor:
            result = await cursor.fetchall()
        return {int(row[0]): row[1] for row in result}

    async def get_leaderboard_scan(self, game: str, channel_id: int):
        """
        Return ``(newest_id, oldest_after)`` describing how much of a channel has
        been scanned for a game, or ``(None, None)`` if it's never been scanned.
        """
        rows = await self.connection.execute(
            "SELECT newest_id, oldest_after FROM leaderboard_scan "
            "WHERE game=? AND channel_id=?",
            (game, str(channel_id)),
        )
        async with rows as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None, None
        newest_id = int(row[0]) if row[0] is not None else None
        return newest_id, row[1]

    async def set_leaderboard_scan(
        self, game: str, channel_id: int, newest_id: int | None, oldest_after: str | None
    ) -> None:
        """Persist a channel's scan progress for a game."""
        await self.connection.execute(
            "INSERT INTO leaderboard_scan (game, channel_id, newest_id, oldest_after) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(game, channel_id) DO UPDATE SET "
            "newest_id=excluded.newest_id, oldest_after=excluded.oldest_after",
            (
                game,
                str(channel_id),
                str(newest_id) if newest_id is not None else None,
                oldest_after,
            ),
        )
        await self.connection.commit()
