"""Account routes: create, list, read, and read a computed balance.

Note what is missing and will stay missing: there is no endpoint that
writes a balance, and no endpoint that mutates a ledger entry. Balance is
a read-only projection of the ledger.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.errors import UnsupportedCurrencyError
from app.core.logging_config import get_logger
from app.core.rate_limit import (
    default_limit,
    limiter,
    rate_limiting_is_disabled,
    transaction_limit,
)
from app.core.security import AuthenticatedUser, get_current_user
from app.models.account import (
    AccountCreateRequest,
    AccountPublic,
    BalanceResponse,
)
from app.models.common import ObjectIdStr, PageMeta
from app.models.transaction import (
    FundingCreateRequest,
    TransactionKind,
    TransactionPublic,
)
from app.routes.transactions import require_idempotency_key
from app.services import account_service, balance_service, ledger_service

logger = get_logger(__name__)

router = APIRouter(prefix="/accounts", tags=["accounts"])


class AccountListResponse(BaseModel):
    """Paginated list of accounts. No unbounded result sets (DESIGN.md)."""

    accounts: list[AccountPublic]
    page: PageMeta


@router.post(
    "",
    response_model=AccountPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Open a new account",
    responses={
        401: {"description": "UNAUTHENTICATED"},
        422: {"description": "MALFORMED_REQUEST / UNSUPPORTED_CURRENCY"},
        429: {"description": "RATE_LIMITED"},
    },
)
@limiter.limit(default_limit, exempt_when=rate_limiting_is_disabled)
async def create_account(
    request: Request,
    payload: AccountCreateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> AccountPublic:
    """Open an account in a supported currency, owned by the caller.

    The new account starts with no ledger entries, and therefore a balance
    of exactly zero. There is no opening-balance parameter: value can only
    enter through a transaction, which is what keeps the
    reconciliation-to-zero invariant true.
    """
    if payload.currency not in settings.supported_currencies:
        raise UnsupportedCurrencyError(
            f"Currency {payload.currency!r} is not supported. Supported: "
            f"{', '.join(settings.supported_currencies)}.",
            context={"currency": payload.currency},
        )

    document = await account_service.create_account(
        owner_id=user.id,
        currency=payload.currency,
        label=payload.label,
    )
    return AccountPublic.from_document(document)


@router.get(
    "",
    response_model=AccountListResponse,
    status_code=status.HTTP_200_OK,
    summary="List the caller's accounts",
    responses={
        401: {"description": "UNAUTHENTICATED"},
        429: {"description": "RATE_LIMITED"},
    },
)
@limiter.limit(default_limit, exempt_when=rate_limiting_is_disabled)
async def list_accounts(
    request: Request,
    limit: int = Query(
        default=account_service.DEFAULT_PAGE_SIZE,
        ge=1,
        le=account_service.MAX_PAGE_SIZE,
    ),
    offset: int = Query(default=0, ge=0),
    user: AuthenticatedUser = Depends(get_current_user),
) -> AccountListResponse:
    """List accounts owned by the caller. Never another user's."""
    documents = await account_service.list_accounts_for_owner(
        user.id, limit=limit, offset=offset
    )
    return AccountListResponse(
        accounts=[AccountPublic.from_document(doc) for doc in documents],
        page=PageMeta(limit=limit, offset=offset, returned=len(documents)),
    )


@router.get(
    "/{account_id}",
    response_model=AccountPublic,
    status_code=status.HTTP_200_OK,
    summary="Read one account",
    responses={
        401: {"description": "UNAUTHENTICATED"},
        404: {"description": "ACCOUNT_NOT_FOUND"},
        429: {"description": "RATE_LIMITED"},
    },
)
@limiter.limit(default_limit, exempt_when=rate_limiting_is_disabled)
async def read_account(
    request: Request,
    account_id: ObjectIdStr,
    user: AuthenticatedUser = Depends(get_current_user),
) -> AccountPublic:
    """Read an account the caller owns.

    Returns ACCOUNT_NOT_FOUND, not a 403, for an account owned by someone
    else, so this route cannot be used to discover which account ids exist.
    """
    document = await account_service.get_owned_account_or_raise(account_id, user)
    return AccountPublic.from_document(document)


@router.get(
    "/{account_id}/balance",
    response_model=BalanceResponse,
    status_code=status.HTTP_200_OK,
    summary="Compute an account's balance from its ledger entries",
    responses={
        401: {"description": "UNAUTHENTICATED"},
        404: {"description": "ACCOUNT_NOT_FOUND"},
        429: {"description": "RATE_LIMITED"},
    },
)
@limiter.limit(default_limit, exempt_when=rate_limiting_is_disabled)
async def read_balance(
    request: Request,
    account_id: ObjectIdStr,
    user: AuthenticatedUser = Depends(get_current_user),
) -> BalanceResponse:
    """Return the account's balance, computed on demand.

    This figure is not read from a stored field. It is an aggregation over
    this account's immutable ledger entries, summing credits as positive
    and debits as negative, performed at the moment of the request. An
    account with no entries has a balance of exactly 0.

    `entry_count` is included so a caller can see how many entries the
    figure was derived from, which makes it evident that the number is
    computed rather than cached.
    """
    document = await account_service.get_owned_account_or_raise(account_id, user)
    balance = await balance_service.compute_balance(document["_id"])
    return balance.to_response(document["_id"], document["currency"])


@router.post(
    "/{account_id}/funding",
    response_model=TransactionPublic,
    status_code=status.HTTP_201_CREATED,
    summary="Bring external value into an account",
    responses={
        400: {"description": "IDEMPOTENCY_KEY_REQUIRED"},
        401: {"description": "UNAUTHENTICATED"},
        404: {"description": "ACCOUNT_NOT_FOUND"},
        409: {"description": "DUPLICATE_SUBMISSION / ACCOUNT_NOT_ACTIVE"},
        422: {"description": "MALFORMED_REQUEST / CURRENCY_MISMATCH"},
        429: {"description": "RATE_LIMITED"},
    },
)
@limiter.limit(transaction_limit, exempt_when=rate_limiting_is_disabled)
async def fund_account(
    request: Request,
    account_id: ObjectIdStr,
    payload: FundingCreateRequest,
    idempotency_key: str = Depends(require_idempotency_key),
    user: AuthenticatedUser = Depends(get_current_user),
) -> TransactionPublic:
    """Credit this account, debiting the ledger's SYSTEM boundary account.

    This is how value enters the ledger. Double-entry bookkeeping makes the
    need explicit: if every transaction nets to zero and every account
    starts at zero, no account can hold a positive balance unless some
    account is allowed to go negative. The SYSTEM boundary account is that
    account, and its negative balance is a meaningful figure, namely the
    total value held across all user accounts in that currency.

    Because the SYSTEM account is the other side of this entry, the
    reconciliation invariant (every ledger entry in the system nets to
    zero) stays exactly true through funding, just as it does through a
    transfer.

    **This is not a production deposit flow.** In a real system the same
    ledger write would be driven by a settlement webhook from a payment
    provider, after money had actually moved. PRD.md puts real payment
    rails out of scope, so this endpoint is the seam where that integration
    would attach. It requires an `Idempotency-Key` like any other
    transaction, and it goes through exactly the same atomic
    `ledger_service` path.
    """
    destination = await account_service.get_owned_account_or_raise(account_id, user)

    system_account = await account_service.get_system_account(payload.currency)
    if system_account is None:
        raise UnsupportedCurrencyError(
            f"No ledger boundary account exists for {payload.currency!r}.",
            context={"currency": payload.currency},
        )

    result = await ledger_service.create_transfer(
        idempotency_key=idempotency_key,
        source_account_id=system_account["_id"],
        destination_account_id=destination["_id"],
        amount_minor=payload.amount_minor,
        currency=payload.currency,
        description=payload.description,
        kind=TransactionKind.FUNDING,
        submitted_by=user.id,
    )
    return TransactionPublic.from_document(result.transaction_document)
