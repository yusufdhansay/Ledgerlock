"""The atomic core. The only module that writes to `transactions` or
`ledger_entries` (RULES.md).

Read this file before anything else in the project. Everything PRD.md
claims rests on the ordering inside `_apply_transfer`.

--------------------------------------------------------------------
Why a MongoDB transaction alone is NOT enough
--------------------------------------------------------------------

The obvious implementation is: open a transaction, aggregate the source
account's ledger entries to get its balance, check the balance covers the
amount, insert the two entries, commit. Wrapping that in
`with_transaction` feels like it should be safe. It is not, and the reason
is worth being precise about, because it is the single most important
thing this project has to get right.

MongoDB transactions give snapshot isolation, and WiredTiger detects
conflicts between transactions that *write the same document*. The
sufficiency check is a read, and the writes are inserts of brand-new
ledger entry documents. Two concurrent transfers out of the same account
therefore touch no document in common:

    T1: read balance (100) -> 100 >= 60 ok -> insert entries -> commit
    T2: read balance (100) -> 100 >= 60 ok -> insert entries -> commit

Both snapshots were taken before either committed, both checks passed,
neither wrote a document the other wrote, so there is no conflict to
detect and both commit. Final balance: -20. That is write skew, and it is
exactly the overdraft bug PRD.md exists to rule out. A transaction
boundary on its own does not prevent it.

SQL would let you say `SELECT ... FOR UPDATE` and take a row lock.
MongoDB has no equivalent, which is precisely why PRD.md picked it: the
guarantee has to be constructed rather than borrowed.

--------------------------------------------------------------------
How this module actually prevents it
--------------------------------------------------------------------

Before reading the balance, the transaction performs a write to the
*source account document*: it increments `debit_serialisation_counter`.
That gives concurrent transfers out of the same account a document in
common to fight over. WiredTiger then aborts one of them with a
WriteConflict, which carries MongoDB's `TransientTransactionError` label,
which makes `with_transaction` retry the whole callback from the top. The
retry re-reads the balance, now including the winner's committed entries,
and either succeeds against the reduced balance or is correctly rejected
for insufficient funds.

So the counter is not data anybody reads. It is a serialisation point,
deliberately introduced so that the sufficiency check and the write it
guards cannot interleave. It is emphatically not a cached balance: it
counts debit attempts, it is never used to answer a balance query, and
nothing breaks if it is wrong.

The lock is taken only when the source account is a USER account, because
its only job is to serialise the overdraft check. SYSTEM boundary
accounts have no overdraft check to serialise, so funding operations skip
it. Without that exemption every funding request for a currency would
contend on one document and serialise the whole system.

Only the source is locked, never the destination. Locking the debited
account is sufficient: a credit cannot push an account below zero, so
there is no constraint on the destination to protect. One honest
consequence, spelled out because it is a real behaviour and not a bug: an
in-flight debit will not see a credit that commits after its snapshot was
taken, so a transfer can be rejected as INSUFFICIENT_FUNDS even though a
payment arriving at the same instant would have covered it. That is a
conservative failure. It never permits an overdraft, and the client
retries. Trading a possible spurious rejection for a guaranteed absence
of overdraft is the right way round for a ledger.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClientSession
from pymongo.errors import DuplicateKeyError, OperationFailure

from app.core import db
from app.core.errors import (
    AccountNotActiveError,
    AccountNotFoundError,
    CurrencyMismatchError,
    DuplicateSubmissionError,
    InsufficientFundsError,
    SameAccountTransferError,
    WriteConflictError,
)
from app.core.logging_config import get_logger
from app.models.account import AccountStatus, AccountType
from app.models.ledger_entry import EntryDirection, LedgerEntryDocument
from app.models.transaction import (
    TransactionDocument,
    TransactionKind,
    TransactionStatus,
)
from app.services import balance_service

logger = get_logger(__name__)

#: Field incremented on the source account inside the transaction purely to
#: create a write-conflict point. See the module docstring. Named for what
#: it does so it cannot be mistaken for a balance or a useful statistic.
DEBIT_SERIALISATION_FIELD = "debit_serialisation_counter"

#: MongoDB error label attached to failures that are safe to retry from the
#: start of the transaction, including WriteConflict.
TRANSIENT_LABEL = "TransientTransactionError"


class LedgerWriteResult(NamedTuple):
    """The committed transaction, plus how hard it was to commit."""

    transaction_document: dict[str, Any]
    debit_entry_id: ObjectId
    credit_entry_id: ObjectId
    #: How many times the transaction callback ran. Greater than 1 means
    #: contention on the source account forced a retry. Surfaced so the
    #: Phase 6 load test can report real contention rather than guess at it.
    attempts: int


class _Attempt:
    """Mutable counter shared with the retried callback."""

    def __init__(self) -> None:
        self.count = 0


def _validate_participants(
    *,
    source: dict[str, Any] | None,
    destination: dict[str, Any] | None,
    source_account_id: ObjectId,
    destination_account_id: ObjectId,
    currency: str,
) -> None:
    """Check both accounts exist, are usable, and agree on currency.

    Runs inside the transaction so that the state it validates is the same
    state the write will be applied to.
    """
    if source is None:
        raise AccountNotFoundError(
            "Source account not found.",
            context={"account_id": str(source_account_id), "side": "source"},
        )
    if destination is None:
        raise AccountNotFoundError(
            "Destination account not found.",
            context={
                "account_id": str(destination_account_id),
                "side": "destination",
            },
        )

    for side, account in (("source", source), ("destination", destination)):
        if account["status"] != AccountStatus.ACTIVE.value:
            raise AccountNotActiveError(
                f"The {side} account has status {account['status']} and cannot "
                "take part in a transaction.",
                context={
                    "account_id": str(account["_id"]),
                    "side": side,
                    "status": account["status"],
                },
            )

    # All three must agree. The client sends the currency explicitly so a
    # client/server disagreement about an account's currency fails loudly
    # instead of moving the wrong amount.
    if source["currency"] != currency or destination["currency"] != currency:
        raise CurrencyMismatchError(
            context={
                "requested_currency": currency,
                "source_currency": source["currency"],
                "destination_currency": destination["currency"],
            }
        )


async def _apply_transfer(
    session: AsyncIOMotorClientSession,
    *,
    attempt: _Attempt,
    idempotency_key: str,
    source_account_id: ObjectId,
    destination_account_id: ObjectId,
    amount_minor: int,
    currency: str,
    description: str,
    kind: TransactionKind,
    submitted_by: ObjectId | None,
) -> LedgerWriteResult:
    """The transaction body. Re-run from the top on a transient error.

    Every step below runs in one MongoDB transaction. If any of them fails,
    all of them roll back, so there is no state in which a debit entry
    exists without its matching credit entry.
    """
    attempt.count += 1

    transactions = db.transactions_collection()
    ledger_entries = db.ledger_entries_collection()
    accounts = db.accounts_collection()

    # --- 1. Idempotency check ----------------------------------------
    # This read catches the common case cheaply: a client retrying a
    # request whose original already committed. It does NOT carry the
    # guarantee on its own, because two genuinely simultaneous submissions
    # both read "not seen" before either inserts. The unique index on
    # `idempotency_key` is what closes that window, at step 5.
    already_applied = await transactions.find_one(
        {"idempotency_key": idempotency_key},
        projection={"_id": 1},
        session=session,
    )
    if already_applied is not None:
        raise DuplicateSubmissionError(
            context={
                "idempotency_key": idempotency_key,
                "existing_transaction_id": str(already_applied["_id"]),
                "detected_by": "pre_check",
            }
        )

    # --- 2. Load and validate both accounts --------------------------
    source = await accounts.find_one({"_id": source_account_id}, session=session)
    destination = await accounts.find_one(
        {"_id": destination_account_id}, session=session
    )
    _validate_participants(
        source=source,
        destination=destination,
        source_account_id=source_account_id,
        destination_account_id=destination_account_id,
        currency=currency,
    )
    assert source is not None and destination is not None  # noqa: S101

    # --- 3. Serialise, then check sufficiency ------------------------
    # The order here is the whole point. The write on the next line is what
    # makes two concurrent debits of this account conflict, so that the
    # balance read after it cannot be stale with respect to a commit that
    # is racing us. See the module docstring for why a transaction boundary
    # alone does not achieve this.
    source_is_user_account = source["account_type"] == AccountType.USER.value

    if source_is_user_account:
        await accounts.update_one(
            {"_id": source_account_id},
            {"$inc": {DEBIT_SERIALISATION_FIELD: 1}},
            session=session,
        )

        balance = await balance_service.compute_balance(
            source_account_id, session=session
        )
        if balance.balance_minor < amount_minor:
            raise InsufficientFundsError(
                context={
                    "account_id": str(source_account_id),
                    "balance_minor": balance.balance_minor,
                    "requested_minor": amount_minor,
                    "shortfall_minor": amount_minor - balance.balance_minor,
                },
            )
    # SYSTEM source: no sufficiency check, so nothing to serialise. Its
    # negative balance is the intended representation of value that has
    # entered the ledger from outside.

    # --- 4 & 5. Write the transaction header, then both entries ------
    transaction_id = ObjectId()
    transaction = TransactionDocument(
        idempotency_key=idempotency_key,
        kind=kind,
        status=TransactionStatus.COMPLETED,
        source_account_id=source_account_id,
        destination_account_id=destination_account_id,
        amount_minor=amount_minor,
        currency=currency,
        description=description,
        submitted_by=submitted_by,
    )
    transaction_document = transaction.to_bson()
    transaction_document["_id"] = transaction_id

    # If a concurrent submission using the same idempotency key committed
    # between step 1 and here, this insert raises DuplicateKeyError and the
    # whole transaction aborts, taking the ledger entries below with it.
    await transactions.insert_one(transaction_document, session=session)

    debit_entry = LedgerEntryDocument.create(
        transaction_id=transaction_id,
        account_id=source_account_id,
        direction=EntryDirection.DEBIT,
        amount_minor=amount_minor,
        currency=currency,
    )
    credit_entry = LedgerEntryDocument.create(
        transaction_id=transaction_id,
        account_id=destination_account_id,
        direction=EntryDirection.CREDIT,
        amount_minor=amount_minor,
        currency=currency,
    )

    # Both entries in one call, inside the same transaction: there is no
    # point at which one exists without the other, even transiently, and
    # no partial state to observe if this aborts.
    insert_result = await ledger_entries.insert_many(
        [debit_entry.to_bson(), credit_entry.to_bson()],
        session=session,
        ordered=True,
    )
    debit_entry_id, credit_entry_id = insert_result.inserted_ids

    return LedgerWriteResult(
        transaction_document=transaction_document,
        debit_entry_id=debit_entry_id,
        credit_entry_id=credit_entry_id,
        attempts=attempt.count,
    )


async def create_transfer(
    *,
    idempotency_key: str,
    source_account_id: ObjectId,
    destination_account_id: ObjectId,
    amount_minor: int,
    currency: str,
    description: str = "",
    kind: TransactionKind = TransactionKind.TRANSFER,
    submitted_by: ObjectId | None = None,
) -> LedgerWriteResult:
    """Move money between two accounts, atomically, exactly once.

    On success, exactly one transaction document and exactly two ledger
    entries exist: a DEBIT on the source and a CREDIT on the destination,
    of equal amount, both referencing the transaction's id.

    Raises, and writes nothing at all:
      DuplicateSubmissionError  the idempotency key was already used
      InsufficientFundsError    the source cannot cover the amount
      AccountNotFoundError      either account is missing
      AccountNotActiveError     either account is FROZEN or CLOSED
      CurrencyMismatchError     the three currencies do not agree
      SameAccountTransferError  source and destination are the same
      WriteConflictError        contention could not be resolved by retrying
    """
    if source_account_id == destination_account_id:
        # Checked before opening a transaction: a self-transfer would write
        # a debit and a credit to the same account for the same amount,
        # netting to zero while still consuming an idempotency key. Cheap to
        # reject up front.
        raise SameAccountTransferError(context={"account_id": str(source_account_id)})

    attempt = _Attempt()
    client = db.get_client()

    async with await client.start_session() as session:

        async def callback(
            active_session: AsyncIOMotorClientSession,
        ) -> LedgerWriteResult:
            return await _apply_transfer(
                active_session,
                attempt=attempt,
                idempotency_key=idempotency_key,
                source_account_id=source_account_id,
                destination_account_id=destination_account_id,
                amount_minor=amount_minor,
                currency=currency,
                description=description,
                kind=kind,
                submitted_by=submitted_by,
            )

        try:
            # with_transaction handles commit, abort, and retry-on-transient
            # for us. A WriteConflict from the serialisation write in step 3
            # arrives labelled TransientTransactionError, so the callback is
            # re-run from the top against fresh state.
            result: LedgerWriteResult = await session.with_transaction(callback)

        except DuplicateKeyError as exc:
            # The only unique index a transfer can violate at this point is
            # `uq_idempotency_key`: a concurrent submission with the same key
            # committed between our pre-check and our insert. Its entries
            # were written, ours were rolled back, and the transfer has been
            # applied exactly once, which is the guarantee.
            logger.warning(
                "transaction_rejected_duplicate_key",
                extra={
                    "idempotency_key": idempotency_key,
                    "detected_by": "unique_index",
                    "attempts": attempt.count,
                },
            )
            raise DuplicateSubmissionError(
                context={
                    "idempotency_key": idempotency_key,
                    "detected_by": "unique_index",
                }
            ) from exc

        except OperationFailure as exc:
            if exc.has_error_label(TRANSIENT_LABEL):
                # with_transaction retries transient failures until its
                # deadline; reaching here means contention outlasted it.
                logger.error(
                    "transaction_write_conflict_exhausted",
                    extra={
                        "source_account_id": str(source_account_id),
                        "attempts": attempt.count,
                    },
                )
                raise WriteConflictError(
                    context={
                        "source_account_id": str(source_account_id),
                        "attempts": attempt.count,
                    }
                ) from exc
            raise

    logger.info(
        "transaction_committed",
        extra={
            "transaction_id": str(result.transaction_document["_id"]),
            "kind": str(kind),
            "source_account_id": str(source_account_id),
            "destination_account_id": str(destination_account_id),
            "amount_minor": amount_minor,
            "currency": currency,
            "attempts": result.attempts,
        },
    )
    return result


async def get_transaction(transaction_id: ObjectId) -> dict[str, Any] | None:
    """Read one transaction document. There is no update or delete."""
    return await db.transactions_collection().find_one({"_id": transaction_id})


async def get_entries_for_transaction(
    transaction_id: ObjectId,
) -> list[dict[str, Any]]:
    """Read the ledger entries a transaction produced. Always exactly two."""
    cursor = db.ledger_entries_collection().find({"transaction_id": transaction_id})
    return await cursor.to_list(length=2)
