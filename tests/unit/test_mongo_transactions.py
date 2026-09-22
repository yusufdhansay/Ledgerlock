"""Phase 0 precondition checks: does this MongoDB deployment actually
support multi-document transactions?

Everything in Ledgerlock is built on the assumption that a single
`session.with_transaction(...)` block can atomically (a) read a balance
and (b) write a transaction document plus two ledger entries, with a
real rollback if anything inside fails. That assumption is only true on
a replica set. A standalone mongod accepts the connection happily and
then refuses to start a transaction.

These tests verify the assumption directly rather than trusting the
compose file, so a misconfigured deployment fails loudly here instead of
silently degrading a correctness guarantee later.
"""

from __future__ import annotations

import pytest
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo.errors import OperationFailure


async def test_deployment_is_a_replica_set(mongo_client: AsyncIOMotorClient) -> None:
    """The deployment must be a replica set, and this node must be primary."""
    hello = await mongo_client.admin.command("hello")

    assert "setName" in hello, (
        "MongoDB is not running as a replica set. Multi-document "
        "transactions are unavailable, so Ledgerlock's atomicity "
        "guarantee cannot hold. Start it via docker-compose.yml."
    )
    assert hello["isWritablePrimary"] is True, (
        f"Connected node is not a writable primary (setName={hello['setName']}). "
        "Transactions require a primary."
    )


async def test_with_transaction_commits_all_writes(
    mongo_client: AsyncIOMotorClient, scratch_db: AsyncIOMotorDatabase
) -> None:
    """A committed transaction makes every write inside it visible."""
    first = scratch_db["first"]
    second = scratch_db["second"]

    async with await mongo_client.start_session() as session:

        async def write_both(active_session: object) -> None:
            await first.insert_one({"marker": "a"}, session=active_session)
            await second.insert_one({"marker": "b"}, session=active_session)

        await session.with_transaction(write_both)

    assert await first.count_documents({"marker": "a"}) == 1
    assert await second.count_documents({"marker": "b"}) == 1


async def test_with_transaction_rolls_back_every_write_on_failure(
    mongo_client: AsyncIOMotorClient, scratch_db: AsyncIOMotorDatabase
) -> None:
    """A failure mid-transaction must leave NO partial writes behind.

    This is the property that makes "a debit entry can never exist
    without its matching credit entry" true.
    """
    first = scratch_db["first"]
    second = scratch_db["second"]

    class DeliberateFailure(RuntimeError):
        pass

    async with await mongo_client.start_session() as session:

        async def write_then_fail(active_session: object) -> None:
            await first.insert_one({"marker": "a"}, session=active_session)
            await second.insert_one({"marker": "b"}, session=active_session)
            raise DeliberateFailure("simulated mid-transaction failure")

        with pytest.raises(DeliberateFailure):
            await session.with_transaction(write_then_fail)

    assert await first.count_documents({}) == 0, "first write was not rolled back"
    assert await second.count_documents({}) == 0, "second write was not rolled back"


async def test_unique_index_violation_aborts_the_whole_transaction(
    mongo_client: AsyncIOMotorClient, scratch_db: AsyncIOMotorDatabase
) -> None:
    """A duplicate-key error inside a transaction aborts the whole thing.

    This is the mechanism Ledgerlock uses for idempotency: a unique index
    on the idempotency key means a concurrent duplicate submission fails
    at write time, not at check time, so there is no read-then-write race
    window to lose.
    """
    keys = scratch_db["keys"]
    entries = scratch_db["entries"]
    await keys.create_index("idempotency_key", unique=True)
    await keys.insert_one({"idempotency_key": "already-used"})

    async with await mongo_client.start_session() as session:

        async def insert_entry_then_duplicate_key(active_session: object) -> None:
            await entries.insert_one({"marker": "orphan"}, session=active_session)
            await keys.insert_one(
                {"idempotency_key": "already-used"}, session=active_session
            )

        with pytest.raises(OperationFailure):
            await session.with_transaction(insert_entry_then_duplicate_key)

    assert await entries.count_documents({}) == 0, (
        "the entry written before the duplicate-key failure survived; "
        "the transaction did not roll back"
    )
