"""Account models.

Note what is *absent*: there is no `balance` field on the account
document, and there never will be (RULES.md). A stored balance is a
second source of truth that has to be kept in step with the ledger under
concurrency, and keeping it in step is exactly the problem this project
exists to avoid. Balance is derived by aggregating `ledger_entries`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field

from app.models.common import CurrencyCode, MongoDocument, utc_now


class AccountStatus(StrEnum):
    """Lifecycle state of an account (PRD.md core feature 1)."""

    ACTIVE = "ACTIVE"
    #: Can be read, cannot take part in a transaction on either side.
    FROZEN = "FROZEN"
    #: Terminal. Cannot take part in a transaction.
    CLOSED = "CLOSED"


class AccountType(StrEnum):
    """Whether an account represents a real holder or the ledger boundary.

    Double-entry bookkeeping has a structural consequence that is easy to
    miss: if every transaction must net to zero, and every account starts
    at zero, then no account can ever hold a positive balance unless some
    account is permitted to go negative. Money has to enter the ledger
    from somewhere.

    `SYSTEM` accounts are that somewhere. They represent value entering or
    leaving the ledger's boundary (in a production system, the other side
    of a bank settlement or card capture). A SYSTEM account is exempt from
    the overdraft check, and its negative balance is a meaningful figure:
    it is the total value currently held across all USER accounts in that
    currency.

    `USER` accounts are held by a registered user and are never allowed to
    go negative. This is the guarantee the concurrency tests prove.
    """

    USER = "USER"
    SYSTEM = "SYSTEM"


AccountLabel = Annotated[
    str,
    Field(
        min_length=1,
        max_length=120,
        description="Human-readable name for the account.",
        examples=["Everyday spending"],
    ),
]


# ---------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------


class AccountCreateRequest(BaseModel):
    """Payload for POST /accounts.

    `account_type` is deliberately not accepted from clients: allowing a
    caller to create a SYSTEM account would let them mint money, since
    SYSTEM accounts skip the overdraft check. SYSTEM accounts are
    provisioned by the application at startup, one per supported currency.
    """

    model_config = ConfigDict(extra="forbid")

    currency: CurrencyCode
    label: AccountLabel = Field(default="Primary account")


# ---------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------


class AccountPublic(BaseModel):
    """An account as exposed by the API. Note: no balance field.

    Balance is a separate resource (`GET /accounts/{id}/balance`) because
    it is a computed aggregate, not an attribute of the account. Folding it
    in here would invite callers to treat it as stored state.
    """

    id: str
    owner_id: str
    currency: CurrencyCode
    status: AccountStatus
    account_type: AccountType
    label: str
    created_at: datetime

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> AccountPublic:
        return cls(
            id=str(document["_id"]),
            owner_id=str(document["owner_id"]),
            currency=document["currency"],
            status=document["status"],
            account_type=document["account_type"],
            label=document["label"],
            created_at=document["created_at"],
        )


class BalanceResponse(BaseModel):
    """Result of GET /accounts/{id}/balance.

    Includes the currency, never a bare number (DESIGN.md), and reports
    how many entries the figure was derived from so a caller can see that
    it is computed rather than stored.
    """

    account_id: str
    currency: CurrencyCode
    balance_minor: int = Field(
        description=(
            "Computed balance in minor units. Sum of signed ledger entry "
            "amounts for this account: credits positive, debits negative."
        ),
        examples=[7500],
    )
    total_credited_minor: int = Field(ge=0, examples=[10000])
    total_debited_minor: int = Field(ge=0, examples=[2500])
    entry_count: int = Field(
        ge=0,
        description="Number of ledger entries aggregated to produce this balance.",
        examples=[4],
    )
    computed_at: datetime


# ---------------------------------------------------------------------
# Stored documents
# ---------------------------------------------------------------------


class AccountDocument(MongoDocument):
    """The `accounts` collection document. Contains no balance field."""

    id: ObjectId | None = Field(default=None, alias="_id")
    owner_id: ObjectId
    currency: CurrencyCode
    status: AccountStatus = Field(default=AccountStatus.ACTIVE)
    account_type: AccountType = Field(default=AccountType.USER)
    label: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
