"""Balance computation by aggregation over `ledger_entries`.

There is no stored balance field to read, because none exists on the
account document and none ever will (RULES.md). Every balance in this
system is derived, at the moment it is asked for, by summing that
account's ledger entries.

That is not a performance choice, it is the whole point. A stored balance
is a second source of truth, and keeping it in step with the ledger under
concurrency is the problem that produces the bug this project exists to
rule out: two simultaneous debits both read the same stale balance, both
pass a sufficient-funds check, and the account goes negative. If the
balance is always recomputed from the entries, and the sufficiency check
runs inside the same transaction as the write, that interleaving cannot
happen.

`compute_balance` takes an optional `session`, and Phase 3 passes the
ledger transaction's session into it. That parameter is what ties the
sufficiency check to the write: read and write end up in the same
transaction, so the read is serialised against any concurrent commit
rather than racing it.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClientSession

from app.core import db
from app.models.account import BalanceResponse
from app.models.common import utc_now


class ComputedBalance(NamedTuple):
    """The aggregate figures for one account, all in minor units."""

    #: Sum of signed entry amounts. Credits positive, debits negative.
    balance_minor: int
    total_credited_minor: int
    total_debited_minor: int
    entry_count: int

    def to_response(self, account_id: ObjectId | str, currency: str) -> BalanceResponse:
        return BalanceResponse(
            account_id=str(account_id),
            currency=currency,
            balance_minor=self.balance_minor,
            total_credited_minor=self.total_credited_minor,
            total_debited_minor=self.total_debited_minor,
            entry_count=self.entry_count,
            computed_at=utc_now(),
        )


#: An account with no ledger entries has a balance of exactly zero, not a
#: missing balance. The aggregation returns no documents in that case, so
#: this is the value substituted.
ZERO_BALANCE = ComputedBalance(
    balance_minor=0,
    total_credited_minor=0,
    total_debited_minor=0,
    entry_count=0,
)


def _balance_pipeline(account_id: ObjectId) -> list[dict[str, Any]]:
    """Build the aggregation that derives one account's balance.

    `$match` is backed by the `(account_id, created_at)` index. The
    `balance_minor` sum is over the stored `signed_amount_minor`, whose
    sign the collection validator guarantees matches the entry's
    direction, so the sum needs no per-document conditional.

    The credited and debited subtotals are reported as well as the net,
    because a bare net figure hides whether an account has seen any
    activity at all, and the Phase 4 tests assert on the gross flows.
    """
    return [
        {"$match": {"account_id": account_id}},
        {
            "$group": {
                "_id": None,
                "balance_minor": {"$sum": "$signed_amount_minor"},
                "total_credited_minor": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$direction", "CREDIT"]},
                            "$amount_minor",
                            0,
                        ]
                    }
                },
                "total_debited_minor": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$direction", "DEBIT"]},
                            "$amount_minor",
                            0,
                        ]
                    }
                },
                "entry_count": {"$sum": 1},
            }
        },
    ]


async def compute_balance(
    account_id: ObjectId,
    *,
    session: AsyncIOMotorClientSession | None = None,
) -> ComputedBalance:
    """Compute an account's balance from its ledger entries.

    Pass `session` to run the aggregation inside an existing MongoDB
    transaction. Phase 3's sufficiency check depends on that: a balance
    read outside the transaction that writes could be invalidated by a
    concurrent commit between the read and the write.
    """
    cursor = db.ledger_entries_collection().aggregate(
        _balance_pipeline(account_id), session=session
    )
    rows = await cursor.to_list(length=1)

    if not rows:
        return ZERO_BALANCE

    row = rows[0]
    return ComputedBalance(
        balance_minor=int(row["balance_minor"]),
        total_credited_minor=int(row["total_credited_minor"]),
        total_debited_minor=int(row["total_debited_minor"]),
        entry_count=int(row["entry_count"]),
    )


async def get_available_balance_minor(
    account_id: ObjectId,
    *,
    session: AsyncIOMotorClientSession | None = None,
) -> int:
    """Return just the net balance. Convenience for the sufficiency check."""
    balance = await compute_balance(account_id, session=session)
    return balance.balance_minor
