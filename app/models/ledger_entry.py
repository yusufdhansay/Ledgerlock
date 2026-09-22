"""Ledger entry models: the immutable bookkeeping record.

Every transaction produces exactly two of these, a DEBIT and a CREDIT of
equal magnitude, written in the same MongoDB transaction. Nothing in this
application ever updates or deletes one.

Sign convention, stated once and relied on everywhere:

    CREDIT -> signed_amount_minor = +amount_minor   (value into account)
    DEBIT  -> signed_amount_minor = -amount_minor   (value out of account)

Two useful properties follow directly:

    balance(account)  = sum(signed_amount_minor where account_id = account)
    reconciliation    = sum(signed_amount_minor over every entry) == 0

The second holds because each transaction contributes exactly +X and -X.
That is the whole reconciliation invariant, and it is checkable with a
single aggregation over the collection.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from bson import ObjectId
from pydantic import BaseModel, Field, model_validator

from app.models.common import CurrencyCode, MongoDocument, utc_now


class EntryDirection(StrEnum):
    """Which side of the double entry this is."""

    #: Value leaving the account. Decreases the account's balance.
    DEBIT = "DEBIT"
    #: Value arriving in the account. Increases the account's balance.
    CREDIT = "CREDIT"


def signed_amount_for(direction: EntryDirection, amount_minor: int) -> int:
    """Return the signed value of an entry under the convention above."""
    if amount_minor <= 0:
        raise ValueError("amount_minor must be a positive integer")
    return amount_minor if direction is EntryDirection.CREDIT else -amount_minor


# ---------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------


class LedgerEntryPublic(BaseModel):
    """A ledger entry as exposed by the API. Read-only: there is no
    corresponding update or delete request model, because no such
    operation exists."""

    id: str
    transaction_id: str
    account_id: str
    direction: EntryDirection
    amount_minor: int
    signed_amount_minor: int
    currency: CurrencyCode
    created_at: datetime

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> LedgerEntryPublic:
        return cls(
            id=str(document["_id"]),
            transaction_id=str(document["transaction_id"]),
            account_id=str(document["account_id"]),
            direction=document["direction"],
            amount_minor=document["amount_minor"],
            signed_amount_minor=document["signed_amount_minor"],
            currency=document["currency"],
            created_at=document["created_at"],
        )


class ReconciliationReport(BaseModel):
    """Result of the system-wide sum-to-zero check.

    `net_signed_minor` is the number that matters. Anything other than
    exactly 0 means the ledger is broken: some transaction wrote one side
    of its pair without the other, which the atomic write is designed to
    make impossible.
    """

    balanced: bool = Field(
        description="True if and only if net_signed_minor == 0 for every currency."
    )
    net_signed_minor: int = Field(
        description="Sum of signed_amount_minor across every ledger entry. Must be 0."
    )
    total_entries: int = Field(ge=0)
    total_transactions: int = Field(ge=0)
    orphaned_entries: int = Field(
        ge=0,
        description=(
            "Entries whose transaction_id has no matching transaction "
            "document, or whose transaction does not have exactly one debit "
            "and one credit. Must be 0."
        ),
    )
    per_currency_net_minor: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Net per currency. A global net of zero could in principle hide "
            "two offsetting errors in different currencies, so the check is "
            "also made per currency."
        ),
    )
    checked_at: datetime


# ---------------------------------------------------------------------
# Stored documents
# ---------------------------------------------------------------------


class LedgerEntryDocument(MongoDocument):
    """The `ledger_entries` collection document.

    `signed_amount_minor` is stored rather than derived at query time.
    That is a deliberate exception to the "no redundant stored values"
    stance taken for balances, and the reasoning is different: this value
    is written once, inside the same atomic write as the entry itself, and
    never updated, so it cannot drift the way a mutable running balance
    can. Storing it lets balance and reconciliation aggregations run as a
    plain indexed `$group` sum instead of a `$cond` over every document.
    The invariant is enforced here in the model and again by the
    collection's `$jsonSchema` validator.
    """

    id: ObjectId | None = Field(default=None, alias="_id")
    transaction_id: ObjectId
    account_id: ObjectId
    direction: EntryDirection
    amount_minor: int = Field(gt=0, description="Magnitude, always positive.")
    signed_amount_minor: int = Field(
        description="+amount_minor for CREDIT, -amount_minor for DEBIT."
    )
    currency: CurrencyCode
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def sign_must_match_direction(self) -> LedgerEntryDocument:
        expected = signed_amount_for(self.direction, self.amount_minor)
        if self.signed_amount_minor != expected:
            raise ValueError(
                f"signed_amount_minor {self.signed_amount_minor} does not match "
                f"direction {self.direction} with amount {self.amount_minor} "
                f"(expected {expected})"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        transaction_id: ObjectId,
        account_id: ObjectId,
        direction: EntryDirection,
        amount_minor: int,
        currency: str,
        created_at: datetime | None = None,
    ) -> LedgerEntryDocument:
        """Build an entry with its signed amount derived, not passed in.

        Callers cannot get the sign wrong because they never supply it.
        """
        return cls(
            transaction_id=transaction_id,
            account_id=account_id,
            direction=direction,
            amount_minor=amount_minor,
            signed_amount_minor=signed_amount_for(direction, amount_minor),
            currency=currency,
            created_at=created_at or utc_now(),
        )
