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

# Configuration must be in place before anything imports app.core.config,
# because Settings reads the environment at instantiation. Setting these
# here, at conftest import time, is what keeps the suite off the
# development database.
TEST_DB_NAME = "ledgerlock_test"
DEFAULT_TEST_MONGODB_URI = "mongodb://localhost:27017/?directConnection=true"

os.environ.setdefault("MONGODB_TEST_URI", DEFAULT_TEST_MONGODB_URI)
os.environ["MONGODB_URI"] = os.environ["MONGODB_TEST_URI"]
os.environ["MONGODB_DB_NAME"] = TEST_DB_NAME
# A real 64-char key, but a throwaway one that only ever signs test
# tokens. Not a secret, and never used by a deployment.
os.environ["JWT_SECRET_KEY"] = (
    "test-only-signing-key-do-not-use-anywhere-real-0123456789abcdef"
)
# bcrypt at cost 12 takes ~250ms per hash. Fixtures in the concurrency
# suite create dozens of users, so the suite would spend most of its time
# hashing. Lowered here only; production default stays 12.
os.environ["BCRYPT_ROUNDS"] = "4"
os.environ["APP_ENV"] = "test"
os.environ["LOG_LEVEL"] = "WARNING"

import pytest  # noqa: E402
from asgi_lifespan import LifespanManager  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase  # noqa: E402

from app.core import db as db_module  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402


def test_mongodb_uri() -> str:
    """Return the MongoDB URI the test suite should connect to."""
    return os.environ["MONGODB_TEST_URI"]


@pytest.fixture(autouse=True)
def reset_settings_cache() -> None:
    """Drop the cached Settings so env changes inside a test are honoured."""
    get_settings.cache_clear()


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
    """A dedicated throwaway database, dropped before and after each test.

    Used by tests that need to poke at raw collections without the
    application's schema validators or indexes in the way.
    """
    db_name = f"{TEST_DB_NAME}_scratch"
    await mongo_client.drop_database(db_name)
    try:
        yield mongo_client[db_name]
    finally:
        await mongo_client.drop_database(db_name)


@pytest.fixture
async def app_database(
    mongo_client: AsyncIOMotorClient,
) -> AsyncIterator[AsyncIOMotorDatabase]:
    """The application's own test database, emptied before each test.

    Documents are deleted rather than the database dropped, so the indexes
    and `$jsonSchema` validators created at startup survive between tests.
    Several guarantees under test (unique idempotency key, one entry per
    direction per transaction, the sign-convention validator) *are* those
    indexes and validators, so dropping them would quietly turn those tests
    into no-ops.
    """
    database = mongo_client[TEST_DB_NAME]
    await db_module.initialise_database(database)
    for name in db_module.ALL_COLLECTIONS:
        await database[name].delete_many({})
    yield database


@pytest.fixture
async def client(
    app_database: AsyncIOMotorDatabase,
) -> AsyncIterator[AsyncClient]:
    """An HTTP client wired to the real ASGI app, with lifespan run.

    Running the lifespan matters: it is what connects MongoDB, verifies the
    deployment is a replica set, and creates the indexes and validators. A
    TestClient that skips it would be testing a different application.
    """
    application = create_app()
    async with LifespanManager(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(
            transport=transport, base_url="http://ledgerlock.test"
        ) as http_client:
            yield http_client
