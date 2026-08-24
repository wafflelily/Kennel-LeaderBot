"""Shared fixtures: an in-memory database with the real schema applied."""

import pathlib

import aiosqlite
import pytest_asyncio

from database import DatabaseManager

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "database" / "schema.sql"


@pytest_asyncio.fixture
async def db():
    """A DatabaseManager backed by a fresh in-memory SQLite database."""
    connection = await aiosqlite.connect(":memory:")
    await connection.executescript(SCHEMA.read_text(encoding="utf-8"))
    await connection.commit()
    yield DatabaseManager(connection=connection)
    await connection.close()
