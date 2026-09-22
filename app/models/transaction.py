"""Transaction models.

A transaction document is the *header*: who paid whom, how much, under
which idempotency key. The two ledger entries it produces are the actual
bookkeeping record. The header exists so that (a) an idempotency key has
somewhere to live under a unique index, and (b) the two entries have a
shared identifier proving they belong to the same movement of money.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field

from app.models.common import (
    AmountMinor,
    CurrencyCode,
    IdempotencyKey,
    MongoDocument,
    ObjectIdStr,
    utc_now,
)


class TransactionStatus(StrEnum):
    """Status reported on every transaction response (DESIGN.md).

    Honest note on which of these the MVP actually produces: because the
    whole write is wrapped in one MongoDB transaction, a submission either
    commits fully as COMPLETED or rolls back leaving no document at all.
    So COMPLETED is the only value written today. PENDING, FAILED and
    REVERSED are part of the contract because a real ledger needs them
    (asynchronous settlement, and reversal by compensating entry rather
    than by mutating history) and callers should be built to read the field
    rather than infer success from the HTTP status code.
    """

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REVERSED = "REVERSED"


class TransactionKind(StrEnum):
    """What sort of movement this is.

    TRANSFER: value moving between two USER accounts.
    FUNDING:  value entering the ledger from the outside world, debiting
              the SYSTEM boundary account and crediting a USER account.
    """

    TRANSFER = "TRANSFER"
    FUNDING = "FUNDING"


TransactionDescription = Annotated[
    str,
    Field(
        max_length=280,
        description="Free-text reference supplied by the client.",
        examples=["Invoice 2291"],
    ),
]


# ---------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------


class TransferCreateRequest(BaseModel):
    """Payload for POST /transactions.

    The `Idempotency-Key` travels as a header, not a body field, so that
    it is visible to infrastructure (proxies, logs) and cannot be confused
    with business data.
    """

    model_config = ConfigDict(extra="forbid")

    source_account_id: ObjectIdStr = Field(
        description="Account to debit. Must be owned by the authenticated user."
    )
    destination_account_id: ObjectIdStr = Field(description="Account to credit.")
    amount_minor: AmountMinor
    currency: CurrencyCode = Field(
        description=(
            "Must match both accounts' currency. Sent explicitly so a "
            "client/server disagreement about an account's currency fails "
            "loudly rather than moving the wrong amount."
        )
    )
    description: TransactionDescription = Field(default="")


class FundingCreateRequest(BaseModel):
    """Payload for POST /accounts/{account_id}/funding.

    Stands in for an external deposit rail. In a production system this
    would be driven by a settlement webhook from a payment provider, not
    by the account holder; PRD.md puts real payment rails out of scope, so
    this endpoint is the seam where that integration would attach.
    """

    model_config = ConfigDict(extra="forbid")

    amount_minor: AmountMinor
    currency: CurrencyCode
    description: TransactionDescription = Field(default="External funding")


# ---------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------


class TransactionPublic(BaseModel):
    """A transaction as exposed by the API."""

    id: str
    status: TransactionStatus = Field(
        description="Always present and explicit; never inferred from HTTP status."
    )
    kind: TransactionKind
    source_account_id: str
    destination_account_id: str
    amount_minor: int
    currency: CurrencyCode
    description: str
    idempotency_key: str
    created_at: datetime

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> TransactionPublic:
        return cls(
            id=str(document["_id"]),
            status=document["status"],
            kind=document["kind"],
            source_account_id=str(document["source_account_id"]),
            destination_account_id=str(document["destination_account_id"]),
            amount_minor=document["amount_minor"],
            currency=document["currency"],
            description=document.get("description", ""),
            idempotency_key=document["idempotency_key"],
            created_at=document["created_at"],
        )


# ---------------------------------------------------------------------
# Stored documents
# ---------------------------------------------------------------------


class TransactionDocument(MongoDocument):
    """The `transactions` collection document.

    `idempotency_key` carries a unique index. That index, not an
    application-level "have I seen this key?" check, is what makes
    idempotency hold under concurrency: two simultaneous submissions of the
    same key both pass any read-time check, but only one can win the
    insert, and the loser's entire transaction (including its ledger
    entries) aborts.
    """

    id: ObjectId | None = Field(default=None, alias="_id")
    idempotency_key: IdempotencyKey
    kind: TransactionKind
    status: TransactionStatus = Field(default=TransactionStatus.COMPLETED)
    source_account_id: ObjectId
    destination_account_id: ObjectId
    amount_minor: int = Field(gt=0)
    currency: CurrencyCode
    description: str = Field(default="", max_length=280)
    #: The user who submitted the request. Retained for audit, and so a
    #: funding transaction can be attributed even though its source is a
    #: SYSTEM account with no owner.
    submitted_by: ObjectId | None = Field(default=None)
    created_at: datetime = Field(default_factory=utc_now)
