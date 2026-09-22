"""Transaction routes.

Thin by design (DESIGN.md): pull the idempotency key off the header,
resolve and authorise the source account, hand everything to
`ledger_service`, shape the response. All the interesting ordering lives
in `ledger_service`, which is the only module permitted to write to
`transactions` or `ledger_entries`.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import TypeAdapter, ValidationError

from app.core.errors import (
    ForbiddenError,
    IdempotencyKeyRequiredError,
    MalformedIdempotencyKeyError,
    TransactionNotFoundError,
)
from app.core.logging_config import get_logger
from app.core.rate_limit import (
    default_limit,
    limiter,
    rate_limiting_is_disabled,
    transaction_limit,
)
from app.core.security import AuthenticatedUser, get_current_user
from app.models.common import IdempotencyKey, ObjectIdStr, to_object_id
from app.models.transaction import (
    TransactionPublic,
    TransferCreateRequest,
)
from app.services import account_service, ledger_service

logger = get_logger(__name__)

router = APIRouter(prefix="/transactions", tags=["transactions"])

_idempotency_key_adapter = TypeAdapter(IdempotencyKey)

IDEMPOTENCY_KEY_DESCRIPTION = (
    "**Required.** A client-generated key that makes this request safe to "
    "retry. Submitting the same key twice returns 409 DUPLICATE_SUBMISSION "
    "and does not apply the transfer a second time. Use a fresh UUID per "
    "logical transfer, and reuse it (unchanged) when retrying that same "
    "transfer after a network failure. 8-128 characters from "
    "[A-Za-z0-9_.:-]."
)


async def require_idempotency_key(
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            description=IDEMPOTENCY_KEY_DESCRIPTION,
        ),
    ] = None,
) -> str:
    """Extract and validate the `Idempotency-Key` header.

    Absence is its own error code rather than a generic validation failure,
    because "you forgot the header" and "your key is the wrong shape" call
    for different fixes on the client side.
    """
    if idempotency_key is None or not idempotency_key.strip():
        raise IdempotencyKeyRequiredError()

    try:
        return _idempotency_key_adapter.validate_python(idempotency_key.strip())
    except ValidationError as exc:
        raise MalformedIdempotencyKeyError(
            context={"reason": "failed_pattern_or_length_check"}
        ) from exc


@router.post(
    "",
    response_model=TransactionPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Transfer money between two accounts",
    responses={
        400: {"description": "IDEMPOTENCY_KEY_REQUIRED"},
        401: {"description": "UNAUTHENTICATED"},
        403: {"description": "FORBIDDEN (caller does not own the source account)"},
        404: {"description": "ACCOUNT_NOT_FOUND"},
        409: {
            "description": "DUPLICATE_SUBMISSION / ACCOUNT_NOT_ACTIVE / WRITE_CONFLICT"
        },
        422: {
            "description": (
                "MALFORMED_REQUEST / INSUFFICIENT_FUNDS / CURRENCY_MISMATCH / "
                "SAME_ACCOUNT_TRANSFER / MALFORMED_IDEMPOTENCY_KEY"
            )
        },
        429: {"description": "RATE_LIMITED"},
    },
)
@limiter.limit(transaction_limit, exempt_when=rate_limiting_is_disabled)
async def create_transaction(
    request: Request,
    payload: TransferCreateRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    user: AuthenticatedUser = Depends(get_current_user),
) -> TransactionPublic:
    """Move money from one account to another.

    Requires an `Idempotency-Key` header. The caller must own the source
    account; the destination may belong to anyone.

    The write produces exactly two immutable ledger entries, a DEBIT on the
    source and a CREDIT on the destination, written in a single MongoDB
    transaction along with the transaction document itself. Either all
    three exist or none do.

    A transfer that would take the source account below zero is rejected
    with `INSUFFICIENT_FUNDS`, and that holds under concurrent submission,
    not only when requests arrive one at a time.

    Resubmitting a used `Idempotency-Key` returns `409
    DUPLICATE_SUBMISSION`. Note that this rejects rather than replaying the
    original response: the guarantee is that the transfer was applied
    exactly once, not that a duplicate request gets a copy of the first
    reply.
    """
    # Ownership of the source is checked here, not in the service, because
    # it is an authorisation question about the caller rather than a
    # bookkeeping invariant. The source must be owned; a SYSTEM account has
    # no owner, so this also blocks any attempt to spend from the ledger
    # boundary directly.
    source = await account_service.get_account_or_raise(payload.source_account_id)
    if source.get("owner_id") != user.id:
        raise ForbiddenError(
            "You do not own the source account.",
            context={
                "account_id": payload.source_account_id,
                "user_id": str(user.id),
            },
        )

    # Confirm the destination exists before opening a transaction, so the
    # common typo case gets a clean 404 rather than consuming retry budget.
    await account_service.get_account_or_raise(payload.destination_account_id)

    result = await ledger_service.create_transfer(
        idempotency_key=idempotency_key,
        source_account_id=to_object_id(payload.source_account_id),
        destination_account_id=to_object_id(payload.destination_account_id),
        amount_minor=payload.amount_minor,
        currency=payload.currency,
        description=payload.description,
        submitted_by=user.id,
    )
    return TransactionPublic.from_document(result.transaction_document)


@router.get(
    "/{transaction_id}",
    response_model=TransactionPublic,
    status_code=status.HTTP_200_OK,
    summary="Read one transaction",
    responses={
        401: {"description": "UNAUTHENTICATED"},
        404: {"description": "NOT_FOUND"},
        429: {"description": "RATE_LIMITED"},
    },
)
@limiter.limit(default_limit, exempt_when=rate_limiting_is_disabled)
async def read_transaction(
    request: Request,
    transaction_id: ObjectIdStr,
    user: AuthenticatedUser = Depends(get_current_user),
) -> TransactionPublic:
    """Read a transaction the caller took part in.

    Exists so that a client which received `409 DUPLICATE_SUBMISSION`, or
    lost the response to its original request, can find out what actually
    happened. Visible to the owner of either account involved; a caller who
    was on neither side gets a 404 rather than a 403, so this route cannot
    be used to enumerate other people's transaction ids.
    """
    document = await ledger_service.get_transaction(to_object_id(transaction_id))

    if document is not None:
        owned_account_ids = {
            account["_id"]
            for account in await account_service.list_accounts_for_owner(
                user.id, limit=account_service.MAX_PAGE_SIZE
            )
        }
        accounts_involved = {
            document["source_account_id"],
            document["destination_account_id"],
        }
        if owned_account_ids & accounts_involved:
            return TransactionPublic.from_document(document)

    raise TransactionNotFoundError(context={"transaction_id": transaction_id})
