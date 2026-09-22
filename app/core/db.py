"""MongoDB connection, collection accessors, indexes, and validators.

Three jobs, all of them load-bearing:

1. Hold the Motor client for the process lifetime and hand out typed
   collection handles, so no module constructs its own client.
2. Refuse to start if the deployment is not a transaction-capable replica
   set. Failing at startup is much better than discovering it on the first
   transfer, when the atomicity guarantee has already been silently lost.
3. Create the indexes and `$jsonSchema` validators that turn several of
   this project's correctness claims into database-enforced constraints
   rather than application conventions. Which constraints those are is
   documented inline, because "the database enforces it" and "our code
   remembers to check it" are very different strength claims and the
   difference should be readable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorCollection,
    AsyncIOMotorDatabase,
)
from pymongo.errors import CollectionInvalid, OperationFailure

from app.core.logging_config import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------
# Collection names, in one place so a typo cannot silently create a new
# empty collection at runtime.
# ---------------------------------------------------------------------
USERS = "users"
REVOKED_TOKENS = "revoked_tokens"
ACCOUNTS = "accounts"
TRANSACTIONS = "transactions"
LEDGER_ENTRIES = "ledger_entries"

ALL_COLLECTIONS = (USERS, REVOKED_TOKENS, ACCOUNTS, TRANSACTIONS, LEDGER_ENTRIES)


class DatabaseNotConnectedError(RuntimeError):
    """Raised if a collection is requested before startup completed."""


@dataclass
class MongoConnection:
    """Process-wide handle to the Motor client and database."""

    client: AsyncIOMotorClient | None = field(default=None)
    database: AsyncIOMotorDatabase | None = field(default=None)
    replica_set_name: str | None = field(default=None)


connection = MongoConnection()


# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------


async def connect_to_mongo(
    uri: str,
    database_name: str,
    *,
    server_selection_timeout_ms: int = 5000,
) -> AsyncIOMotorDatabase:
    """Open the client, verify transaction support, and return the database."""
    client: AsyncIOMotorClient = AsyncIOMotorClient(
        uri,
        serverSelectionTimeoutMS=server_selection_timeout_ms,
        # Reads and writes both go to the primary. A secondary read could
        # return a stale balance, and a balance that is stale by even one
        # committed transfer is an overdraft waiting to happen.
        readPreference="primary",
        # Majority write concern with journalling: a committed transfer
        # survives a primary failover. Anything weaker means an
        # acknowledged transfer can disappear.
        w="majority",
        journal=True,
    )
    connection.client = client
    connection.database = client[database_name]
    connection.replica_set_name = await _verify_transaction_support(client)

    logger.info(
        "mongo_connected",
        extra={
            "database": database_name,
            "replica_set": connection.replica_set_name,
        },
    )
    return connection.database


async def _verify_transaction_support(client: AsyncIOMotorClient) -> str:
    """Assert the deployment can run multi-document transactions.

    A standalone mongod accepts connections and ordinary writes perfectly
    happily, then rejects `startTransaction`. Checking here converts that
    from a runtime surprise into a refusal to boot.
    """
    hello = await client.admin.command("hello")
    replica_set_name = hello.get("setName")

    if not replica_set_name:
        raise RuntimeError(
            "MongoDB is not running as a replica set, so multi-document "
            "transactions are unavailable. Ledgerlock's atomicity guarantee "
            "(a debit entry can never exist without its matching credit "
            "entry) depends on them, so it will not start without one. "
            "Bring up the replica set with: docker compose up -d"
        )
    if not hello.get("isWritablePrimary", False):
        raise RuntimeError(
            f"Connected to replica set {replica_set_name!r} but this node is "
            "not a writable primary. Transactions require a primary."
        )
    return str(replica_set_name)


async def close_mongo_connection() -> None:
    """Close the client. Safe to call when never connected."""
    if connection.client is not None:
        connection.client.close()
        logger.info("mongo_disconnected", extra={})
    connection.client = None
    connection.database = None
    connection.replica_set_name = None


def get_database() -> AsyncIOMotorDatabase:
    """Return the connected database, or raise if startup has not run."""
    if connection.database is None:
        raise DatabaseNotConnectedError(
            "MongoDB is not connected. This means application startup did not "
            "run; check the lifespan handler in app.main."
        )
    return connection.database


def get_client() -> AsyncIOMotorClient:
    """Return the connected client. Needed to start sessions."""
    if connection.client is None:
        raise DatabaseNotConnectedError("MongoDB is not connected.")
    return connection.client


# ---------------------------------------------------------------------
# Collection accessors
# ---------------------------------------------------------------------


def users_collection() -> AsyncIOMotorCollection:
    return get_database()[USERS]


def revoked_tokens_collection() -> AsyncIOMotorCollection:
    return get_database()[REVOKED_TOKENS]


def accounts_collection() -> AsyncIOMotorCollection:
    return get_database()[ACCOUNTS]


def transactions_collection() -> AsyncIOMotorCollection:
    return get_database()[TRANSACTIONS]


def ledger_entries_collection() -> AsyncIOMotorCollection:
    return get_database()[LEDGER_ENTRIES]


# ---------------------------------------------------------------------
# Schema validators
# ---------------------------------------------------------------------


def _ledger_entry_validator() -> dict[str, Any]:
    """Validator for `ledger_entries`.

    Two parts, and the second one is the interesting half:

    * `$jsonSchema` pins the shape: required fields, BSON types, a
      positive magnitude, and `additionalProperties: false` so no
      unexpected field can be written.
    * `$expr` enforces the *sign convention* itself:
      `signed_amount_minor` must equal `+amount_minor` for a CREDIT and
      `-amount_minor` for a DEBIT. This is a real database-level
      invariant, checked on insert and on any attempted update, not a
      convention the application is trusted to honour. It means the
      balance and reconciliation aggregations can sum
      `signed_amount_minor` and trust the result.
    """
    return {
        "$and": [
            {
                "$jsonSchema": {
                    "bsonType": "object",
                    "required": [
                        "_id",
                        "transaction_id",
                        "account_id",
                        "direction",
                        "amount_minor",
                        "signed_amount_minor",
                        "currency",
                        "created_at",
                    ],
                    "additionalProperties": False,
                    "properties": {
                        "_id": {"bsonType": "objectId"},
                        "transaction_id": {"bsonType": "objectId"},
                        "account_id": {"bsonType": "objectId"},
                        "direction": {"enum": ["DEBIT", "CREDIT"]},
                        "amount_minor": {
                            "bsonType": ["int", "long"],
                            "minimum": 1,
                            "description": "Magnitude in minor units, always positive.",
                        },
                        "signed_amount_minor": {"bsonType": ["int", "long"]},
                        "currency": {
                            "bsonType": "string",
                            "pattern": "^[A-Z]{3}$",
                        },
                        "created_at": {"bsonType": "date"},
                    },
                }
            },
            {
                "$expr": {
                    "$eq": [
                        "$signed_amount_minor",
                        {
                            "$cond": [
                                {"$eq": ["$direction", "CREDIT"]},
                                "$amount_minor",
                                {"$multiply": ["$amount_minor", -1]},
                            ]
                        },
                    ]
                }
            },
        ]
    }


def _transaction_validator() -> dict[str, Any]:
    """Validator for `transactions`: shape plus a positive amount."""
    return {
        "$jsonSchema": {
            "bsonType": "object",
            "required": [
                "_id",
                "idempotency_key",
                "kind",
                "status",
                "source_account_id",
                "destination_account_id",
                "amount_minor",
                "currency",
                "created_at",
            ],
            "additionalProperties": False,
            "properties": {
                "_id": {"bsonType": "objectId"},
                "idempotency_key": {
                    "bsonType": "string",
                    "minLength": 8,
                    "maxLength": 128,
                },
                "kind": {"enum": ["TRANSFER", "FUNDING"]},
                "status": {"enum": ["PENDING", "COMPLETED", "FAILED", "REVERSED"]},
                "source_account_id": {"bsonType": "objectId"},
                "destination_account_id": {"bsonType": "objectId"},
                "amount_minor": {"bsonType": ["int", "long"], "minimum": 1},
                "currency": {"bsonType": "string", "pattern": "^[A-Z]{3}$"},
                "description": {"bsonType": "string", "maxLength": 280},
                "submitted_by": {"bsonType": ["objectId", "null"]},
                "created_at": {"bsonType": "date"},
            },
        }
    }


COLLECTION_VALIDATORS: dict[str, dict[str, Any]] = {
    LEDGER_ENTRIES: _ledger_entry_validator(),
    TRANSACTIONS: _transaction_validator(),
}


async def ensure_collections_and_validators(
    database: AsyncIOMotorDatabase | None = None,
) -> None:
    """Create every collection up front and attach its validator.

    Collections are created explicitly rather than implicitly on first
    insert. Implicit creation inside a multi-document transaction is a
    sharp edge across MongoDB versions, and the very first transfer a
    fresh deployment handles would otherwise be the one to hit it.
    """
    db = database if database is not None else get_database()
    existing = set(await db.list_collection_names())

    for name in ALL_COLLECTIONS:
        validator = COLLECTION_VALIDATORS.get(name)
        if name in existing:
            if validator is not None:
                # collMod applies (or re-applies) the validator to an
                # existing collection, so upgrading a deployment picks up a
                # tightened schema.
                await db.command(
                    {
                        "collMod": name,
                        "validator": validator,
                        "validationLevel": "strict",
                        "validationAction": "error",
                    }
                )
            continue

        try:
            if validator is not None:
                await db.create_collection(
                    name,
                    validator=validator,
                    validationLevel="strict",
                    validationAction="error",
                )
            else:
                await db.create_collection(name)
        except CollectionInvalid:
            # Another worker won the race. Harmless.
            pass

    logger.info(
        "mongo_collections_ready",
        extra={"collections": list(ALL_COLLECTIONS)},
    )


# ---------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------


async def ensure_indexes(database: AsyncIOMotorDatabase | None = None) -> None:
    """Create every index the application relies on.

    Read the comments: some of these are performance, but three of them
    are correctness constraints that the application deliberately leans on
    instead of checking in Python.
    """
    db = database if database is not None else get_database()

    # --- users -------------------------------------------------------
    # Unique email. Emails are normalised to lower case before insert, so
    # this cannot be bypassed by changing capitalisation.
    await db[USERS].create_index("email", unique=True, name="uq_email")

    # --- revoked_tokens (the JWT blacklist) --------------------------
    # Unique jti makes revoking the same token twice a no-op rather than
    # creating duplicate rows.
    await db[REVOKED_TOKENS].create_index("jti", unique=True, name="uq_jti")
    # TTL index: MongoDB deletes a revocation record once the token it
    # refers to would have expired anyway, so the blacklist stays bounded
    # without a cleanup job. expireAfterSeconds=0 means "expire at the
    # instant stored in this date field".
    await db[REVOKED_TOKENS].create_index(
        "expires_at", expireAfterSeconds=0, name="ttl_expires_at"
    )

    # --- accounts ----------------------------------------------------
    await db[ACCOUNTS].create_index("owner_id", name="ix_owner_id")
    # CORRECTNESS: at most one SYSTEM account per currency. SYSTEM accounts
    # are exempt from the overdraft check, so a duplicate one would be a
    # second, untracked source of money entering the ledger. The partial
    # filter keeps the constraint off USER accounts, which are
    # unrestricted.
    await db[ACCOUNTS].create_index(
        [("account_type", 1), ("currency", 1)],
        unique=True,
        partialFilterExpression={"account_type": "SYSTEM"},
        name="uq_system_account_per_currency",
    )

    # --- transactions ------------------------------------------------
    # CORRECTNESS, and the single most important index in the project:
    # this unique index is what makes idempotency hold under concurrency.
    # An application-level "have I seen this key?" read cannot do it,
    # because two concurrent submissions both read "no" before either
    # writes. With this index the duplicate fails at *write* time inside
    # the transaction, which aborts the whole transaction including its
    # ledger entries.
    await db[TRANSACTIONS].create_index(
        "idempotency_key", unique=True, name="uq_idempotency_key"
    )
    await db[TRANSACTIONS].create_index(
        [("source_account_id", 1), ("created_at", -1)],
        name="ix_source_created",
    )
    await db[TRANSACTIONS].create_index(
        [("destination_account_id", 1), ("created_at", -1)],
        name="ix_destination_created",
    )

    # --- ledger_entries ----------------------------------------------
    # Backs the balance aggregation: filter by account, sum signed amounts.
    await db[LEDGER_ENTRIES].create_index(
        [("account_id", 1), ("created_at", -1)],
        name="ix_account_created",
    )
    # CORRECTNESS: exactly one DEBIT and at most one CREDIT per
    # transaction. A bug that wrote two debits for one transaction (or
    # replayed one side) would violate this index and fail, rather than
    # quietly unbalancing the ledger.
    await db[LEDGER_ENTRIES].create_index(
        [("transaction_id", 1), ("direction", 1)],
        unique=True,
        name="uq_one_entry_per_direction_per_transaction",
    )

    logger.info("mongo_indexes_ready", extra={})


async def initialise_database(database: AsyncIOMotorDatabase | None = None) -> None:
    """Run the full schema setup: collections, validators, then indexes."""
    db = database if database is not None else get_database()
    await ensure_collections_and_validators(db)
    try:
        await ensure_indexes(db)
    except OperationFailure as exc:  # pragma: no cover - surfaced at startup
        raise RuntimeError(
            f"Failed to create required indexes: {exc}. The application's "
            "idempotency and single-entry-per-direction guarantees depend on "
            "them, so it will not continue without them."
        ) from exc
