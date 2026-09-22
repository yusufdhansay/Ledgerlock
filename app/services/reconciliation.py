"""System-wide ledger integrity check.

Double-entry bookkeeping has one global property that makes it auditable:
because every transaction writes a credit of +X and a debit of -X, the sum
of every entry in the entire system must be exactly zero. Not
approximately zero, not zero within a rounding tolerance. Exactly zero,
because the amounts are integers.

That single number is worth more than any amount of per-request testing,
because it is checkable after the fact, cheaply, without knowing anything
about what happened. If it is not zero, something wrote one side of a pair
without the other, and no amount of "the tests passed" changes that.

A net of zero is necessary but not sufficient, though, and this module
checks the rest too:

* Two offsetting errors in different currencies would still net to zero
  globally, so the net is also computed per currency.
* A stray pair of entries belonging to no transaction at all would net to
  zero, so entries with no matching transaction document are counted.
* A transaction with two credits and two debits nets to zero while being
  obviously wrong, so every transaction's entries are checked to be
  exactly one DEBIT and one CREDIT summing to zero.
* None of the above would catch an overdrawn account, so USER accounts
  with a negative computed balance are listed. That is the overdraft
  guarantee checked globally against the ledger, rather than one request
  at a time.

This is read-only. It never repairs anything, because silently correcting
a ledger discrepancy destroys the evidence of how it happened.
"""

from __future__ import annotations

from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core import db
from app.models.common import utc_now
from app.models.ledger_entry import ReconciliationReport


async def _net_and_count(database: AsyncIOMotorDatabase) -> tuple[int, int]:
    """Total signed sum and entry count across every ledger entry."""
    rows = (
        await database[db.LEDGER_ENTRIES]
        .aggregate(
            [
                {
                    "$group": {
                        "_id": None,
                        "net": {"$sum": "$signed_amount_minor"},
                        "count": {"$sum": 1},
                    }
                }
            ]
        )
        .to_list(length=1)
    )
    if not rows:
        return 0, 0
    return int(rows[0]["net"]), int(rows[0]["count"])


async def _net_per_currency(database: AsyncIOMotorDatabase) -> dict[str, int]:
    """Signed sum grouped by currency. Each one must be zero on its own."""
    rows = (
        await database[db.LEDGER_ENTRIES]
        .aggregate(
            [
                {
                    "$group": {
                        "_id": "$currency",
                        "net": {"$sum": "$signed_amount_minor"},
                    }
                }
            ]
        )
        .to_list(length=None)
    )
    return {str(row["_id"]): int(row["net"]) for row in rows}


async def _count_entries_without_transaction(
    database: AsyncIOMotorDatabase,
) -> int:
    """Entries pointing at a transaction document that does not exist."""
    rows = (
        await database[db.LEDGER_ENTRIES]
        .aggregate(
            [
                {
                    "$lookup": {
                        "from": db.TRANSACTIONS,
                        "localField": "transaction_id",
                        "foreignField": "_id",
                        "as": "matched_transaction",
                    }
                },
                {"$match": {"matched_transaction": {"$size": 0}}},
                {"$count": "orphans"},
            ]
        )
        .to_list(length=1)
    )
    return int(rows[0]["orphans"]) if rows else 0


async def _count_unbalanced_transaction_groups(
    database: AsyncIOMotorDatabase,
) -> int:
    """Transactions whose entries are not a clean debit/credit pair.

    Groups every entry by its transaction and rejects any group that is not
    exactly one DEBIT plus one CREDIT summing to zero. A half-written pair,
    a duplicated side, or a mismatched amount all show up here even though
    the global net might still be zero.
    """
    rows = (
        await database[db.LEDGER_ENTRIES]
        .aggregate(
            [
                {
                    "$group": {
                        "_id": "$transaction_id",
                        "entry_count": {"$sum": 1},
                        "net": {"$sum": "$signed_amount_minor"},
                        "debits": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$direction", "DEBIT"]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "credits": {
                            "$sum": {
                                "$cond": [
                                    {"$eq": ["$direction", "CREDIT"]},
                                    1,
                                    0,
                                ]
                            }
                        },
                    }
                },
                {
                    "$match": {
                        "$or": [
                            {"entry_count": {"$ne": 2}},
                            {"debits": {"$ne": 1}},
                            {"credits": {"$ne": 1}},
                            {"net": {"$ne": 0}},
                        ]
                    }
                },
                {"$count": "unbalanced"},
            ]
        )
        .to_list(length=1)
    )
    return int(rows[0]["unbalanced"]) if rows else 0


async def _find_negative_user_accounts(
    database: AsyncIOMotorDatabase,
) -> list[str]:
    """USER accounts whose computed balance is below zero.

    Must always be empty. SYSTEM accounts are excluded deliberately: their
    negative balance is the intended representation of value that has
    entered the ledger from outside, and equals the total held across user
    accounts in that currency.
    """
    rows = (
        await database[db.LEDGER_ENTRIES]
        .aggregate(
            [
                {
                    "$group": {
                        "_id": "$account_id",
                        "balance": {"$sum": "$signed_amount_minor"},
                    }
                },
                {"$match": {"balance": {"$lt": 0}}},
                {
                    "$lookup": {
                        "from": db.ACCOUNTS,
                        "localField": "_id",
                        "foreignField": "_id",
                        "as": "account",
                    }
                },
                {"$unwind": "$account"},
                {"$match": {"account.account_type": "USER"}},
                {"$project": {"_id": 1, "balance": 1}},
            ]
        )
        .to_list(length=None)
    )
    return [str(row["_id"]) for row in rows]


async def reconcile(
    database: AsyncIOMotorDatabase | None = None,
) -> ReconciliationReport:
    """Run every integrity check and return the report.

    Assert on `healthy`, not on `balanced`: `balanced` only covers the
    sum-to-zero invariant, while `healthy` additionally requires no
    orphaned entries, no malformed transaction groups, and no overdrawn
    user accounts.
    """
    active_database = database if database is not None else db.get_database()

    net_signed_minor, total_entries = await _net_and_count(active_database)
    per_currency = await _net_per_currency(active_database)
    entries_without_transaction = await _count_entries_without_transaction(
        active_database
    )
    unbalanced_groups = await _count_unbalanced_transaction_groups(active_database)
    negative_user_accounts = await _find_negative_user_accounts(active_database)
    total_transactions = await active_database[db.TRANSACTIONS].count_documents({})

    balanced = net_signed_minor == 0 and all(net == 0 for net in per_currency.values())
    healthy = (
        balanced
        and entries_without_transaction == 0
        and unbalanced_groups == 0
        and not negative_user_accounts
    )

    return ReconciliationReport(
        balanced=balanced,
        healthy=healthy,
        net_signed_minor=net_signed_minor,
        total_entries=total_entries,
        total_transactions=total_transactions,
        entries_without_transaction=entries_without_transaction,
        unbalanced_transaction_groups=unbalanced_groups,
        negative_user_accounts=negative_user_accounts,
        per_currency_net_minor=per_currency,
        checked_at=utc_now(),
    )


async def summarise_for_log() -> dict[str, Any]:
    """Compact form of the report, for structured logging."""
    report = await reconcile()
    return {
        "healthy": report.healthy,
        "net_signed_minor": report.net_signed_minor,
        "total_entries": report.total_entries,
        "total_transactions": report.total_transactions,
        "negative_user_account_count": len(report.negative_user_accounts),
    }
