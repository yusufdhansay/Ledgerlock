"""Shared pytest fixtures for Ledgerlock.

Tests run against a real MongoDB single-node replica set (see
docker-compose.yml). They are deliberately NOT run against a mock or an
in-memory fake: the guarantees this project exists to prove (atomic
multi-document writes, no overdraft under concurrency, idempotency under
duplicate submission) are properties of MongoDB's transaction layer.
Mocking the database out would mean testing nothing that matters.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

# Host-side default matches the port mapping in docker-compose.yml.
# directConnection=true is required because the single-node replica set
# advertises itself as localhost:27017.
DEFAULT_TEST_MONGODB_URI = "mongodb://localhost:27017/?directConnection=true"

TEST_DB_NAME = "ledgerlock_test"


def test_mongodb_uri() -> str:
    """Return the MongoDB URI the test suite should connect to."""
    return os.getenv("MONGODB_TEST_URI", DEFAULT_TEST_MONGODB_URI)


@pytest.fixture
async def mongo_client() -> AsyncIterator[AsyncIOMotorClient]:
    """An open Motor client, closed on teardown."""
    client: AsyncIOMotorClient = AsyncIOMotorClient(
        test_mongodb_uri(),
        serverSelectionTimeoutMS=5000,
    )
    try:
        yield client
    finally:
        client.close()


@pytest.fixture
async def scratch_db(
    mongo_client: AsyncIOMotorClient,
) -> AsyncIterator[AsyncIOMotorDatabase]:
    """A dedicated throwaway database, dropped before and after each test."""
    db_name = f"{TEST_DB_NAME}_scratch"
    await mongo_client.drop_database(db_name)
    try:
        yield mongo_client[db_name]
    finally:
        await mongo_client.drop_database(db_name)
