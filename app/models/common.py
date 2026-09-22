"""Shared field types and helpers used by every model.

Kept in one place so the ObjectId adapter, the money type, and the
currency type cannot drift between the four model modules.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _require_valid_object_id(value: str) -> str:
    """Reject anything that is not a well-formed 24-hex-character ObjectId.

    This matters for more than tidiness. Every account/transaction
    identifier that arrives from a client is interpolated into a MongoDB
    filter document. Constraining it to a validated ObjectId string means
    a client cannot smuggle a dict like ``{"$ne": null}`` or a ``$where``
    expression into a query position, which is MongoDB's analogue of SQL
    injection (see RULES.md).
    """
    if not ObjectId.is_valid(value):
        raise ValueError("must be a 24-character hexadecimal ObjectId")
    return value


ObjectIdStr = Annotated[
    str,
    AfterValidator(_require_valid_object_id),
    Field(examples=["665f1b2c8a4e5f0012ab34cd"]),
]

#: Money is always an integer count of minor units (cents, paise, pence).
#: Never a float: binary floating point cannot represent 0.01 exactly, and
#: rounding drift in a ledger is a correctness bug, not a display bug.
#: Amounts on a transaction request must be strictly positive; direction
#: is expressed by which account is debited, not by the sign.
AmountMinor = Annotated[
    int,
    Field(
        # strict=True is load-bearing, not tidiness. Pydantic's default lax
        # coercion accepts `True` as the integer 1 (bool subclasses int in
        # Python), the string "100" as 100, and the float 10.0 as 10. A
        # transfer of `true` minor units silently becoming a transfer of 1 is
        # exactly the class of quiet wrongness a ledger must not permit, so
        # every one of those is rejected instead. Found by the Phase 8
        # injection tests, which submitted `True` as an amount and watched it
        # reach the sufficiency check as 1.
        strict=True,
        gt=0,
        le=1_000_000_000_000,
        description=(
            "Amount in minor units (e.g. cents). Must be a positive integer. "
            "Strictly typed: booleans, numeric strings and floats are "
            "rejected rather than coerced."
        ),
        examples=[2500],
    ),
]

#: Signed minor units, used for ledger entry values and computed balances.
#: A credit is positive, a debit is negative, so summing signed values is
#: both how a balance is computed and how reconciliation-to-zero is checked.
SignedAmountMinor = Annotated[int, Field(examples=[-2500])]

CurrencyCode = Annotated[
    str,
    Field(
        pattern=r"^[A-Z]{3}$",
        description="ISO 4217 alphabetic currency code, uppercase.",
        examples=["USD"],
    ),
]

IdempotencyKey = Annotated[
    str,
    Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
        description=(
            "Client-generated key that makes transaction submission safe to "
            "retry. Restricted to URL-safe characters."
        ),
        examples=["order-1f4c9b2e-7a10-4a3f-9d2b-5c8e1f0a6b33"],
    ),
]


def utc_now() -> datetime:
    """Current time, timezone-aware UTC.

    Always UTC, always tz-aware (DESIGN.md). MongoDB stores datetimes as
    UTC milliseconds regardless, so writing naive local times would mean
    silently shifting every timestamp by the host's offset.
    """
    return datetime.now(UTC)


def to_object_id(value: str) -> ObjectId:
    """Convert an already-validated id string to an ObjectId."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as exc:  # pragma: no cover - guarded upstream
        raise ValueError(f"invalid ObjectId: {value!r}") from exc


class MongoDocument(BaseModel):
    """Base class for models that mirror a stored MongoDB document.

    These are the internal representation, not the API surface. Response
    models are declared separately so that changing a stored field never
    silently changes the public contract.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,  # ObjectId is not a pydantic-native type
        populate_by_name=True,
        extra="forbid",
    )

    def to_bson(self, *, include_id: bool = False) -> dict[str, Any]:
        """Render as a dict suitable for an insert.

        ``_id`` is omitted by default so MongoDB assigns it, which keeps id
        generation in one place.
        """
        document = self.model_dump(by_alias=True, exclude_none=False)
        if not include_id:
            document.pop("_id", None)
        return document


class PageMeta(BaseModel):
    """Pagination envelope. No list endpoint returns an unbounded set."""

    limit: int = Field(ge=1, le=200, examples=[50])
    offset: int = Field(ge=0, examples=[0])
    returned: int = Field(ge=0, examples=[50])
